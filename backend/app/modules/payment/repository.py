"""payment 模块的数据访问层。

只读写 ``payment`` schema。**跨 schema 不建外键**（docs/13 §0.1），
所以这里看不到对 ``trade.order_main`` 的引用 —— 归属关系由 service 校验。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payment.models import (
    PAY_CLOSED,
    PAY_REFUNDED,
    PAY_SUCCESS,
    PAY_WAIT,
    MockChannelTrade,
    Payment,
    PaymentRefund,
)


async def get_by_pay_no(session: AsyncSession, pay_no: str) -> Payment | None:
    return await session.scalar(select(Payment).where(Payment.pay_no == pay_no))


async def get_by_order(session: AsyncSession, order_main_no: str) -> Payment | None:
    """一个母单只有一张支付单（``UNIQUE (order_main_no)``）。"""
    return await session.scalar(
        select(Payment).where(Payment.order_main_no == order_main_no)
    )


async def insert(session: AsyncSession, payment: Payment) -> Payment:
    session.add(payment)
    await session.flush()
    return payment


async def cas_success(
    session: AsyncSession, pay_no: str, *, channel_trade_no: str, paid_amount: int
) -> bool:
    """CAS 把支付单推到「已支付」。

    ★ **回调幂等就靠这里**：条件是 ``status = 待支付``。渠道重复回调时
    ``rowcount = 0``，调用方直接返回当前状态，不会重复推进订单。
    并发回调也只有一个能改成功。
    """
    result = await session.execute(
        update(Payment)
        .where(Payment.pay_no == pay_no, Payment.status == PAY_WAIT)
        .values(
            status=PAY_SUCCESS,
            paid_amount=paid_amount,
            channel_trade_no=channel_trade_no,
            pay_time=func.now(),
            updated_at=func.now(),
            version=Payment.version + 1,
        )
    )
    return result.rowcount > 0


async def cas_close(session: AsyncSession, pay_no: str) -> bool:
    """关闭支付单。只关「待支付」的 —— 已支付的不能关（钱已经收了）。"""
    result = await session.execute(
        update(Payment)
        .where(Payment.pay_no == pay_no, Payment.status == PAY_WAIT)
        .values(
            status=PAY_CLOSED,
            close_time=func.now(),
            updated_at=func.now(),
            version=Payment.version + 1,
        )
    )
    return result.rowcount > 0


async def get_mock_trade(session: AsyncSession, out_trade_no: str) -> MockChannelTrade | None:
    return await session.scalar(
        select(MockChannelTrade).where(MockChannelTrade.out_trade_no == out_trade_no)
    )


async def insert_mock_trade(session: AsyncSession, trade: MockChannelTrade) -> MockChannelTrade:
    session.add(trade)
    await session.flush()
    return trade


async def mark_mock_trade_paid(
    session: AsyncSession, out_trade_no: str, *, trade_no: str
) -> bool:
    """模拟渠道侧记账。同样用 CAS，重复回调只生效一次。"""
    result = await session.execute(
        update(MockChannelTrade)
        .where(MockChannelTrade.out_trade_no == out_trade_no, MockChannelTrade.status == 0)
        .values(status=1, trade_no=trade_no, paid_at=func.now())
    )
    return result.rowcount > 0


# ============================================================
# 资金退款单
# ============================================================
async def get_refund_by_no(session: AsyncSession, refund_no: str) -> PaymentRefund | None:
    return await session.scalar(
        select(PaymentRefund).where(PaymentRefund.refund_no == refund_no)
    )


async def get_refund_by_biz_no(
    session: AsyncSession, refund_biz_no: str
) -> PaymentRefund | None:
    """按业务退款号取。售后单号就是业务退款号 —— 一个售后单只能退一次钱。"""
    return await session.scalar(
        select(PaymentRefund).where(PaymentRefund.refund_biz_no == refund_biz_no)
    )


async def insert_refund(session: AsyncSession, refund: PaymentRefund) -> PaymentRefund:
    session.add(refund)
    await session.flush()
    return refund


async def add_paid_refund_amount(
    session: AsyncSession, pay_no: str, *, amount: int
) -> bool:
    """在支付单上累加已退金额。**这是防超退的账本**。

    ``status`` 一并条件更新成「已退款」—— 只有当这次退完恰好把实付退光时。
    真正拦住超退的是 ``refunded_amount + :amount <= paid_amount`` 这个条件
    （以及表上的 CHECK 约束）；``status = 已支付`` 只是顺手过滤掉已关闭的支付单。
    """
    changed = await session.scalar(
        update(Payment)
        .where(
            Payment.pay_no == pay_no,
            Payment.status == PAY_SUCCESS,
            Payment.refunded_amount + amount <= Payment.paid_amount,
        )
        .values(
            refunded_amount=Payment.refunded_amount + amount,
            status=PAY_REFUNDED,
            updated_at=func.now(),
            version=Payment.version + 1,
        )
        .returning(Payment.pay_no)
    )
    return changed is not None


async def cas_refund_success(
    session: AsyncSession, refund_no: str, *, channel_refund_no: str
) -> bool:
    """CAS 把资金退款单推到「退款成功」。重复回调时 ``rowcount = 0``。"""
    result = await session.execute(
        update(PaymentRefund)
        .where(PaymentRefund.refund_no == refund_no, PaymentRefund.status != 2)
        .values(
            status=2,
            channel_refund_no=channel_refund_no,
            success_time=func.now(),
            updated_at=func.now(),
            version=PaymentRefund.version + 1,
        )
    )
    return result.rowcount > 0


async def cas_refund_failed(
    session: AsyncSession, refund_no: str, *, reason: str, next_retry_at: datetime
) -> bool:
    """标记退款失败并排下一次重试。``retry_count`` 同时 +1。"""
    result = await session.execute(
        update(PaymentRefund)
        .where(PaymentRefund.refund_no == refund_no, PaymentRefund.status != 2)
        .values(
            status=3,
            fail_reason=reason[:255],
            retry_count=PaymentRefund.retry_count + 1,
            next_retry_at=next_retry_at,
            updated_at=func.now(),
            version=PaymentRefund.version + 1,
        )
    )
    return result.rowcount > 0


async def list_retryable_refunds(
    session: AsyncSession, *, limit: int = 200
) -> list[PaymentRefund]:
    """该重试的资金退款单。命中部分索引 ``idx_payment_refund_retry``。

    包含「待退款（0）」与「退款失败（3）」两种 —— 前者可能是即时投递丢了
    （进程在提交后、投递前崩溃），后者是渠道明确失败的。
    """
    return list(
        await session.scalars(
            select(PaymentRefund)
            .where(PaymentRefund.status.in_((0, 3)), PaymentRefund.next_retry_at <= func.now())
            .order_by(PaymentRefund.next_retry_at)
            .limit(limit)
        )
    )
