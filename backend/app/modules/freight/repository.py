"""freight 模块的数据访问层。

只读写 ``freight`` schema 下的表。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.freight.models import (
    FreightExcludeRegion,
    FreightRegionRule,
    FreightTemplate,
    SkuFreightBind,
)


# ============================================================
# 模板
# ============================================================
async def get_template(session: AsyncSession, template_id: int) -> FreightTemplate | None:
    return await session.get(FreightTemplate, template_id)


async def get_shop_template(
    session: AsyncSession, shop_id: int, template_id: int
) -> FreightTemplate | None:
    """带店铺校验的取模板 —— 顺便就做了越权检查。"""
    return await session.scalar(
        select(FreightTemplate).where(
            FreightTemplate.id == template_id, FreightTemplate.shop_id == shop_id
        )
    )


async def list_templates(session: AsyncSession, shop_id: int) -> list[FreightTemplate]:
    return list(
        await session.scalars(
            select(FreightTemplate)
            .where(FreightTemplate.shop_id == shop_id)
            .order_by(FreightTemplate.id)
        )
    )


async def list_templates_by_ids(
    session: AsyncSession, template_ids: Sequence[int]
) -> list[FreightTemplate]:
    if not template_ids:
        return []
    return list(
        await session.scalars(
            select(FreightTemplate).where(FreightTemplate.id.in_(template_ids))
        )
    )


async def insert_template(session: AsyncSession, tpl: FreightTemplate) -> FreightTemplate:
    session.add(tpl)
    await session.flush()
    return tpl


async def get_default_template(session: AsyncSession, shop_id: int) -> FreightTemplate | None:
    """店铺默认模板。**不过滤状态** —— 调用方要能区分"没设默认"和"默认被停用了"，
    这两种情况给商家的提示不一样。"""
    return await session.scalar(
        select(FreightTemplate).where(
            FreightTemplate.shop_id == shop_id, FreightTemplate.is_default.is_(True)
        )
    )


async def list_default_templates(
    session: AsyncSession, shop_ids: Sequence[int]
) -> list[FreightTemplate]:
    """批量取各店的默认模板。一单可能跨店，按 shop_id 一次查完，不做 N+1。"""
    if not shop_ids:
        return []
    return list(
        await session.scalars(
            select(FreightTemplate).where(
                FreightTemplate.shop_id.in_(shop_ids), FreightTemplate.is_default.is_(True)
            )
        )
    )


async def set_default_template(
    session: AsyncSession, *, shop_id: int, template_id: int
) -> None:
    """把某条模板设为该店默认。

    ★ 必须**先清旧的再设新的**：``uk_freight_tpl_default`` 是部分唯一索引，
      同一个店铺只允许一条 ``is_default``，顺序反过来会直接撞索引。
    """
    await session.execute(
        update(FreightTemplate)
        .where(FreightTemplate.shop_id == shop_id, FreightTemplate.is_default.is_(True))
        .values(is_default=False)
    )
    await session.execute(
        update(FreightTemplate)
        .where(FreightTemplate.id == template_id, FreightTemplate.shop_id == shop_id)
        .values(is_default=True)
    )


# ============================================================
# 区域规则
# ============================================================
async def list_region_rules(
    session: AsyncSession, template_id: int
) -> list[FreightRegionRule]:
    return list(
        await session.scalars(
            select(FreightRegionRule).where(FreightRegionRule.template_id == template_id)
        )
    )


async def list_region_rules_by_templates(
    session: AsyncSession, template_ids: Sequence[int]
) -> list[FreightRegionRule]:
    if not template_ids:
        return []
    return list(
        await session.scalars(
            select(FreightRegionRule).where(FreightRegionRule.template_id.in_(template_ids))
        )
    )


async def delete_region_rules(session: AsyncSession, template_id: int) -> None:
    await session.execute(
        delete(FreightRegionRule).where(FreightRegionRule.template_id == template_id)
    )


async def insert_region_rules(
    session: AsyncSession, rules: list[FreightRegionRule]
) -> None:
    session.add_all(rules)
    await session.flush()


# ============================================================
# 不发货区域
# ============================================================
async def list_exclude_regions(
    session: AsyncSession, template_id: int
) -> list[FreightExcludeRegion]:
    return list(
        await session.scalars(
            select(FreightExcludeRegion).where(
                FreightExcludeRegion.template_id == template_id
            )
        )
    )


async def list_exclude_by_templates(
    session: AsyncSession, template_ids: Sequence[int]
) -> list[FreightExcludeRegion]:
    if not template_ids:
        return []
    return list(
        await session.scalars(
            select(FreightExcludeRegion).where(
                FreightExcludeRegion.template_id.in_(template_ids)
            )
        )
    )


async def delete_exclude_regions(session: AsyncSession, template_id: int) -> None:
    await session.execute(
        delete(FreightExcludeRegion).where(FreightExcludeRegion.template_id == template_id)
    )


async def insert_exclude_regions(
    session: AsyncSession, rows: list[FreightExcludeRegion]
) -> None:
    session.add_all(rows)
    await session.flush()


# ============================================================
# SKU 绑定
# ============================================================
async def list_binds_by_skus(
    session: AsyncSession, sku_ids: Sequence[int]
) -> list[SkuFreightBind]:
    """批量取 SKU 的绑定。用于算价时一次性解析，避免逐个查。

    只取启用的，按优先级降序 —— 调用方取每个 SKU 的第一条即可。
    """
    if not sku_ids:
        return []
    return list(
        await session.scalars(
            select(SkuFreightBind)
            .where(SkuFreightBind.sku_id.in_(sku_ids), SkuFreightBind.enabled.is_(True))
            .order_by(SkuFreightBind.sku_id, SkuFreightBind.priority.desc())
        )
    )


async def list_binds_by_template(
    session: AsyncSession, template_id: int
) -> list[SkuFreightBind]:
    return list(
        await session.scalars(
            select(SkuFreightBind).where(SkuFreightBind.template_id == template_id)
        )
    )


async def count_binds_of_template(
    session: AsyncSession, template_id: int, *, exclude_sku_ids: Sequence[int] = ()
) -> int:
    """该模板绑了多少 SKU。

    ``exclude_sku_ids`` 传已软删商品的 SKU 时，数字与抽屉里能看到的明细一致
    （软删不动 ``sku_freight_bind``，残留行会一直挂着）。改模板时的"影响面"
    提示不传这个参数 —— 那种场景算的是"有多少行会被改写"，绑几行算几行。
    """
    stmt = (
        select(func.count())
        .select_from(SkuFreightBind)
        .where(SkuFreightBind.template_id == template_id)
    )
    if exclude_sku_ids:
        stmt = stmt.where(SkuFreightBind.sku_id.not_in(exclude_sku_ids))
    return int(await session.scalar(stmt) or 0)


async def upsert_bind(
    session: AsyncSession,
    *,
    sku_id: int,
    template_id: int,
    warehouse_id: int,
    priority: int,
    bind_id: int,
) -> None:
    """绑定 SKU 到模板。同一 (sku, 模板, 仓库) 已存在时更新优先级。

    用 ``ON CONFLICT`` 而不是先查再插：并发下后者有竞态，
    而且唯一约束本来就该由数据库来保证。
    """
    stmt = pg_insert(SkuFreightBind).values(
        id=bind_id,
        sku_id=sku_id,
        template_id=template_id,
        warehouse_id=warehouse_id,
        priority=priority,
        enabled=True,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uk_sku_freight_bind",
        set_={"priority": priority, "enabled": True},
    )
    await session.execute(stmt)


async def get_bind(
    session: AsyncSession, sku_id: int, warehouse_id: int
) -> SkuFreightBind | None:
    """取某 SKU 在某仓的生效绑定（优先级最高的那条）。"""
    stmt: Select = (
        select(SkuFreightBind)
        .where(
            SkuFreightBind.sku_id == sku_id,
            SkuFreightBind.warehouse_id == warehouse_id,
            SkuFreightBind.enabled.is_(True),
        )
        .order_by(SkuFreightBind.priority.desc())
        .limit(1)
    )
    return await session.scalar(stmt)
