"""aftersale 的纯函数单测：退款金额计算与售后状态机。

不碰数据库，跑得飞快。

**金额与状态机是本模块仅有的两处"算错了很贵"的地方** ——
前者算错是直接资损且对账永远不平，后者漏一条边是线上的一次非法流转。
所以这两块都用穷举/精确断言钉死，而不是挑几个用例试试。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.enums import OrderEvent, RefundStatus, SubOrderStatus
from app.core.errors import BizError, ErrorCode
from app.modules.aftersale import calc
from app.modules.aftersale.models import (
    QUALITY_PASS,
    REASON_NO_LONGER_WANT,
    REASON_QUALITY,
    REFUND_ONLY,
    RETURN_REFUND,
)
from app.modules.aftersale.state_machine import (
    TRANSITIONS,
    E,
    RefundEvent,
    S,
    apply_event_for,
    can_transit,
    is_active,
    is_terminal,
    next_status,
    refund_type_for,
    timeout_event,
)
from app.modules.trade.models import OrderItem, OrderSub


def _item(
    *,
    id: int = 1,
    num: int = 3,
    payable_amount: int = 1000,
    refunded_num: int = 0,
    refunding_num: int = 0,
    refunded_amount: int = 0,
) -> OrderItem:
    """内存里造一个订单项。ORM 对象不落库也能直接实例化。"""
    return OrderItem(
        id=id,
        num=num,
        item_amount=payable_amount,
        payable_amount=payable_amount,
        refunded_num=refunded_num,
        refunding_num=refunding_num,
        refunded_amount=refunded_amount,
    )


def _sub(*, payable_amount: int = 1000, refunded_amount: int = 0, freight: int = 1300) -> OrderSub:
    return OrderSub(
        payable_amount=payable_amount,
        refunded_amount=refunded_amount,
        freight_amount=freight,
    )


# ============================================================
# 逐项退款：差额法
# ============================================================
def test_item_refund_single_shot_is_full_amount() -> None:
    """一次退完：直接拿实付分摊额。"""
    assert calc.calc_item_refund(_item(num=3, payable_amount=1000), 3) == 1000


def test_item_refund_uses_difference_method_on_last() -> None:
    """★ 3 件分 3 次各退 1 件：333 / 333 / 334，累计恰好 1000。

    纯比例法会算出 333 × 3 = 999，少 1 分 —— 少的那 1 分就是退款对账永远平不了的原因。
    """
    item = _item(num=3, payable_amount=1000)

    first = calc.calc_item_refund(item, 1)
    assert first == 333, "非最后一笔按件比例向下取整"

    item.refunded_num = 1
    item.refunded_amount = first
    second = calc.calc_item_refund(item, 1)
    assert second == 333

    item.refunded_num = 2
    item.refunded_amount = first + second
    third = calc.calc_item_refund(item, 1)
    assert third == 334, "最后一笔用差额法补齐"

    assert first + second + third == item.payable_amount


def test_item_refund_two_then_one() -> None:
    """2 件 + 1 件：第一笔 666，第二笔差额 334。"""
    item = _item(num=3, payable_amount=1000)
    first = calc.calc_item_refund(item, 2)
    assert first == 666
    item.refunded_num, item.refunded_amount = 2, first
    assert calc.calc_item_refund(item, 1) == 334


def test_item_refund_rejects_too_many() -> None:
    item = _item(num=3, payable_amount=1000, refunded_num=2)
    with pytest.raises(BizError) as exc:
        calc.calc_item_refund(item, 2)
    assert exc.value.code is ErrorCode.REFUND_NUM_EXCEED


def test_item_refund_rejects_zero_or_negative() -> None:
    with pytest.raises(BizError):
        calc.calc_item_refund(_item(), 0)
    with pytest.raises(BizError):
        calc.calc_item_refund(_item(), -1)


# ============================================================
# 整单判定与运费
# ============================================================
def test_whole_sub_when_all_items_fully_refunded() -> None:
    items = [_item(id=1, num=1), _item(id=2, num=2)]
    assert calc.is_whole_sub_refund(items, {1: 1, 2: 2}) is True


def test_partial_sub_when_one_item_left() -> None:
    items = [_item(id=1, num=1), _item(id=2, num=2)]
    assert calc.is_whole_sub_refund(items, {1: 1}) is False


def test_whole_sub_counts_in_flight_refunds() -> None:
    """★ 别行的在途售后（refunding_num）也要算"已名花有主"。

    漏了它会把"其实整单已退完"误判成部分退 → 运费永远不退给用户。
    """
    items = [_item(id=1, num=1, refunding_num=1), _item(id=2, num=2)]
    assert calc.is_whole_sub_refund(items, {2: 2}) is True


def test_freight_only_refunded_on_whole_sub() -> None:
    sub = _sub(freight=1300)
    assert calc.calc_freight_refund(sub, whole=False) == 0
    assert calc.calc_freight_refund(sub, whole=True) == 1300


# ============================================================
# 守恒校验
# ============================================================
def test_check_limits_rejects_over_refund() -> None:
    sub = _sub(payable_amount=1000, refunded_amount=900)
    with pytest.raises(BizError) as exc:
        calc.check_refund_limits(sub, [_item()], 200)
    assert exc.value.code is ErrorCode.REFUND_AMOUNT_EXCEED


def test_check_limits_accepts_exact_boundary() -> None:
    sub = _sub(payable_amount=1000, refunded_amount=900)
    calc.check_refund_limits(sub, [_item()], 100)  # 不抛


def test_check_limits_rejects_num_overflow_with_invariant_error() -> None:
    """数量溢出属于"代码 Bug 级"的不变量破坏，用 PriceInvariantError。"""
    from app.core.errors import PriceInvariantError

    sub = _sub()
    items = [_item(num=3, refunded_num=2, refunding_num=2)]
    with pytest.raises(PriceInvariantError):
        calc.check_refund_limits(sub, items, 0)


# ============================================================
# 售后申请窗口
# ============================================================
def test_window_unlimited_before_receipt() -> None:
    """还没签收（= 还没发货）随时可退，不看时间。"""
    calc.assert_within_window(_sub(), REASON_NO_LONGER_WANT)  # 不抛


def test_window_no_reason_seven_days() -> None:
    now = datetime.now(UTC)
    sub = _sub()
    sub.receive_time = now - timedelta(days=6)
    calc.assert_within_window(sub, REASON_NO_LONGER_WANT, now=now)

    sub.receive_time = now - timedelta(days=8)
    with pytest.raises(BizError) as exc:
        calc.assert_within_window(sub, REASON_NO_LONGER_WANT, now=now)
    assert exc.value.code is ErrorCode.AFTERSALE_EXPIRED


def test_window_quality_fifteen_days() -> None:
    """质量问题窗口更长：第 8 天不想要已经过期，但质量问题还能申请。"""
    now = datetime.now(UTC)
    sub = _sub()
    sub.receive_time = now - timedelta(days=8)

    calc.assert_within_window(sub, REASON_QUALITY, now=now)  # 不抛
    with pytest.raises(BizError):
        calc.assert_within_window(sub, REASON_NO_LONGER_WANT, now=now)


# ============================================================
# 售后状态机 —— 穷举所有组合
# ============================================================
# 合法流转的期望结果。这张表就是 docs/08 §3.2 的流程图 + 仅退款分支
EXPECTED: dict[tuple[RefundStatus, RefundEvent], RefundStatus] = {
    # 10 待商家审核
    (S.APPLYING, E.APPROVE_REFUND): S.WAIT_REFUND,
    (S.APPLYING, E.APPROVE_RETURN): S.WAIT_RETURN,
    (S.APPLYING, E.AUTO_APPROVE_REFUND): S.WAIT_REFUND,
    (S.APPLYING, E.AUTO_APPROVE_RETURN): S.WAIT_RETURN,
    (S.APPLYING, E.MERCHANT_REJECT): S.MERCHANT_REJECTED,
    (S.APPLYING, E.USER_REVOKE): S.USER_REVOKED,
    # 20 待退款（瞬态）
    (S.WAIT_REFUND, E.START_REFUND): S.REFUNDING,
    (S.WAIT_REFUND, E.USER_REVOKE): S.USER_REVOKED,
    # 30 待买家寄回
    (S.WAIT_RETURN, E.FILL_RETURN_EXPRESS): S.WAIT_RECEIVE,
    (S.WAIT_RETURN, E.TIMEOUT_CLOSE): S.CLOSED,
    (S.WAIT_RETURN, E.USER_REVOKE): S.USER_REVOKED,
    # 40 待商家收货
    (S.WAIT_RECEIVE, E.MERCHANT_RECEIVE): S.QUALITY_CHECKING,
    (S.WAIT_RECEIVE, E.AUTO_RECEIVE): S.QUALITY_CHECKING,
    # 50 质检中
    (S.QUALITY_CHECKING, E.QUALITY_PASS): S.REFUNDING,
    (S.QUALITY_CHECKING, E.AUTO_QUALITY_PASS): S.REFUNDING,
    (S.QUALITY_CHECKING, E.QUALITY_FAIL): S.QUALITY_FAILED,
    # 60 退款中
    (S.REFUNDING, E.REFUND_SUCCESS): S.SUCCESS,
}


def test_transitions_table_matches_expected() -> None:
    """状态机表与设计逐条对齐。

    以后有人往 TRANSITIONS 里加边，必须同时在这里说明它对应哪条规则 ——
    防止悄悄加出一条不该有的流转。
    """
    actual: dict[tuple[RefundStatus, RefundEvent], RefundStatus] = {}
    for from_status, events in TRANSITIONS.items():
        for event, to_status in events.items():
            actual[(from_status, event)] = to_status
    assert actual == EXPECTED


def test_every_illegal_combination_rejected() -> None:
    """**穷举** 所有 (状态, 事件) 组合，非法的必须全部被拒。

    手工列用例必然有遗漏，而遗漏的那条就是线上的一次非法流转
    （比如对已退款的售后单再执行一次"同意"）。
    """
    checked = 0
    for from_status in list(RefundStatus):
        for event in list(RefundEvent):
            checked += 1
            if (from_status, event) in EXPECTED:
                continue
            with pytest.raises(BizError) as exc:
                next_status(from_status, event)
            assert exc.value.code is ErrorCode.AFTERSALE_STATUS_INVALID, (
                f"{from_status.name} + {event} 应当被拒绝"
            )
    assert checked == len(list(RefundStatus)) * len(list(RefundEvent))


def test_terminal_states_have_no_outgoing_edges() -> None:
    """终态不能有任何出边 —— 已退款/已关闭的售后单不该再流转。

    特别是 60 退款中：钱已经在路上了，不能再撤销或拒绝。
    """
    for status in (
        S.MERCHANT_REJECTED,
        S.QUALITY_FAILED,
        S.SUCCESS,
        S.CLOSED,
        S.USER_REVOKED,
    ):
        assert TRANSITIONS[status] == {}, f"{status.name} 应当是终态"
        assert is_terminal(status) is True

    assert is_terminal(S.REFUNDING) is False


def test_refunding_cannot_be_cancelled() -> None:
    """★ 退款中不能撤销/拒绝 —— 渠道可能已经打款了。"""
    assert next_status(S.REFUNDING, E.REFUND_SUCCESS) is S.SUCCESS
    for event in (E.USER_REVOKE, E.MERCHANT_REJECT, E.TIMEOUT_CLOSE):
        with pytest.raises(BizError):
            next_status(S.REFUNDING, event)


def test_platform_intervening_is_unreachable() -> None:
    """本期不做平台介入：状态保留但没有任何入边。"""
    assert TRANSITIONS[S.PLATFORM_INTERVENING] == {}
    for events in TRANSITIONS.values():
        assert S.PLATFORM_INTERVENING not in events.values()


def test_can_transit_helper() -> None:
    assert can_transit(S.APPLYING, E.APPROVE_REFUND) is True
    assert can_transit(S.SUCCESS, E.APPROVE_REFUND) is False


def test_is_active_matches_partial_index_scope() -> None:
    """``is_active`` 的口径必须与 uk_refund_sub_active 部分唯一索引一致。"""
    assert is_active(S.APPLYING) is True
    assert is_active(S.REFUNDING) is True
    assert is_active(S.QUALITY_CHECKING) is True
    assert is_active(S.SUCCESS) is False
    assert is_active(S.QUALITY_FAILED) is False
    assert is_active(S.PLATFORM_INTERVENING) is False


# ============================================================
# 与子单状态机的桥
# ============================================================
@pytest.mark.parametrize(
    ("sub_status", "expected_event", "expected_type"),
    [
        (SubOrderStatus.WAIT_DELIVER, OrderEvent.APPLY_REFUND, REFUND_ONLY),
        (SubOrderStatus.WAIT_RECEIVE, OrderEvent.APPLY_REFUND, RETURN_REFUND),
        (SubOrderStatus.FINISHED, OrderEvent.APPLY_AFTERSALE, RETURN_REFUND),
    ],
)
def test_apply_event_and_type_mapping(
    sub_status: SubOrderStatus, expected_event: OrderEvent, expected_type: int
) -> None:
    assert apply_event_for(int(sub_status)) == expected_event
    assert refund_type_for(int(sub_status)) == expected_type


@pytest.mark.parametrize(
    "sub_status",
    [SubOrderStatus.WAIT_PAY, SubOrderStatus.CLOSED, SubOrderStatus.REFUNDING, SubOrderStatus.REFUNDED],
)
def test_apply_rejected_for_unsupported_sub_status(sub_status: SubOrderStatus) -> None:
    """待付款走取消订单、已关闭/已退款不能再申请 —— 都要被拒。"""
    with pytest.raises(BizError) as exc:
        apply_event_for(int(sub_status))
    assert exc.value.code is ErrorCode.ORDER_STATUS_INVALID


@pytest.mark.parametrize(
    ("status", "refund_type", "expected"),
    [
        (S.APPLYING, REFUND_ONLY, E.AUTO_APPROVE_REFUND),
        (S.APPLYING, RETURN_REFUND, E.AUTO_APPROVE_RETURN),
        (S.WAIT_RETURN, RETURN_REFUND, E.TIMEOUT_CLOSE),
        (S.WAIT_RECEIVE, RETURN_REFUND, E.AUTO_RECEIVE),
        (S.QUALITY_CHECKING, RETURN_REFUND, E.AUTO_QUALITY_PASS),
    ],
)
def test_timeout_event_per_stage(
    status: RefundStatus, refund_type: int, expected: RefundEvent
) -> None:
    assert timeout_event(status, refund_type) == expected


def test_timeout_event_rejects_stages_without_deadline() -> None:
    """退款中/终态没有 deadline，不该被超时任务扫到。"""
    with pytest.raises(BizError):
        timeout_event(S.REFUNDING, RETURN_REFUND)


def test_quality_pass_constant_is_not_confused_with_status() -> None:
    """质检结果的 1/2 与售后状态的 10/11… 是两套编码，别混用。"""
    assert QUALITY_PASS == 1
    assert S.APPLYING == 10
