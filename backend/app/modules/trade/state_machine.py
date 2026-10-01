"""子单状态机（docs/07 §4）。

**表驱动，不是 if-else**。理由：状态和事件的组合是有限的、可穷举的，
写成表之后"哪些流转合法"一目了然，单测也能穷举验证没有漏网的路径；
而 if-else 散在各处时，没人说得清完整的状态图。

**所有状态变更必须走 ``service.transit()``**，禁止任何地方直接
``UPDATE trade.order_sub SET status = ...``（code review 与 grep 检查）。
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from app.core.errors import BizError, ErrorCode
from app.modules.trade.models import (
    ORDER_CLOSED,
    ORDER_FINISHED,
    ORDER_REFUNDED,
    ORDER_REFUNDING,
    ORDER_WAIT_DELIVER,
    ORDER_WAIT_PAY,
    ORDER_WAIT_RECEIVE,
    STATUS_TEXT,
)


class SubOrderStatus(IntEnum):
    WAIT_PAY = ORDER_WAIT_PAY
    WAIT_DELIVER = ORDER_WAIT_DELIVER
    WAIT_RECEIVE = ORDER_WAIT_RECEIVE
    FINISHED = ORDER_FINISHED
    CLOSED = ORDER_CLOSED
    REFUNDING = ORDER_REFUNDING
    REFUNDED = ORDER_REFUNDED


class OrderEvent(StrEnum):
    PAY_SUCCESS = "PAY_SUCCESS"
    USER_CANCEL = "USER_CANCEL"
    TIMEOUT_CANCEL = "TIMEOUT_CANCEL"
    SHIP = "SHIP"
    PARTIAL_SHIP = "PARTIAL_SHIP"
    CONFIRM_RECEIVE = "CONFIRM_RECEIVE"
    AUTO_RECEIVE = "AUTO_RECEIVE"
    APPLY_REFUND = "APPLY_REFUND"
    APPLY_AFTERSALE = "APPLY_AFTERSALE"
    REFUND_SUCCESS = "REFUND_SUCCESS"
    REFUND_REJECT = "REFUND_REJECT"
    USER_REVOKE = "USER_REVOKE"


S, E = SubOrderStatus, OrderEvent

# ★ 状态机定义：当前状态 → 允许的事件 → 目标状态
#
# ``None`` 表示"恢复到售后单记录的 source_status"——因为 20→60 和 30→60
# 在退款被拒后应该回到**各自**的原状态，单看目标状态表达不了。
# 调用方通过 TransitContext.restore_to 传入（docs/07 §4.3）。
TRANSITIONS: dict[SubOrderStatus, dict[OrderEvent, SubOrderStatus | None]] = {
    S.WAIT_PAY: {
        E.PAY_SUCCESS: S.WAIT_DELIVER,
        E.USER_CANCEL: S.CLOSED,
        E.TIMEOUT_CANCEL: S.CLOSED,
    },
    S.WAIT_DELIVER: {
        E.SHIP: S.WAIT_RECEIVE,
        E.PARTIAL_SHIP: S.WAIT_DELIVER,  # 部分发货，状态不变
        E.APPLY_REFUND: S.REFUNDING,
    },
    S.WAIT_RECEIVE: {
        E.CONFIRM_RECEIVE: S.FINISHED,
        E.AUTO_RECEIVE: S.FINISHED,
        E.APPLY_REFUND: S.REFUNDING,
    },
    S.FINISHED: {
        E.APPLY_AFTERSALE: S.REFUNDING,
    },
    S.REFUNDING: {
        E.REFUND_SUCCESS: S.REFUNDED,
        E.REFUND_REJECT: None,  # 拒绝后退回原状态
        E.USER_REVOKE: None,  # 撤销后退回原状态
    },
    # 终态没有出边
    S.CLOSED: {},
    S.REFUNDED: {},
}


def can_transit(from_status: SubOrderStatus, event: OrderEvent) -> bool:
    """这个流转是否合法。给调用方做预检用（真正的防线在 transit 里）。"""
    return event in TRANSITIONS.get(from_status, {})


def next_status(
    from_status: SubOrderStatus,
    event: OrderEvent,
    *,
    restore_to: SubOrderStatus | None = None,
) -> SubOrderStatus:
    """算出目标状态。非法流转抛业务异常。

    ``restore_to`` 只在 ``REFUND_REJECT`` / ``USER_REVOKE`` 这类"回到原状态"
    的事件上需要 —— 由调用方从售后单的 ``source_status`` 读出来传入。
    """
    allowed = TRANSITIONS.get(from_status, {})
    if event not in allowed:
        raise BizError(
            ErrorCode.ORDER_STATUS_INVALID,
            f"订单当前是「{STATUS_TEXT.get(int(from_status), from_status)}」，不能执行该操作",
        )

    target = allowed[event]
    if target is None:
        if restore_to is None:
            raise BizError(
                ErrorCode.ORDER_STATUS_INVALID,
                "该操作需要知道要恢复到哪个状态（售后单的 source_status）",
            )
        return restore_to
    return target


def aggregate_main_status(statuses: set[SubOrderStatus]) -> SubOrderStatus:
    """母单状态 = 子单状态的聚合（docs/07 §5）。

    规则：
    - 全部同状态 → 就是这个状态（含全完成/全关闭/全退款）
    - 混合 → 按**最落后**的状态展示。用户视角是"还有事情没做完"，
      所以 待付款 > 待发货 > 待收货 > 退款中 > 已完成 依次优先
    - 只剩 已关闭 + 已退款 的组合时，优先显示已退款

    "部分退款"的展示（"1 件商品已退款"）是**展示层的组合**，
    不改变母单的单一状态字段。
    """
    if not statuses:
        raise ValueError("子单状态集合不能为空")
    if len(statuses) == 1:
        return next(iter(statuses))

    for status in (
        S.WAIT_PAY,
        S.WAIT_DELIVER,
        S.WAIT_RECEIVE,
        S.REFUNDING,
        S.FINISHED,
    ):
        if status in statuses:
            return status

    # 只剩 CLOSED 与 REFUNDED
    return S.REFUNDED if S.REFUNDED in statuses else S.CLOSED


def derive_pay_status(
    total_paid: int, total_refunded: int, *, sub_count: int, refunded_subs: int
) -> int:
    """由金额与子单情况推出母单的 ``pay_status``。

    它和履约状态是**正交**的（docs/07 §5）：子单可以一半已退款一半待收货，
    履约状态显示"待收货"，而支付状态显示"部分退款"。
    """
    from app.modules.trade.models import PAY_FULL_REFUND, PAY_PAID, PAY_PARTIAL_REFUND, PAY_UNPAID

    if total_paid <= 0:
        return PAY_UNPAID
    if refunded_subs >= sub_count and total_refunded >= total_paid:
        return PAY_FULL_REFUND
    if total_refunded > 0:
        return PAY_PARTIAL_REFUND
    return PAY_PAID
