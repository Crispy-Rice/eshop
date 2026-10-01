"""payment 模块的领域逻辑。**本期只有模拟渠道**。

链路（docs/09）：

```
发起支付 → 模拟渠道下单 → 用户「去支付」 → 模拟回调 → 推进订单 → 查单
```

**回调必须幂等** —— 这是支付模块最容易出错的地方。渠道在网络抖动、超时重试时
会**重复回调同一笔**，真实渠道甚至会有并发回调。挡不住的话订单被推进两次、
库存被扣两次、券被用两次。这里用两层：

1. ``cas_success`` 条件更新（``WHERE status = 待支付``）
2. ``mock_channel_trade.out_trade_no`` 唯一约束

再加最外层：整段逻辑跑在 ``get_session`` 的**一个事务**里，任何一步失败全部回滚。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.snowflake import next_id
from app.modules.core import outbox
from app.modules.payment import repository as repo
from app.modules.payment.models import (
    CHANNEL_MOCK,
    MAX_REFUND_RETRY,
    PAY_SUCCESS,
    PAY_WAIT,
    REFUND_WAIT,
    MockChannelTrade,
    Payment,
    PaymentRefund,
)
from app.modules.trade.models import ORDER_WAIT_PAY, OrderMain
from app.modules.trade.order_no import build_pay_no, build_refund_no

logger = get_logger(__name__)


def _assert_mock_enabled() -> None:
    """生产环境必须关掉模拟支付（config 的 ``_guard_production`` 也会拦一次）。

    这里是第二道：就算配置被误改成开启，接口层也不会真的放行 ——
    一道防线失效时另一道仍然成立。
    """
    if not get_settings().payment_mock_enabled:
        raise BizError(ErrorCode.PAY_CHANNEL_UNAVAILABLE, "模拟支付已关闭")


async def create_payment(
    session: AsyncSession, *, user_id: int, order_main_no: str
) -> Payment:
    """发起支付。**幂等**：同一母单重复调用返回同一张支付单。

    真实渠道下"发起支付"会拿到渠道的 prepay_id；模拟渠道只是落一条交易记录。
    重复发起时**复用**已有的支付单 —— 用户来回切页面不该产生多张支付单。
    """
    _assert_mock_enabled()

    order = await _get_payable_order(session, user_id, order_main_no)

    existing = await repo.get_by_order(session, order_main_no)
    if existing is not None:
        # 已支付/已关闭的支付单也直接返回，由前端决定展示什么
        return existing

    payment = await repo.insert(
        session,
        Payment(
            id=next_id(),
            pay_no=build_pay_no(next_id()),
            order_main_no=order_main_no,
            user_id=user_id,
            amount=order.payable_amount,
            channel=CHANNEL_MOCK,
            status=PAY_WAIT,
        ),
    )
    # 模拟渠道侧的"统一下单"。真实渠道这一步要调远程接口
    await repo.insert_mock_trade(
        session,
        MockChannelTrade(
            id=next_id(),
            out_trade_no=payment.pay_no,
            pay_no=payment.pay_no,
            amount=payment.amount,
            status=0,
        ),
    )
    logger.info(
        "发起支付", extra={"payNo": payment.pay_no, "orderMainNo": order_main_no}
    )
    return payment


async def mock_callback(
    session: AsyncSession, *, pay_no: str, trade_no: str | None = None
) -> Payment:
    """模拟渠道回调。**幂等**，重复调用只推进一次。

    调用方（payment router）把它包在一个事务里：任何一步抛出，支付单的条件更新
    也会一起回滚，不会出现"支付单已成功但订单没推进"的中间态。
    """
    _assert_mock_enabled()

    payment = await repo.get_by_pay_no(session, pay_no)
    if payment is None:
        raise BizError(ErrorCode.NOT_FOUND, "支付单不存在")

    # ★ 幂等出口：已成功就直接返回，不再推进订单
    if payment.status == PAY_SUCCESS:
        logger.info("支付回调重放，已忽略", extra={"payNo": pay_no})
        return payment
    if payment.status != PAY_WAIT:
        raise BizError(ErrorCode.PAYMENT_CLOSED, "支付单已关闭")

    channel_trade_no = trade_no or f"MOCK{next_id()}"

    # ① 渠道侧记账（out_trade_no 唯一 + 条件更新，双保险）
    await repo.mark_mock_trade_paid(session, pay_no, trade_no=channel_trade_no)

    # ② 支付单推进。并发回调时只有一个 rowcount=1
    if not await repo.cas_success(
        session,
        pay_no,
        channel_trade_no=channel_trade_no,
        paid_amount=payment.amount,
    ):
        # 没抢到 —— 说明另一个并发回调已经推进过了。重读后按已成功返回
        refreshed = await repo.get_by_pay_no(session, pay_no)
        if refreshed is not None and refreshed.status == PAY_SUCCESS:
            return refreshed
        raise BizError(ErrorCode.PAYMENT_CLOSED, "支付单状态异常")

    # ③ 推进订单。**与支付单在同一个事务里** —— 要么都成功，要么都回滚
    from app.modules.trade import service as trade_service  # 局部避免循环依赖

    if not await trade_service.mark_paid(
        session, payment.order_main_no, paid_amount=payment.amount
    ):
        # 订单在这期间被超时关单了。抛异常 → 整个事务回滚，支付单也退回待支付，
        # 不会留下"钱收了但订单已关闭"的烂账
        raise BizError(ErrorCode.PAYMENT_CLOSED, "订单已关闭，无法支付")

    await outbox.add(
        session,
        topic=outbox.TOPIC_ORDER_PAID,
        biz_key=f"PAID:{payment.order_main_no}",
        payload={
            "orderMainNo": payment.order_main_no,
            "payNo": pay_no,
            "userId": payment.user_id,
            "amount": payment.amount,
        },
    )

    logger.info(
        "支付成功",
        extra={"payNo": pay_no, "orderMainNo": payment.order_main_no},
    )
    return await repo.get_by_pay_no(session, pay_no)  # type: ignore[return-value]


async def get_payment(session: AsyncSession, *, user_id: int, pay_no: str) -> Payment:
    payment = await repo.get_by_pay_no(session, pay_no)
    if payment is None or int(payment.user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "支付单不存在")
    return payment


async def close_by_order(session: AsyncSession, order_main_no: str) -> None:
    """关单时把支付单一并关掉。由 ``trade.close_order`` 调用。

    **只关「待支付」的**：已支付的支付单不能关（钱已经收了，得走退款）。
    没建过支付单（用户没点过"去支付"）也什么都不做。
    """
    payment = await repo.get_by_order(session, order_main_no)
    if payment is None:
        return
    if await repo.cas_close(session, payment.pay_no):
        # 模拟渠道侧的交易记录保持原状：真实渠道这里要调关单接口，
        # 模拟渠道的"待支付"交易本身就等于已失效
        logger.info("支付单已关闭", extra={"payNo": payment.pay_no})


async def _get_payable_order(
    session: AsyncSession, user_id: int, order_main_no: str
) -> OrderMain:
    """取可支付的母单，并校验归属。"""
    from app.modules.trade import repository as trade_repo  # 局部避免循环依赖

    order = await trade_repo.get_main_for_user(session, order_main_no, user_id)
    if order is None:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")
    if int(order.status) != ORDER_WAIT_PAY:
        raise BizError(ErrorCode.ORDER_STATUS_INVALID, "订单当前状态不能支付")
    if int(order.payable_amount) <= 0:
        raise BizError(ErrorCode.PAY_AMOUNT_MISMATCH, "订单金额异常")
    return order


# ============================================================
# 资金退款（由 aftersale 模块驱动）
# ============================================================
# 说明：退款是**售后**的业务流程，本模块只负责"把一笔钱打回渠道"这件技术动作。
# 所以这里不 import aftersale —— 依赖是单向的（aftersale → payment），
# 由 aftersale 的 worker 任务把两边的动作编排在同一个事务里。


async def create_refund(
    session: AsyncSession, *, refund_biz_no: str, order_main_no: str, amount: int
) -> PaymentRefund:
    """建一张资金退款单，并在支付单上累加已退金额。

    ★ **必须与售后单的状态流转在同一个事务里** —— 否则会出现"售后说退款中、
    资金侧没有记录"或反之。``refund_biz_no``（= 售后单号）上的唯一约束保证
    一个售后单只能退一次钱，重放时直接返回已有那张。
    """
    _assert_mock_enabled()

    existing = await repo.get_refund_by_biz_no(session, refund_biz_no)
    if existing is not None:
        return existing

    if amount <= 0:
        raise BizError(ErrorCode.PAY_AMOUNT_MISMATCH, "退款金额必须大于 0")

    payment = await repo.get_by_order(session, order_main_no)
    if payment is None:
        raise BizError(ErrorCode.NOT_FOUND, "该订单没有支付记录")
    if int(payment.status) != PAY_SUCCESS:
        raise BizError(ErrorCode.PAYMENT_CLOSED, "支付单当前状态不可退款")

    # 超退兜底。失败说明"已退 + 本次 > 实付"，是资金安全问题，必须拒绝
    if not await repo.add_paid_refund_amount(session, payment.pay_no, amount=amount):
        raise BizError(
            ErrorCode.REFUND_AMOUNT_EXCEED,
            f"退款金额超过支付单可退额度（实付 {payment.paid_amount} 分，"
            f"已退 {payment.refunded_amount} 分）",
        )

    return await repo.insert_refund(
        session,
        PaymentRefund(
            id=next_id(),
            refund_no=build_refund_no(next_id()),
            refund_biz_no=refund_biz_no,
            pay_no=payment.pay_no,
            order_main_no=order_main_no,
            user_id=int(payment.user_id),
            amount=amount,
            channel=CHANNEL_MOCK,
            status=REFUND_WAIT,
        ),
    )


async def call_channel_refund(refund_no: str, amount: int) -> str:
    """向渠道发起退款，返回渠道退款单号。**模拟渠道**。

    ★ **不接收 session**：真实渠道下这是一次 HTTP 调用，绝不能发生在数据库事务里
    （会长时间持锁）。调用方必须先结束只读事务、拿到退款单数据再调这里。

    **按 ``refund_no`` 幂等** —— 重试时返回同一个渠道单号。真实渠道也一样：
    商户退款单号是它们的幂等键，重复请求不会重复打款。
    """
    _assert_mock_enabled()
    logger.info("模拟渠道退款", extra={"refundNo": refund_no, "amount": amount})
    return f"MOCKREFUND{refund_no}"


async def mark_refund_success(
    session: AsyncSession, refund_no: str, *, channel_refund_no: str
) -> bool:
    """把资金退款单置为成功。重复调用返回 False（已经成功过）。"""
    return await repo.cas_refund_success(session, refund_no, channel_refund_no=channel_refund_no)


async def mark_refund_failed(session: AsyncSession, refund_no: str, *, reason: str) -> None:
    """标记退款失败并安排下一次重试。

    退避：2^n 分钟，封顶 6 小时。超过 ``MAX_REFUND_RETRY`` 次不再自动重试，
    交给人工（``next_retry_at`` 推到很远的将来，扫描自然跳过）。
    """
    refund = await repo.get_refund_by_no(session, refund_no)
    if refund is None:
        return
    attempt = int(refund.retry_count) + 1
    if attempt >= MAX_REFUND_RETRY:
        next_retry = datetime.now(UTC) + timedelta(days=365)
        logger.error(
            "资金退款重试次数耗尽，转人工处理",
            extra={"refundNo": refund_no, "retryCount": attempt, "reason": reason},
        )
    else:
        minutes = min(2**attempt, 6 * 60)
        next_retry = datetime.now(UTC) + timedelta(minutes=minutes)
    await repo.cas_refund_failed(session, refund_no, reason=reason, next_retry_at=next_retry)


async def list_retryable_refunds(session: AsyncSession, *, limit: int = 200) -> list[str]:
    """该重试的资金退款单号。给 worker 的兜底扫描用。"""
    rows = await repo.list_retryable_refunds(session, limit=limit)
    return [r.refund_no for r in rows]
