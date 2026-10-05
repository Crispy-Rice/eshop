"""product 模块的数据访问层。只碰 ``product`` schema。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Numeric, Select, delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.product.models import Category, Sku, SkuSpec, SpecGroup, SpecValue, Spu

# ============================================================
# 类目
# ============================================================


async def list_categories(session: AsyncSession, *, only_active: bool = True) -> list[Category]:
    # ★ 按 (sort, id) 排，**不能**把 path 放在前面。兄弟节点的 path 各自含自己的
    #   id（``/1/10/`` 与 ``/1/20/``），按 path 排等价于按 id 排，``sort`` 就永远
    #   轮不到 —— 树是照这个顺序往父节点的 children 里 append 的，顺序错了运营在
    #   界面上改排序也不会动。
    stmt = select(Category).order_by(Category.sort, Category.id)
    if only_active:
        stmt = stmt.where(Category.status == 1)
    result = await session.scalars(stmt)
    return list(result)


async def get_category(session: AsyncSession, category_id: int) -> Category | None:
    return await session.get(Category, category_id)


async def category_name_exists(
    session: AsyncSession,
    parent_id: int | None,
    name: str,
    *,
    exclude_id: int | None = None,
) -> bool:
    """同级下是否已有同名类目。

    ``exclude_id`` 是给"改自己的名字"和"移动到自己当前的父节点"用的 —— 不排掉
    自己，这两种操作都会被自己那个同名行判成重名。
    """
    stmt = select(Category.id).where(Category.name == name)
    stmt = stmt.where(Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id)
    if exclude_id is not None:
        stmt = stmt.where(Category.id != exclude_id)
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


async def count_children(session: AsyncSession, category_id: int, *, only_active: bool = False) -> int:
    stmt = select(func.count()).select_from(Category).where(Category.parent_id == category_id)
    if only_active:
        stmt = stmt.where(Category.status == 1)
    return (await session.scalar(stmt)) or 0


async def count_spus_by_category_ids(
    session: AsyncSession, category_ids: Sequence[int]
) -> dict[int, int]:
    """一次 GROUP BY 拿到多个类目下的商品数。

    管理端整棵树都要显示"N 件商品"，逐个查会变成 N+1。
    """
    if not category_ids:
        return {}
    rows = await session.execute(
        select(Spu.category_id, func.count())
        .where(Spu.category_id.in_(category_ids), Spu.deleted.is_(False))
        .group_by(Spu.category_id)
    )
    return {int(category_id): int(count) for category_id, count in rows}


async def max_level_in_subtree(session: AsyncSession, path: str) -> int:
    """子树里最深的层级。移动前判"搬完会不会超过三级"用。"""
    deepest = await session.scalar(select(func.max(Category.level)).where(Category.path.startswith(path)))
    return int(deepest) if deepest is not None else 1


async def rewrite_subtree(
    session: AsyncSession, *, old_prefix: str, new_prefix: str, level_delta: int
) -> None:
    """把整棵子树搬到新前缀下，一条 SQL 搞定，不加载子树。

    物化路径的好处就在这里：子孙的 ``path`` 一定以祖先的 ``path`` 开头，所以
    换个前缀、层级整体平移就够，不用递归。

    ★ 前缀一律带尾斜杠。``path`` 是 ``/1/10/`` 这种形态，带斜杠才能保证
    ``/1/1/`` 不会误伤 ``/1/10/``（``LIKE '/1/1/%'`` 对 ``/1/10/`` 不成立）。
    """
    await session.execute(
        update(Category)
        .where(Category.path.startswith(old_prefix))
        .values(
            path=func.concat(new_prefix, func.substring(Category.path, len(old_prefix) + 1)),
            level=Category.level + level_delta,
            updated_at=func.now(),
        )
    )


async def update_category_fields(session: AsyncSession, category_id: int, values: dict[str, Any]) -> None:
    """部分更新。照 ``update_spu_fields`` 写，但**没有 version 自增** —— 类目表没有这列。"""
    if not values:
        return
    values["updated_at"] = func.now()
    await session.execute(update(Category).where(Category.id == category_id).values(**values))


async def delete_category(session: AsyncSession, category_id: int) -> None:
    await session.execute(delete(Category).where(Category.id == category_id))


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


async def soft_delete_spu(session: AsyncSession, spu_id: int) -> None:
    """软删商品 —— 只置 ``deleted``，**不物理删**。

    ``product.spu.deleted`` 这个字段一直存在、读侧也全都接好了（每处查询都带
    ``deleted = false``），只是一直没有代码去写它。物理删除会破坏订单语义
    （订单里存的是商品快照，行没了历史订单就指向空气），而且误删不可逆。

    连带影响都已经被读侧处理好了，不需要在这里补：
    - 商城搜索/详情：过滤 ``deleted``，直接消失
    - 平台的商品列表：同一条路径，一起消失
    - 库存页 / 运费绑定：别的模块调 ``list_deleted_sku_ids`` 把残留行一起排掉
    - 历史订单/售后：读快照，不受影响
    """
    await update_spu_fields(session, spu_id, {"deleted": True})


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


async def list_skus_by_shop(session: AsyncSession, shop_id: int, *, limit: int = 500) -> list[Sku]:
    """按店铺列 SKU —— **不含已软删商品**的 SKU。

    给 inventory 用：商家发布商品后要在库存页看到它，而库存页是按 SKU 驱动的，
    所以需要"这个店铺有哪些 SKU"这个查询。加上限避免店铺很大时一次拉爆。

    ★ 必须排掉已软删的商品：这个结果是被拿去**补建库存行**的
      （``inventory.service.ensure_stock_rows``）。不排掉的话，商家刚删掉一个
      还没建过库存的商品，一打开库存页就会凭空多出几行 0 库存的残留行。

      已经建过库存行的已删商品是另一回事：那些行留在 ``inventory.sku_stock``
      里，由库存页的读侧用 ``list_deleted_sku_ids`` 排掉，也不在这里补新的。
    """
    result = await session.scalars(
        select(Sku)
        .join(Spu, Spu.id == Sku.spu_id)
        .where(Sku.shop_id == shop_id, Spu.deleted.is_(False))
        .order_by(Sku.id)
        .limit(limit)
    )
    return list(result)


async def list_deleted_sku_ids(session: AsyncSession, shop_id: int) -> list[int]:
    """**已软删商品**下面的 SKU id。

    给别的模块的读侧做过滤用：软删不动别的 schema，所以 ``inventory.sku_stock``、
    ``freight.sku_freight_bind`` 里可能还留着这些 SKU 的行。行不能删（见
    ``soft_delete_spu``），但也不该再出现在商家的操作界面上。

    走 id 列表而不是让调用方自己 join ``product`` 的表 —— 别的模块不许碰 product
    schema（docs/01 §2），这里是那扇门。
    """
    result = await session.scalars(
        select(Sku.id)
        .join(Spu, Spu.id == Sku.spu_id)
        .where(Sku.shop_id == shop_id, Spu.deleted.is_(True))
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


async def incr_spu_review_stats(
    session: AsyncSession,
    spu_id: int,
    *,
    count_delta: int,
    score_delta: int,
    good_delta: int,
) -> None:
    """增减 SPU 的评价统计。由评价模块在**同一个事务内**调用。

    ``avg_score`` 由三个计数推导，**计数归零就用 ``nullif`` 置成 NULL** ——
    "零评价 ⇒ avg_score IS NULL"是展示层"无评价不显示评分"的依据
    （docs/12 §9）。

    ``good_delta`` 由调用方算好（``+1 if score >= 4 else 0``），不把阈值判断塞进 SQL，
    这样阈值是纯函数、能单测。

    全量重算不走这里 —— 那是一条 ``UPDATE ... FROM`` 的运维 SQL，见
    ``review/tasks.py``（docs/12 §3.2 明确把那种跨 schema 的批量修正列为运维例外）。
    """
    new_count = Spu.review_count + count_delta
    new_sum = Spu.review_score_sum + score_delta
    await session.execute(
        update(Spu)
        .where(Spu.id == spu_id)
        .values(
            review_count=new_count,
            review_score_sum=new_sum,
            good_review_count=Spu.good_review_count + good_delta,
            avg_score=func.round(func.cast(new_sum, Numeric(10, 4)) / func.nullif(new_count, 0), 2),
            updated_at=func.now(),
            version=Spu.version + 1,
        )
    )


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
