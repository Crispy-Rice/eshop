"""inventory 模块的数据访问层。

只读写 ``inventory`` schema 下的表。**不碰 product 的表**——
需要 SKU 的标题/编码时由 service 层调 ``product.service.batch_get_skus``，
保持模块边界（docs/01 §2）。

分页沿用**键集游标**而不是 OFFSET：雪花的 id 本身大致按时间递增，
``WHERE id < :cursor ORDER BY id DESC`` 既简单又不受翻页期间新数据的影响。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import next_id
from app.modules.inventory.models import (
    WAREHOUSE_ENABLED,
    SkuStock,
    StockFlow,
    Warehouse,
)


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
    session: AsyncSession, shop_id: int, name: str, region_code: str
) -> Warehouse:
    """建仓。**该店铺还没有仓库时自动设为默认仓**。

    默认仓的意义：下单时不指定仓库就走它。第一期不实现多仓路由，
    所以每个店铺必须有且只有一个默认仓。
    """
    has_any = await session.scalar(select(Warehouse.id).where(Warehouse.shop_id == shop_id))
    warehouse = Warehouse(
        id=next_id(),
        shop_id=shop_id,
        name=name,
        region_code=region_code,
        is_default=has_any is None,
        status=WAREHOUSE_ENABLED,
    )
    session.add(warehouse)
    await session.flush()
    return warehouse


async def ensure_default_warehouse(session: AsyncSession, shop_id: int) -> Warehouse:
    """取默认仓，没有就建一个。

    库存页、调整接口都先调它 —— 商家不该为了调一次库存先去建仓库。
    """
    existing = await get_default_warehouse(session, shop_id)
    if existing is not None:
        return existing
    return await create_warehouse(session, shop_id, "默认仓库", "")


# ============================================================
# 库存
# ============================================================
async def list_stock(
    session: AsyncSession,
    shop_id: int,
    *,
    warehouse_id: int | None = None,
    sku_id: int | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> list[SkuStock]:
    stmt: Select = select(SkuStock).where(SkuStock.shop_id == shop_id)
    if warehouse_id is not None:
        stmt = stmt.where(SkuStock.warehouse_id == warehouse_id)
    if sku_id is not None:
        stmt = stmt.where(SkuStock.sku_id == sku_id)
    if cursor is not None:
        stmt = stmt.where(SkuStock.id < int(cursor))
    stmt = stmt.order_by(SkuStock.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


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


async def sku_warehouses(session: AsyncSession, sku_ids: Sequence[int]) -> dict[int, int]:
    """批量取每个 SKU 的**发货仓**。

    运费按仓库分组计算，所以要先知道每个 SKU 从哪发（docs/06 §5）。
    第一期每个 SKU 只在一个仓有库存记录，所以取第一条即可；
    将来多仓铺货后，这里要改成"按库存量最大的仓"或引入仓路由策略。
    """
    if not sku_ids:
        return {}
    rows = await session.execute(
        select(SkuStock.sku_id, SkuStock.warehouse_id)
        .where(SkuStock.sku_id.in_(sku_ids))
        .order_by(SkuStock.sku_id, SkuStock.warehouse_id)
    )
    result: dict[int, int] = {}
    for sku_id, wh_id in rows:
        result.setdefault(int(sku_id), int(wh_id))
    return result


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
