"""notify 模块的数据访问。纯函数，收调用方的 session，返回 ORM 行。"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notify.models import SiteMessage


async def insert_ignore_dup(
    session: AsyncSession,
    *,
    user_id: int,
    msg_type: str,
    title: str,
    body: str,
    biz_key: str,
    link_type: str,
    link_value: str | None,
) -> bool:
    """写一条站内信；``biz_key`` 已存在就静默跳过。返回是否真的写入了。

    ★ ``ON CONFLICT DO NOTHING`` 是这里唯一的幂等保证，而它必须存在：
      调用方有**两条**路径 —— support 的同事务直写、worker 的**至少一次**投递
      —— 都可能把同一条消息交过来两次。

    ``id`` 不传：``site_message.id`` 是 ``GENERATED ALWAYS AS IDENTITY``，
    显式赋值会被 PG 拒绝（这不是雪花 ID）。
    """
    stmt = (
        pg_insert(SiteMessage)
        .values(
            user_id=user_id,
            msg_type=msg_type,
            title=title,
            body=body,
            biz_key=biz_key,
            link_type=link_type,
            link_value=link_value,
            is_read=False,
        )
        .on_conflict_do_nothing(constraint="uk_site_message_biz")
    )
    result = await session.execute(stmt)
    return result.rowcount == 1


async def list_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    unread_only: bool,
    cursor: int | None,
    limit: int,
) -> list[SiteMessage]:
    """我的站内信，倒序。

    ★ 游标用 ``id`` 而不是 ``(created_at, id)``：``id`` 是 IDENTITY，**单调递增**，
      单列就能给出稳定顺序 —— 比会话那边（按 ``last_message_at`` 排，有并列）
      少一层复合游标。多取一条让调用方算 ``has_more``。
    """
    stmt = select(SiteMessage).where(SiteMessage.user_id == user_id)
    if unread_only:
        stmt = stmt.where(SiteMessage.is_read.is_(False))
    if cursor is not None:
        stmt = stmt.where(SiteMessage.id < cursor)
    stmt = stmt.order_by(SiteMessage.id.desc()).limit(limit + 1)
    return list((await session.scalars(stmt)).all())


async def count_unread(session: AsyncSession, user_id: int) -> int:
    """未读数。走 ``idx_site_message_user (user_id, is_read, created_at DESC)``。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(SiteMessage)
            .where(SiteMessage.user_id == user_id, SiteMessage.is_read.is_(False))
        )
        or 0
    )


async def mark_read(session: AsyncSession, user_id: int, msg_id: int) -> bool:
    """标记单条已读。返回是否命中（**别人的消息返回 False**，路由据此 404）。

    ★ ``WHERE`` 里**不加** ``is_read = false``：否则"重复标记同一条"会返回 0 行，
      路由就会把一条自己的、已经读过的消息报成 404。``read_at`` 用 coalesce
      保住**第一次**读的时间。
    """
    result = await session.execute(
        update(SiteMessage)
        .where(SiteMessage.id == msg_id, SiteMessage.user_id == user_id)
        .values(is_read=True, read_at=func.coalesce(SiteMessage.read_at, func.now()))
    )
    return result.rowcount > 0


async def mark_all_read(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        update(SiteMessage)
        .where(SiteMessage.user_id == user_id, SiteMessage.is_read.is_(False))
        .values(is_read=True, read_at=func.coalesce(SiteMessage.read_at, func.now()))
    )
    return int(result.rowcount or 0)
