"""inventory 模块的数据访问层。

只读写 ``inventory`` schema 下的表。**不碰 product 的表**——
需要 SKU 的标题/编码时由 service 层调 ``product.service.batch_get_skus``，
保持模块边界（docs/01 §2）。

分页沿用**键集游标**而不是 OFFSET：雪花的 id 本身大致按时间递增，
``WHERE id < :cursor ORDER BY id DESC`` 既简单又不受翻页期间新数据的影响。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import next_id
from app.modules.inventory.models import (
    WAREHOUSE_ENABLED,
    SkuStock,
    StockFlow,
    Warehouse,
    WarehouseRegionRule,
)
from app.modules.inventory.routing import region_level_of


# ============================================================
# 仓库
# ============================================================
async def list_warehouses(session: AsyncSession, shop_id: int) -> list[Warehouse]:
    result = await session.scalars(
        select(Warehouse)
        .where(Warehouse.shop_id == shop_id)
        .order_by(Warehouse.is_default.desc(), Warehouse.id)
    )
    return list(result)


async def get_warehouse(session: AsyncSession, shop_id: int, warehouse_id: int) -> Warehouse | None:
    return await session.scalar(
        select(Warehouse).where(
            Warehouse.id == warehouse_id,
            Warehouse.shop_id == shop_id,  # 带上 shop_id 就顺便做了越权校验
        )
    )


async def get_default_warehouse(session: AsyncSession, shop_id: int) -> Warehouse | None:
    return await session.scalar(
        select(Warehouse).where(Warehouse.shop_id == shop_id, Warehouse.is_default.is_(True))
    )


async def create_warehouse(
    session: AsyncSession, shop_id: int, *, values: dict[str, Any]
) -> Warehouse:
    """建仓。**该店铺还没有仓库时自动设为默认仓**。

    "每店必有且仅有一个默认仓"是一条不变量（路由的兜底靠它），起点就在这一句：
    第一个建出来的仓即默认仓；之后换默认要显式调 :func:`set_default_warehouse`
    （默认仓不允许停用，见 service）。
    """
    has_any = await session.scalar(select(Warehouse.id).where(Warehouse.shop_id == shop_id))
    warehouse = Warehouse(
        id=next_id(),
        shop_id=shop_id,
        is_default=has_any is None,
        status=WAREHOUSE_ENABLED,
        **values,
    )
    session.add(warehouse)
    await session.flush()
    return warehouse


async def update_warehouse_fields(
    session: AsyncSession, warehouse_id: int, values: dict[str, Any]
) -> None:
    if not values:
        return
    values["updated_at"] = func.now()
    await session.execute(update(Warehouse).where(Warehouse.id == warehouse_id).values(**values))


async def set_default_warehouse(session: AsyncSession, shop_id: int, warehouse_id: int) -> None:
    """换默认仓。**必须先摘掉旧的** —— ``uk_warehouse_default`` 是唯一索引，
    同一店铺同时有两个 ``is_default`` 会直接撞约束（这也是它在结构上被保证的原因）。
    """
    await session.execute(
        update(Warehouse)
        .where(Warehouse.shop_id == shop_id, Warehouse.is_default.is_(True))
        .values(is_default=False, updated_at=func.now())
    )
    await session.execute(
        update(Warehouse)
        .where(Warehouse.id == warehouse_id, Warehouse.shop_id == shop_id)
        .values(is_default=True, updated_at=func.now())
    )


# ============================================================
# 仓 → 覆盖区划
# ============================================================
async def list_region_rules(session: AsyncSession, shop_id: int) -> list[WarehouseRegionRule]:
    """该店铺的全部区域规则。路由按店**批量**取，所以走 shop_id（有索引）。"""
    result = await session.scalars(
        select(WarehouseRegionRule)
        .where(WarehouseRegionRule.shop_id == shop_id)
        .order_by(WarehouseRegionRule.region_code)
    )
    return list(result)


async def delete_region_rules_of_warehouse(session: AsyncSession, warehouse_id: int) -> None:
    await session.execute(
        delete(WarehouseRegionRule).where(WarehouseRegionRule.warehouse_id == warehouse_id)
    )


async def insert_region_rules(
    session: AsyncSession, *, shop_id: int, warehouse_id: int, codes: Sequence[str]
) -> None:
    for code in codes:
        session.add(
            WarehouseRegionRule(
                id=next_id(),
                shop_id=shop_id,
                warehouse_id=warehouse_id,
                region_code=code,
                region_level=region_level_of(code),
            )
        )
    await session.flush()


async def region_rule_owners(
    session: AsyncSession, *, shop_id: int, codes: Sequence[str], exclude_warehouse_id: int
) -> dict[str, int]:
    """这些区划码**被别的仓**认领了哪些。用于整体替换前的冲突检查。

    唯一键是 ``(shop_id, region_code)`` —— 一个地方只能由一个仓发货，
    所以商家把某个区划配给第二个仓时必须先提示他"那儿已经归别人了"，
    而不是丢一个数据库唯一约束错误给他。
    """
    if not codes:
        return {}
    rows = await session.execute(
        select(WarehouseRegionRule.region_code, WarehouseRegionRule.warehouse_id).where(
            WarehouseRegionRule.shop_id == shop_id,
            WarehouseRegionRule.region_code.in_(list(codes)),
            WarehouseRegionRule.warehouse_id != exclude_warehouse_id,
        )
    )
    return {str(code): int(wh_id) for code, wh_id in rows}


# ============================================================
# 库存
# ============================================================
async def list_stock(
    session: AsyncSession,
    shop_id: int,
    *,
    warehouse_id: int | None = None,
    sku_id: int | None = None,
    exclude_sku_ids: Sequence[int] = (),
    cursor: str | None = None,
    limit: int = 20,
) -> list[SkuStock]:
    """库存行，**按 id 倒序**（键集游标）。

    ``exclude_sku_ids`` 是已软删商品的 SKU（由 ``product.service`` 给，见那边的
    ``list_deleted_sku_ids``）：它们的库存行还在表里，但不该再出现在库存页。
    必须在这一层过滤而不是取回来再筛 —— 那样每页会少几条，游标翻页会错位。
    """
    stmt: Select = select(SkuStock).where(SkuStock.shop_id == shop_id)
    if warehouse_id is not None:
        stmt = stmt.where(SkuStock.warehouse_id == warehouse_id)
    if sku_id is not None:
        stmt = stmt.where(SkuStock.sku_id == sku_id)
    if exclude_sku_ids:
        stmt = stmt.where(SkuStock.sku_id.not_in(exclude_sku_ids))
    if cursor is not None:
        stmt = stmt.where(SkuStock.id < int(cursor))
    stmt = stmt.order_by(SkuStock.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def count_out_of_stock(
    session: AsyncSession,
    shop_id: int,
    *,
    exclude_sku_ids: Sequence[int] = (),
) -> int:
    """**可售为 0 的库存行数**，给 AI 助手的"店铺概览"用。

    ★ 口径写清楚：数的是**库存行**（一个 SKU 在一个仓的一条记录），不是商品数。
      同一个 SKU 摆在两个仓、两个仓都没货会算 2。助手要的是"有几处卖不动了"
      这个量级感，不是财务口径 —— 需要精确口径时它应该去查具体的行。

    ``exclude_sku_ids`` 与 ``list_stock`` 同源（已软删商品的 SKU）：
    不排掉的话商家会看到"有 3 处没货"，点进库存页却找不到那几行。
    """
    stmt = (
        select(func.count())
        .select_from(SkuStock)
        .where(SkuStock.shop_id == shop_id, SkuStock.available == 0)
    )
    if exclude_sku_ids:
        stmt = stmt.where(SkuStock.sku_id.not_in(exclude_sku_ids))
    return int(await session.scalar(stmt) or 0)


async def get_stock(session: AsyncSession, sku_id: int, warehouse_id: int) -> SkuStock | None:
    return await session.scalar(
        select(SkuStock).where(
            SkuStock.sku_id == sku_id, SkuStock.warehouse_id == warehouse_id
        )
    )


async def sum_available(session: AsyncSession, sku_id: int) -> int:
    """该 SKU 在所有仓的可售总量。买家侧展示用。"""
    total = await session.scalar(
        select(func.coalesce(func.sum(SkuStock.available), 0)).where(SkuStock.sku_id == sku_id)
    )
    return int(total or 0)


async def sum_available_by_skus(
    session: AsyncSession, sku_ids: Sequence[int]
) -> dict[int, int]:
    """批量取多个 SKU 的可售总量。

    购物车列表用它一次拿全（docs/02 §4 的 N+1 规避）：循环查 50 次商品和库存
    是这一层最典型的性能事故。**没有库存记录的 SKU 不出现在结果里**，
    调用方用 ``.get(sku_id, 0)`` 取值。
    """
    if not sku_ids:
        return {}
    rows = await session.execute(
        select(SkuStock.sku_id, func.coalesce(func.sum(SkuStock.available), 0))
        .where(SkuStock.sku_id.in_(sku_ids))
        .group_by(SkuStock.sku_id)
    )
    return {int(sku_id): int(total) for sku_id, total in rows}


async def available_of(
    session: AsyncSession, *, warehouse_ids: Sequence[int], sku_ids: Sequence[int]
) -> dict[tuple[int, int], int]:
    """批量取 ``(sku_id, warehouse_id) → available`` —— **按仓择仓**用（见 routing）。

    ★ 与 `sum_available_by_skus` 的区别就是**不跨仓求和**：缺货兜底要问的是
      "这个仓自己够不够"，而跨仓求和恰恰是这一轮要修掉的那个口径。
    ★ **没有库存记录的组合不出现在结果里**，调用方用 ``.get((sku, wh), 0)`` ——
      "没有那一行"的语义就是"这个仓没这个货"（`ensure_stock_rows` 只给商家打开过的
      仓补行，所以新仓、没铺过货的仓天然是 0）。
    """
    if not sku_ids or not warehouse_ids:
        return {}
    rows = await session.execute(
        select(SkuStock.sku_id, SkuStock.warehouse_id, SkuStock.available).where(
            SkuStock.sku_id.in_(sku_ids), SkuStock.warehouse_id.in_(warehouse_ids)
        )
    )
    return {(int(sku_id), int(wh_id)): int(available) for sku_id, wh_id, available in rows}


# ============================================================
# 流水
# ============================================================
async def list_flows(
    session: AsyncSession,
    shop_id: int,
    *,
    sku_id: int | None = None,
    warehouse_id: int | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> list[StockFlow]:
    """库存流水。按时间倒序。

    ``stock_flow`` 上没有 shop_id，所以 join ``sku_stock`` 来限定店铺 ——
    否则商家能看到别家的流水。
    """
    stmt: Select = (
        select(StockFlow)
        .join(
            SkuStock,
            (SkuStock.sku_id == StockFlow.sku_id)
            & (SkuStock.warehouse_id == StockFlow.warehouse_id),
        )
        .where(SkuStock.shop_id == shop_id)
    )
    if sku_id is not None:
        stmt = stmt.where(StockFlow.sku_id == sku_id)
    if warehouse_id is not None:
        stmt = stmt.where(StockFlow.warehouse_id == warehouse_id)
    if cursor is not None:
        stmt = stmt.where(StockFlow.id < int(cursor))
    stmt = stmt.order_by(StockFlow.id.desc()).limit(limit)
    return list(await session.scalars(stmt))
