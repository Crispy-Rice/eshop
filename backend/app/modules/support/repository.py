"""support 模块的数据访问。纯函数，收调用方的 session，返回 ORM 行。

★ **每个按归属的写操作都必须带归属条件**（``user_id`` 或 ``shop_id``）——
  与 ``cart/repository.py`` 同一条纪律：这是防越权的唯一手段。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.support.models import (
    SENDER_SYSTEM,
    SENDER_USER,
    TICKET_ACTIVE_WHERE,
    TICKET_CLOSED,
    TICKET_OPEN,
    Ticket,
    TicketMessage,
    TicketStateFlow,
)

# 时间上的"永远之前"。游标为空时用它兜底 —— 见 unread_of 的注释
NEVER = literal_column("'-infinity'::timestamptz")

# 列表默认排序：最近有消息的在最前
_ORDER = (Ticket.last_message_at.desc(), Ticket.id.desc())


async def insert_ticket_ignore_conflict(
    session: AsyncSession,
    *,
    ticket_id: int,
    ticket_no: str,
    user_id: int,
    shop_id: int,
    subject: str,
    source: int,
    order_main_no: str | None,
    order_sub_no: str | None,
    refund_no: str | None,
    now: datetime,
) -> bool:
    """开一条会话；**该用户对同一个对象已有进行中的会话时静默不写**。

    返回是否真的插入了。

    ★ 用 ``ON CONFLICT`` 而不是"先查再插"：并发下两个请求都会查不到，
      然后一个撞唯一索引报错。冲突目标必须**带上部分索引的 WHERE**，
      否则 PG 认不出 ``uk_ticket_user_shop_active`` 这个部分唯一索引。

    ``ticket_id`` 与 ``ticket_no`` 必须来自**同一个**雪花 id（调用方保证）：
      会话号里只保留了雪花的后 10 位，从号里反推不出原 id。
    """
    stmt = (
        pg_insert(Ticket)
        .values(
            id=ticket_id,
            ticket_no=ticket_no,
            user_id=user_id,
            shop_id=shop_id,
            subject=subject,
            source=source,
            order_main_no=order_main_no,
            order_sub_no=order_sub_no,
            refund_no=refund_no,
            status=TICKET_OPEN,
            last_message_at=now,
            # 系统 = 还没有人说过话（见 models 的注释）
            last_sender_type=SENDER_SYSTEM,
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "shop_id"],
            index_where=text(TICKET_ACTIVE_WHERE),
        )
    )
    result = await session.execute(stmt)
    return result.rowcount == 1


async def get_by_no(session: AsyncSession, ticket_no: str) -> Ticket | None:
    return await session.scalar(select(Ticket).where(Ticket.ticket_no == ticket_no))


async def get_for_update(session: AsyncSession, ticket_no: str) -> Ticket | None:
    """行锁。回复与关闭都要先拿它 —— 否则并发下 ``last_sender_type`` 会写错。"""
    return await session.scalar(
        select(Ticket).where(Ticket.ticket_no == ticket_no).with_for_update()
    )


async def get_active(session: AsyncSession, *, user_id: int, shop_id: int) -> Ticket | None:
    return await session.scalar(
        select(Ticket).where(
            Ticket.user_id == user_id,
            Ticket.shop_id == shop_id,
            text(TICKET_ACTIVE_WHERE),
        )
    )


def _apply_filters(
    stmt: Select[Any],
    *,
    user_id: int | None,
    shop_id: int | None,
    status: int | None,
) -> Select[Any]:
    if user_id is not None:
        stmt = stmt.where(Ticket.user_id == user_id)
    if shop_id is not None:
        stmt = stmt.where(Ticket.shop_id == shop_id)
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    return stmt


async def list_tickets(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    shop_id: int | None = None,
    status: int | None = None,
    owes_reply_only: bool = False,
    cursor: tuple[datetime, int] | None = None,
    limit: int,
) -> list[Ticket]:
    """三个受众共用一份列表查询，差别只在过滤条件。

    ★ ``owes_reply_only``（后台队列的「待回复」）**必须在这里过滤，不能拿到
      Python 侧筛**：分页是先取 ``limit + 1`` 再判 ``has_more``，筛在分页之后
      会让"这一页 20 条里只有 3 条命中"而 ``has_more`` 仍为真 —— 页大小就废了。
      它与 ``rules.staff_owes_reply`` 是同一条判定，改一处要改两处（有集成测试兜着）。
    """
    stmt = _apply_filters(
        select(Ticket), user_id=user_id, shop_id=shop_id, status=status
    )
    if owes_reply_only:
        stmt = stmt.where(Ticket.status == TICKET_OPEN, Ticket.last_sender_type == SENDER_USER)
    if cursor is not None:
        last_at, last_id = cursor
        stmt = stmt.where(
            (Ticket.last_message_at < last_at)
            | ((Ticket.last_message_at == last_at) & (Ticket.id < last_id))
        )
    stmt = stmt.order_by(*_ORDER).limit(limit + 1)
    return list((await session.scalars(stmt)).all())


async def touch_after_message(
    session: AsyncSession, ticket_no: str, *, sender_type: int, now: datetime
) -> None:
    """发消息后更新排序键与"最后是谁说的"。"""
    await session.execute(
        update(Ticket)
        .where(Ticket.ticket_no == ticket_no)
        .values(last_message_at=now, last_sender_type=sender_type, updated_at=now)
    )


async def cas_close(
    session: AsyncSession,
    ticket_no: str,
    *,
    from_status: int,
    close_by: int,
    reason: str | None,
    now: datetime,
) -> bool:
    """关闭（CAS）。``rowcount == 0`` 说明状态已经不是 ``from_status`` 了。"""
    result = await session.execute(
        update(Ticket)
        .where(Ticket.ticket_no == ticket_no, Ticket.status == from_status)
        .values(
            status=TICKET_CLOSED,
            close_by=close_by,
            close_reason=reason,
            close_time=now,
            updated_at=now,
        )
    )
    return result.rowcount > 0


async def reopen(session: AsyncSession, ticket_no: str, *, now: datetime) -> None:
    """重开：清掉关闭信息，回到进行中。"""
    await session.execute(
        update(Ticket)
        .where(Ticket.ticket_no == ticket_no)
        .values(
            status=TICKET_OPEN,
            close_by=None,
            close_reason=None,
            close_time=None,
            updated_at=now,
        )
    )


async def set_user_read(session: AsyncSession, ticket_no: str, *, now: datetime) -> None:
    await session.execute(
        update(Ticket).where(Ticket.ticket_no == ticket_no).values(user_read_at=now)
    )


async def set_staff_read(session: AsyncSession, ticket_no: str, *, now: datetime) -> None:
    await session.execute(
        update(Ticket).where(Ticket.ticket_no == ticket_no).values(staff_read_at=now)
    )


async def insert_message(
    session: AsyncSession,
    *,
    ticket_no: str,
    sender_type: int,
    sender_id: int | None,
    body: str,
    images: Sequence[str],
) -> TicketMessage:
    message = TicketMessage(
        ticket_no=ticket_no,
        sender_type=sender_type,
        sender_id=sender_id,
        body=body,
        images=list(images),
    )
    session.add(message)
    await session.flush()
    return message


async def list_messages(
    session: AsyncSession, ticket_no: str, *, before_id: int | None, limit: int
) -> list[TicketMessage]:
    """倒序取一页（多取一条让调用方算 has_more），再翻成**正序**返回。

    正序是给前端直接渲染的：消息流的自然顺序是从旧到新。
    """
    stmt = select(TicketMessage).where(TicketMessage.ticket_no == ticket_no)
    if before_id is not None:
        stmt = stmt.where(TicketMessage.id < before_id)
    stmt = stmt.order_by(TicketMessage.id.desc()).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    rows.reverse()
    return rows


async def unread_of(
    session: AsyncSession, ticket_nos: Sequence[str], *, mine_is_buyer: bool
) -> dict[str, int]:
    """各会话的未读数 —— **一次分组查询**，不是每个会话查一次。

    ★ 游标是**行上的值**，没法当成一个全局参数传进来；但这正是 SQL 擅长的：
      两张表 join 之后逐行比 ``m.created_at > t.user_read_at``。
      没读过（游标为 NULL）时用 ``-infinity`` 兜底 —— 否则 ``> NULL`` 求值为
      NULL（假），一条都没读过的会话反而会被算成"零条未读"。
    """
    if not ticket_nos:
        return {}
    cursor_col = Ticket.user_read_at if mine_is_buyer else Ticket.staff_read_at
    sender_filter = (
        TicketMessage.sender_type != SENDER_USER
        if mine_is_buyer
        else TicketMessage.sender_type == SENDER_USER
    )
    stmt = (
        select(TicketMessage.ticket_no, func.count())
        .select_from(TicketMessage)
        .join(Ticket, Ticket.ticket_no == TicketMessage.ticket_no)
        .where(
            TicketMessage.ticket_no.in_(list(ticket_nos)),
            sender_filter,
            TicketMessage.created_at > func.coalesce(cursor_col, NEVER),
        )
        .group_by(TicketMessage.ticket_no)
    )
    rows = await session.execute(stmt)
    return {no: int(count) for no, count in rows.all()}


async def count_owes_reply(session: AsyncSession, *, shop_id: int | None) -> int:
    """「待回复」的条数 —— 后台导航角标用。

    ★ 列表接口是**游标分页**、不带总数，所以角标不能靠"取一页数 items.length"来凑
      （超过一页就会少报，而那正是最需要提醒的时候）。这里单独 count。
      判定与 ``list_tickets(owes_reply_only=True)`` 必须一致：进行中 + 最后一条是买家。
    """
    stmt = select(func.count()).select_from(Ticket).where(
        Ticket.status == TICKET_OPEN, Ticket.last_sender_type == SENDER_USER
    )
    if shop_id is not None:
        stmt = stmt.where(Ticket.shop_id == shop_id)
    return int(await session.scalar(stmt) or 0)


async def insert_state_flow(
    session: AsyncSession,
    *,
    ticket_no: str,
    from_status: int,
    to_status: int,
    event: str,
    operator_type: int,
    operator_id: int | None,
    remark: str | None = None,
) -> None:
    session.add(
        TicketStateFlow(
            ticket_no=ticket_no,
            from_status=from_status,
            to_status=to_status,
            event=event,
            operator_type=operator_type,
            operator_id=None if operator_id is None else str(operator_id),
            remark=remark,
        )
    )
