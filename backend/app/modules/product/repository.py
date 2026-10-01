"""product 模块的数据访问层。只碰 ``product`` schema。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.product.models import Category, Sku, SkuSpec, SpecGroup, SpecValue, Spu

# ============================================================
# 类目
# ============================================================


async def list_categories(session: AsyncSession, *, only_active: bool = True) -> list[Category]:
    stmt = select(Category).order_by(Category.path, Category.sort, Category.id)
    if only_active:
        stmt = stmt.where(Category.status == 1)
    result = await session.scalars(stmt)
    return list(result)


async def get_category(session: AsyncSession, category_id: int) -> Category | None:
    return await session.get(Category, category_id)


async def category_name_exists(session: AsyncSession, parent_id: int | None, name: str) -> bool:
    stmt = select(Category.id).where(Category.name == name)
    stmt = stmt.where(Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id)
    return await session.scalar(stmt.limit(1)) is not None


async def insert_category(session: AsyncSession, category: Category) -> Category:
    session.add(category)
    await session.flush()
    return category


async def list_descendant_category_ids(session: AsyncSession, path: str) -> list[int]:
    """含自身的整棵子树。用物化路径前缀匹配，一次查询拿到，不用递归。"""
    result = await session.scalars(select(Category.id).where(Category.path.startswith(path)))
    return list(result)


async def list_categories_by_ids(session: AsyncSession, category_ids: Sequence[int]) -> list[Category]:
    if not category_ids:
        return []
    result = await session.scalars(select(Category).where(Category.id.in_(category_ids)))
    return list(result)


async def count_children(session: AsyncSession, category_id: int) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(Category).where(Category.parent_id == category_id)
        )
    ) or 0


# ============================================================
# SPU
# ============================================================


async def insert_spu(session: AsyncSession, spu: Spu) -> Spu:
    session.add(spu)
    await session.flush()
    return spu


async def get_spu(session: AsyncSession, spu_id: int, *, include_deleted: bool = False) -> Spu | None:
    stmt = select(Spu).where(Spu.id == spu_id)
    if not include_deleted:
        stmt = stmt.where(Spu.deleted.is_(False))
    return await session.scalar(stmt)


async def update_spu_fields(session: AsyncSession, spu_id: int, values: dict[str, Any]) -> None:
    if not values:
        return
    values["updated_at"] = func.now()
    values["version"] = Spu.version + 1
    await session.execute(update(Spu).where(Spu.id == spu_id).values(**values))


async def list_spus_by_ids(session: AsyncSession, spu_ids: Sequence[int]) -> list[Spu]:
    if not spu_ids:
        return []
    result = await session.scalars(select(Spu).where(Spu.id.in_(spu_ids), Spu.deleted.is_(False)))
    return list(result)


async def search_spus(
    session: AsyncSession,
    *,
    keywords: list[str],
    category_ids: list[int] | None,
    price_from: int | None,
    price_to: int | None,
    shop_id: int | None,
    status: int | None,
    on_shelf_only: bool,
    order_by: Sequence[Any],
    cursor_clause: Any | None,
    limit: int,
) -> list[Spu]:
    """搜索/浏览商品。

    - ``keywords``：按空格拆好的词，每个词都要命中 search_text（AND 语义）
    - 价格区间用「区间相交」判断：``price_max >= from AND price_min <= to``
    - 分页用游标（行值比较），不用 OFFSET
    """
    stmt: Select[Any] = select(Spu).where(Spu.deleted.is_(False))

    if on_shelf_only:
        stmt = stmt.where(Spu.status == 2)
    if status is not None:
        stmt = stmt.where(Spu.status == status)
    if shop_id is not None:
        stmt = stmt.where(Spu.shop_id == shop_id)
    if category_ids:
        stmt = stmt.where(Spu.category_id.in_(category_ids))
    if price_from is not None:
        stmt = stmt.where(Spu.price_max >= price_from)
    if price_to is not None:
        stmt = stmt.where(Spu.price_min <= price_to)
    for word in keywords:
        # 转义 LIKE 的通配符，避免用户输入 % 把全表带出来
        escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Spu.search_text.ilike(f"%{escaped}%", escape="\\"))
    if cursor_clause is not None:
        stmt = stmt.where(cursor_clause)

    stmt = stmt.order_by(*order_by).limit(limit)
    result = await session.scalars(stmt)
    return list(result)


def row_value_cursor(column: Any, value: Any, row_id: int, *, descending: bool) -> Any:
    """行值比较实现游标分页，可用上 ``(column, id)`` 索引。

    ``tuple_(a, b) < (:v, :id)`` 等价于 ``a < :v OR (a = :v AND b < :id)``，
    但写法更短、也能被 PG 正确优化。
    """
    if descending:
        return tuple_(column, Spu.id) < tuple_(value, row_id)
    return tuple_(column, Spu.id) > tuple_(value, row_id)


# ============================================================
# SKU
# ============================================================


async def insert_skus(session: AsyncSession, skus: list[Sku]) -> None:
    session.add_all(skus)
    await session.flush()


async def get_sku(session: AsyncSession, sku_id: int) -> Sku | None:
    return await session.get(Sku, sku_id)


async def list_skus_by_ids(session: AsyncSession, sku_ids: Sequence[int]) -> list[Sku]:
    if not sku_ids:
        return []
    result = await session.scalars(select(Sku).where(Sku.id.in_(sku_ids)))
    return list(result)


async def list_skus_by_spu(session: AsyncSession, spu_id: int) -> list[Sku]:
    result = await session.scalars(select(Sku).where(Sku.spu_id == spu_id).order_by(Sku.price, Sku.id))
    return list(result)


async def list_skus_by_spus(session: AsyncSession, spu_ids: Sequence[int]) -> list[Sku]:
    if not spu_ids:
        return []
    result = await session.scalars(
        select(Sku).where(Sku.spu_id.in_(spu_ids)).order_by(Sku.spu_id, Sku.price, Sku.id)
    )
    return list(result)


async def update_sku_fields(session: AsyncSession, sku_id: int, values: dict[str, Any]) -> None:
    if not values:
        return
    values["updated_at"] = func.now()
    values["version"] = Sku.version + 1
    await session.execute(update(Sku).where(Sku.id == sku_id).values(**values))


async def update_sku_status_by_spu(session: AsyncSession, spu_id: int, status: int) -> None:
    await session.execute(
        update(Sku).where(Sku.spu_id == spu_id).values(status=status, updated_at=func.now())
    )


async def price_range_of_spu(session: AsyncSession, spu_id: int) -> tuple[int, int]:
    """在售 SKU 的价格区间，用于维护 SPU 的展示价。"""
    row = (
        await session.execute(
            select(func.min(Sku.price), func.max(Sku.price)).where(Sku.spu_id == spu_id, Sku.status == 1)
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


# ============================================================
# 规格
# ============================================================


async def insert_spec_groups(session: AsyncSession, groups: list[SpecGroup]) -> None:
    session.add_all(groups)
    await session.flush()


async def insert_spec_values(session: AsyncSession, values: list[SpecValue]) -> None:
    session.add_all(values)
    await session.flush()


async def insert_sku_specs(session: AsyncSession, rows: list[SkuSpec]) -> None:
    session.add_all(rows)
    await session.flush()


async def list_spec_groups(session: AsyncSession, spu_id: int) -> list[SpecGroup]:
    result = await session.scalars(
        select(SpecGroup).where(SpecGroup.spu_id == spu_id).order_by(SpecGroup.sort, SpecGroup.id)
    )
    return list(result)


async def list_spec_values(session: AsyncSession, group_ids: Sequence[int]) -> list[SpecValue]:
    if not group_ids:
        return []
    result = await session.scalars(
        select(SpecValue).where(SpecValue.group_id.in_(group_ids)).order_by(SpecValue.sort, SpecValue.id)
    )
    return list(result)


async def list_sku_specs(session: AsyncSession, sku_ids: Sequence[int]) -> list[SkuSpec]:
    if not sku_ids:
        return []
    result = await session.scalars(select(SkuSpec).where(SkuSpec.sku_id.in_(sku_ids)))
    return list(result)


async def delete_specs_of_spu(session: AsyncSession, spu_id: int) -> None:
    """删除某 SPU 的全部规格数据。只在删除草稿商品时用。"""
    group_ids = await session.scalars(select(SpecGroup.id).where(SpecGroup.spu_id == spu_id))
    group_id_list = list(group_ids)
    if group_id_list:
        await session.execute(delete(SkuSpec).where(SkuSpec.spec_group_id.in_(group_id_list)))
        await session.execute(delete(SpecValue).where(SpecValue.group_id.in_(group_id_list)))
        await session.execute(delete(SpecGroup).where(SpecGroup.id.in_(group_id_list)))


async def count_on_shelf_spus_by_category(session: AsyncSession, category_id: int) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Spu)
            .where(Spu.category_id == category_id, Spu.deleted.is_(False))
        )
    ) or 0


async def update_search_text(session: AsyncSession, spu_id: int, search_text: str) -> None:
    await session.execute(
        update(Spu).where(Spu.id == spu_id).values(search_text=search_text, updated_at=func.now())
    )
