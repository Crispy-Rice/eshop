"""售后单状态机（docs/08 §3）。

**表驱动，不是 if-else** —— 理由与 trade 的子单状态机相同：状态与事件的组合有限
且可穷举，写成表之后"哪些流转合法"一目了然，单测能穷举验证没有漏网的路径。

状态枚举直接复用 ``app.core.enums.RefundStatus``（10/11/20/30/40/50/51/60/70/80/81/90），
不在这里重复定义一遍数字。

**与子单状态机的联动**在 ``apply_event_for`` / ``refund_type_for`` 里 —— 申请售后时
必须根据子单当前状态决定"用哪个交易事件""强制成哪种售后类型"，这是两套状态机之间
唯一的桥。三条边汇入子单的 60 退款中：

    子单 20 待发货 ──APPLY_REFUND────▶ 60      售后类型强制 = 仅退款
    子单 30 待收货 ──APPLY_REFUND────▶ 60      售后类型强制 = 退货退款
    子单 40 已完成 ──APPLY_AFTERSALE─▶ 60      售后类型强制 = 退货退款

**售后类型由系统推导，不信客户端** —— docs/08 §3.4 明确：已发货后用户选"仅退款"
要强制转成退货退款，否则会出现"用户拿着货还直接退钱"的口子。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.enums import RefundStatus
from app.core.errors import BizError, ErrorCode
from app.modules.aftersale.models import REFUND_ONLY, REFUND_STATUS_TEXT, RETURN_REFUND

S = RefundStatus


class RefundEvent(StrEnum):
    """触发售后单状态流转的事件。"""

    # 商家审核（人工与超时分成两个事件，流水里能区分是谁触发的）
    APPROVE_REFUND = "APPROVE_REFUND"
    APPROVE_RETURN = "APPROVE_RETURN"
    AUTO_APPROVE_REFUND = "AUTO_APPROVE_REFUND"
    AUTO_APPROVE_RETURN = "AUTO_APPROVE_RETURN"
    MERCHANT_REJECT = "MERCHANT_REJECT"

    # 退款
    START_REFUND = "START_REFUND"
    REFUND_SUCCESS = "REFUND_SUCCESS"

    # 退货物流
    FILL_RETURN_EXPRESS = "FILL_RETURN_EXPRESS"
    MERCHANT_RECEIVE = "MERCHANT_RECEIVE"
    AUTO_RECEIVE = "AUTO_RECEIVE"

    # 质检。与 QUALITY_FAIL 成对，不改名成 ACCEPTED ——
    # 两个 noqa 是 ruff S105 的误报（它把 _PASS 当成 password）
    QUALITY_PASS = "QUALITY_PASS"  # noqa: S105
    AUTO_QUALITY_PASS = "AUTO_QUALITY_PASS"  # noqa: S105
    QUALITY_FAIL = "QUALITY_FAIL"

    # 超时与撤销
    TIMEOUT_CLOSE = "TIMEOUT_CLOSE"
    USER_REVOKE = "USER_REVOKE"


E = RefundEvent

# ★ 售后单状态机。每条边的目标都是**具体状态**（不像 trade 需要恢复原状态）——
#   "回到原状态"是子单的事，由 trade 的 restore_to 机制负责。
TRANSITIONS: dict[RefundStatus, dict[RefundEvent, RefundStatus]] = {
    # 10 待商家审核
    S.APPLYING: {
        E.APPROVE_REFUND: S.WAIT_REFUND,  # 商家同意，仅退款
        E.APPROVE_RETURN: S.WAIT_RETURN,  # 商家同意，退货退款
        E.AUTO_APPROVE_REFUND: S.WAIT_REFUND,  # 48h 未处理，系统同意
        E.AUTO_APPROVE_RETURN: S.WAIT_RETURN,
        E.MERCHANT_REJECT: S.MERCHANT_REJECTED,
        E.USER_REVOKE: S.USER_REVOKED,
    },
    # 20 待退款：仅退款分支的**瞬态** —— 同意后同一个事务里立刻 START_REFUND，
    # 不落盘停留。保留它是为了让审计链"已同意 ≠ 已发起退款"语义完整
    S.WAIT_REFUND: {
        E.START_REFUND: S.REFUNDING,
        E.USER_REVOKE: S.USER_REVOKED,
    },
    # 30 待买家寄回
    S.WAIT_RETURN: {
        E.FILL_RETURN_EXPRESS: S.WAIT_RECEIVE,
        E.TIMEOUT_CLOSE: S.CLOSED,  # 用户 7 天不寄回 → 关闭
        E.USER_REVOKE: S.USER_REVOKED,
    },
    # 40 待商家收货
    S.WAIT_RECEIVE: {
        E.MERCHANT_RECEIVE: S.QUALITY_CHECKING,
        E.AUTO_RECEIVE: S.QUALITY_CHECKING,  # 商家 7 天不收货 → 自动签收
    },
    # 50 质检中
    S.QUALITY_CHECKING: {
        E.QUALITY_PASS: S.REFUNDING,
        E.AUTO_QUALITY_PASS: S.REFUNDING,  # 质检 48h 未提交 → 自动通过
        E.QUALITY_FAIL: S.QUALITY_FAILED,
    },
    # 60 退款中：只等渠道结果，不允许撤销或拒绝（钱已经在路上了）
    S.REFUNDING: {
        E.REFUND_SUCCESS: S.SUCCESS,
    },
    # ---- 终态：没有出边 ----
    S.MERCHANT_REJECTED: {},
    S.QUALITY_FAILED: {},
    S.SUCCESS: {},
    S.CLOSED: {},
    S.USER_REVOKED: {},
    # 平台介入本期不做，保留状态但没有任何入边 —— 永远不可达
    S.PLATFORM_INTERVENING: {},
}

# 可以撤销的售后状态。**不含 40**：用户已经寄出货了，撤销会留下一笔
# 在途退货无法记账。一期禁止，将来可加"拒收途中件"
REVOCABLE_STATUSES = frozenset({S.APPLYING, S.WAIT_REFUND, S.WAIT_RETURN})

TERMINAL_STATUSES = frozenset(
    {S.MERCHANT_REJECTED, S.QUALITY_FAILED, S.SUCCESS, S.CLOSED, S.USER_REVOKED}
)


def can_transit(from_status: RefundStatus, event: RefundEvent) -> bool:
    """这个流转是否合法。给调用方做预检用（真正的防线在 CAS 与行锁）。"""
    return event in TRANSITIONS.get(from_status, {})


def next_status(from_status: RefundStatus, event: RefundEvent) -> RefundStatus:
    """算出目标状态。非法流转抛业务异常。"""
    target = TRANSITIONS.get(from_status, {}).get(event)
    if target is None:
        raise BizError(
            ErrorCode.AFTERSALE_STATUS_INVALID,
            f"售后单当前是「{REFUND_STATUS_TEXT.get(int(from_status), from_status)}」，"
            "不能执行该操作",
        )
    return target


def is_terminal(status: RefundStatus) -> bool:
    return status in TERMINAL_STATUSES


def is_active(status: RefundStatus) -> bool:
    """是否"进行中"。与 ``uk_refund_sub_active`` 部分唯一索引的口径必须一致。"""
    return not is_terminal(status) and status is not S.PLATFORM_INTERVENING


# ============================================================
# 与子单状态机的桥
# ============================================================
def apply_event_for(sub_status: int):
    """申请售后该发哪个**交易事件**。三条边都汇入子单的 60 退款中。

    返回 ``OrderEvent``。子单状态不支持售后时抛 ``ORDER_STATUS_INVALID``。
    """
    from app.core.enums import OrderEvent, SubOrderStatus

    status = SubOrderStatus(sub_status)
    if status in (SubOrderStatus.WAIT_DELIVER, SubOrderStatus.WAIT_RECEIVE):
        return OrderEvent.APPLY_REFUND
    if status is SubOrderStatus.FINISHED:
        return OrderEvent.APPLY_AFTERSALE
    raise BizError(
        ErrorCode.ORDER_STATUS_INVALID, "该订单当前状态不支持申请售后"
    )


def refund_type_for(sub_status: int) -> int:
    """由子单状态**强制推导**售后类型（不信客户端）。

    - 未发货（20）：货还在仓库里，仅退款即可
    - 已发货（30）/ 已完成（40）：货已经出去了，必须走退货退款
    """
    from app.core.enums import SubOrderStatus

    status = SubOrderStatus(sub_status)
    if status is SubOrderStatus.WAIT_DELIVER:
        return REFUND_ONLY
    if status in (SubOrderStatus.WAIT_RECEIVE, SubOrderStatus.FINISHED):
        return RETURN_REFUND
    raise BizError(ErrorCode.ORDER_STATUS_INVALID, "该订单当前状态不支持申请售后")


def timeout_event(status: RefundStatus, refund_type: int) -> RefundEvent:
    """某个环节超时时该发哪个事件（docs/08 §7）。

    只有带 ``deadline`` 的四个环节会走到这里，其余状态没有 deadline、不会被扫到。
    """
    if status is S.APPLYING:
        # 48h 未审核 → 自动同意（本期不做平台介入）
        return (
            E.AUTO_APPROVE_REFUND if refund_type == REFUND_ONLY else E.AUTO_APPROVE_RETURN
        )
    if status is S.WAIT_RETURN:
        return E.TIMEOUT_CLOSE
    if status is S.WAIT_RECEIVE:
        return E.AUTO_RECEIVE
    if status is S.QUALITY_CHECKING:
        return E.AUTO_QUALITY_PASS
    raise BizError(ErrorCode.AFTERSALE_STATUS_INVALID, "该售后状态没有超时处理")
