"""cart 模块的数据访问层。

只读写 ``cart`` schema 下的表。**每个按用户的写操作都必须带 ``user_id`` 条件**——
这是防越权的唯一手段（改别人的购物车不该成功）。所有函数都把它作为必填参数，
而不是可选过滤条件。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import next_id
from app.modules.cart.models import MAX_NUM_PER_SKU, CartItem


async def upsert_item(
    session: AsyncSession,
    *,
    user_id: int,
    shop_id: int,
    sku_id: int,
    spu_id: int,
    price: int,
    num: int,
    source: int,
) -> None:
    """加购。同一 SKU 已存在则**累加** num，用 ``LEAST`` 封顶。

    用 PG 的 ``ON CONFLICT DO UPDATE`` 一次搞定（docs/02 §4）：

    - 原子，不需要"先查再决定 insert 还是 update"，那样在并发下有竞态
    - ``LEAST(item.num + excluded.num, 200)`` 让单 SKU 上限由数据库兜底，
      应用层的校验只是为了给出更好的报错，不是唯一防线
    """
    stmt = pg_insert(CartItem).values(
        id=next_id(),
        user_id=user_id,
        shop_id=shop_id,
        sku_id=sku_id,
        spu_id=spu_id,
        price_snapshot=price,
        num=num,
        source=source,
        selected=True,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uk_cart_user_sku",
        set_={
            "num": func.least(CartItem.num + stmt.excluded.num, MAX_NUM_PER_SKU),
            "updated_at": func.now(),
            # 重新加购时把商品重新勾上——用户刚主动加的东西不该是未选中的
            "selected": True,
        },
    )
    await session.execute(stmt)


async def list_items(
    session: AsyncSession, user_id: int, *, sku_ids: Sequence[int] | None = None
) -> list[CartItem]:
    """该用户的购物车项。按店铺、再加购时间排序，前端直接顺序渲染即可分组。"""
    stmt = select(CartItem).where(CartItem.user_id == user_id)
    if sku_ids is not None:
        if not sku_ids:
            return []
        stmt = stmt.where(CartItem.sku_id.in_(sku_ids))
    stmt = stmt.order_by(CartItem.shop_id, CartItem.created_at.desc())
    return list(await session.scalars(stmt))


async def count_items(session: AsyncSession, user_id: int) -> int:
    """购物车里有几种 SKU。用于整车上限校验与角标。"""
    return int(
        await session.scalar(
            select(func.count()).select_from(CartItem).where(CartItem.user_id == user_id)
        )
        or 0
    )


async def get_item(session: AsyncSession, user_id: int, sku_id: int) -> CartItem | None:
    return await session.scalar(
        select(CartItem).where(CartItem.user_id == user_id, CartItem.sku_id == sku_id)
    )


async def update_num(session: AsyncSession, user_id: int, sku_id: int, num: int) -> bool:
    """把数量**设为**指定值（SET 语义，不是累加）。返回是否有行被改。"""
    result = await session.execute(
        update(CartItem)
        .where(CartItem.user_id == user_id, CartItem.sku_id == sku_id)
        .values(num=num, updated_at=func.now())
    )
    return result.rowcount > 0


async def delete_items(session: AsyncSession, user_id: int, sku_ids: Sequence[int]) -> int:
    """批量删除。返回删除条数。"""
    if not sku_ids:
        return 0
    result = await session.execute(
        delete(CartItem).where(CartItem.user_id == user_id, CartItem.sku_id.in_(sku_ids))
    )
    return result.rowcount


async def set_selected(
    session: AsyncSession, user_id: int, sku_ids: Sequence[int], selected: bool
) -> int:
    """批量勾选/取消。不传 skuIds 表示全选/全不选。"""
    stmt = update(CartItem).where(CartItem.user_id == user_id)
    if sku_ids:
        stmt = stmt.where(CartItem.sku_id.in_(sku_ids))
    result = await session.execute(stmt.values(selected=selected, updated_at=func.now()))
    return result.rowcount
