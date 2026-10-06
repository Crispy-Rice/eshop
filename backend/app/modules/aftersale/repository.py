"""aftersale 模块的数据访问层。

只读写 ``aftersale`` schema。

**状态变更一律走 ``service`` 里的状态机**，这里只提供 CAS 原语 ——
``cas_status`` 是状态机三层保护里的最后一层（``WHERE status = :from``）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.aftersale.models import ACTIVE_STATUSES, RefundItem, RefundOrder


async def get_by_no(session: AsyncSession, refund_no: str) -> RefundOrder | None:
    return await session.scalar(select(RefundOrder).where(RefundOrder.refund_no == refund_no))


async def get_for_update(session: AsyncSession, refund_no: str) -> RefundOrder | None:
    """加**行锁**读售后单。状态流转的第一步（与 trade 的子单同一套做法）。"""
    return await session.scalar(
        select(RefundOrder).where(RefundOrder.refund_no == refund_no).with_for_update()
    )


async def get_by_request(
    session: AsyncSession, user_id: int, request_id: str
) -> RefundOrder | None:
    """按幂等键找已提交的申请。重放时直接返回原单。"""
    return await session.scalar(
        select(RefundOrder).where(
            RefundOrder.user_id == user_id, RefundOrder.request_id == request_id
        )
    )


async def get_active_by_sub(session: AsyncSession, order_sub_no: str) -> RefundOrder | None:
    """该子单正在进行的售后。用于申请前的友好提示（真正的防线是唯一索引）。"""
    return await session.scalar(
        select(RefundOrder).where(
            RefundOrder.order_sub_no == order_sub_no,
            RefundOrder.status.in_(ACTIVE_STATUSES),
        )
    )


async def insert_order(session: AsyncSession, order: RefundOrder) -> RefundOrder:
    session.add(order)
    await session.flush()
    return order


async def count_user_open_refunds(session: AsyncSession, user_id: int) -> int:
    """该用户还有多少笔进行中的售后。账号注销的前置守卫用（见 service）。"""
    return (
        await session.scalar(
            select(func.count())
            .select_from(RefundOrder)
            .where(RefundOrder.user_id == user_id, RefundOrder.status.in_(ACTIVE_STATUSES))
        )
    ) or 0


async def insert_items(session: AsyncSession, items: list[RefundItem]) -> None:
    session.add_all(items)
    await session.flush()


async def cas_status(
    session: AsyncSession,
    refund_no: str,
    *,
    from_status: int,
    to_status: int,
    values: dict | None = None,
) -> bool:
    """CAS 更新售后单状态。``rowcount = 0`` 说明状态已被别人改过（并发或超时任务抢了）。"""
    payload: dict = {
        "status": to_status,
        "updated_at": func.now(),
        "version": RefundOrder.version + 1,
    }
    if values:
        payload.update(values)
    result = await session.execute(
        update(RefundOrder)
        .where(RefundOrder.refund_no == refund_no, RefundOrder.status == from_status)
        .values(**payload)
    )
    return result.rowcount > 0


async def update_fields(session: AsyncSession, refund_no: str, values: dict) -> None:
    """更新**非状态**字段（备注、退货单号、各环节时间等）。

    状态必须走 ``cas_status``，不走这里。
    """
    await session.execute(
        update(RefundOrder)
        .where(RefundOrder.refund_no == refund_no)
        .values(updated_at=func.now(), version=RefundOrder.version + 1, **values)
    )


async def list_items(session: AsyncSession, refund_no: str) -> list[RefundItem]:
    return list(
        await session.scalars(
            select(RefundItem).where(RefundItem.refund_no == refund_no).order_by(RefundItem.id)
        )
    )


async def list_items_of_orders(
    session: AsyncSession, refund_nos: Sequence[str]
) -> list[RefundItem]:
    """批量取多个售后单的明细 —— 列表页避免 N+1。"""
    if not refund_nos:
        return []
    return list(
        await session.scalars(
            select(RefundItem).where(RefundItem.refund_no.in_(refund_nos)).order_by(RefundItem.id)
        )
    )


async def mark_items_restored(session: AsyncSession, refund_no: str) -> None:
    """标记明细的库存已回补。**仅用于排查**，幂等其实由 inventory.stock_biz_key 保证。"""
    await session.execute(
        update(RefundItem)
        .where(RefundItem.refund_no == refund_no)
        .values(stock_restored=True, restore_time=func.now())
    )


# ============================================================
# 列表（键集游标分页）
# ============================================================
async def list_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    status: int | None = None,
    cursor: tuple[datetime, int] | None = None,
    limit: int = 10,
) -> list[RefundOrder]:
    """我的售后。**键集游标分页**，不用 OFFSET（越翻越慢）。"""
    stmt: Select = select(RefundOrder).where(RefundOrder.user_id == user_id)
    if status is not None:
        stmt = stmt.where(RefundOrder.status == status)
    if cursor is not None:
        last_time, last_id = cursor
        stmt = stmt.where(
            (RefundOrder.created_at < last_time)
            | ((RefundOrder.created_at == last_time) & (RefundOrder.id < last_id))
        )
    stmt = stmt.order_by(RefundOrder.created_at.desc(), RefundOrder.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def list_for_shop(
    session: AsyncSession,
    shop_id: int,
    *,
    status: int | None = None,
    statuses: Sequence[int] | None = None,
    cursor: tuple[datetime, int] | None = None,
    limit: int = 20,
) -> list[RefundOrder]:
    """商家售后列表。走 ``idx_refund_shop_status``。"""
    stmt: Select = select(RefundOrder).where(RefundOrder.shop_id == shop_id)
    if status is not None:
        stmt = stmt.where(RefundOrder.status == status)
    if statuses:
        stmt = stmt.where(RefundOrder.status.in_(statuses))
    if cursor is not None:
        last_time, last_id = cursor
        stmt = stmt.where(
            (RefundOrder.created_at < last_time)
            | ((RefundOrder.created_at == last_time) & (RefundOrder.id < last_id))
        )
    stmt = stmt.order_by(RefundOrder.created_at.desc(), RefundOrder.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def list_timeout(
    session: AsyncSession, *, now: datetime, limit: int = 200
) -> list[str]:
    """到点该处理的售后单号。命中部分索引 ``idx_refund_deadline``。

    只扫"还带 deadline 的进行中状态"—— 终态没有 deadline，自然不会被选出来。
    """
    return list(
        await session.scalars(
            select(RefundOrder.refund_no)
            .where(RefundOrder.deadline.isnot(None), RefundOrder.deadline < now)
            .order_by(RefundOrder.deadline)
            .limit(limit)
        )
    )


async def count_pending_for_shop(session: AsyncSession, shop_id: int) -> int:
    """待商家处理的售后数（待审核 + 待收货 + 质检中）。商家后台的角标。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(RefundOrder)
            .where(RefundOrder.shop_id == shop_id, RefundOrder.status.in_((10, 40, 50)))
        )
        or 0
    )
