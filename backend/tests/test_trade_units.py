"""trade 的纯函数单测：订单号与状态机。

不碰数据库，跑得飞快。状态机这部分**穷举**所有 (状态, 事件) 组合 ——
手工列用例必然会漏，而漏掉的那条就是线上的一次非法流转。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.errors import BizError, ErrorCode
from app.modules.trade.models import (
    PAY_FULL_REFUND,
    PAY_PAID,
    PAY_PARTIAL_REFUND,
    PAY_UNPAID,
)
from app.modules.trade.order_no import (
    MAIN_NO_LENGTH,
    build_delivery_no,
    build_main_no,
    build_pay_no,
    build_sub_no,
    is_valid_luhn,
    is_valid_main_no,
    luhn_check_digit,
    parse_main_no,
)
from app.modules.trade.state_machine import (
    TRANSITIONS,
    E,
    OrderEvent,
    S,
    SubOrderStatus,
    aggregate_main_status,
    can_transit,
    derive_pay_status,
    next_status,
)

# ============================================================
# 订单号
# ============================================================


def test_luhn_check_digit_known_values() -> None:
    """Luhn 的标准测试向量（信用卡号去掉校验位的那套算法）。"""
    # "7992739871" 的校验位是 3
    assert luhn_check_digit("7992739871") == 3
    assert is_valid_luhn("79927398713") is True
    assert is_valid_luhn("79927398712") is False


def test_main_no_format() -> None:
    no = build_main_no(1234567890123456789, now=datetime(2026, 9, 30))
    assert len(no) == MAIN_NO_LENGTH
    assert no.startswith("M20260930")
    assert is_valid_main_no(no) is True


def test_main_no_parse_roundtrip() -> None:
    no = build_main_no(999, now=datetime(2026, 1, 2))
    assert parse_main_no(no) == datetime(2026, 1, 2)


def test_main_no_check_digit_rejects_typo() -> None:
    """★ 校验位的意义：用户手输错一位要能被立刻发现。

    这里把中间某一位 +1，模拟手输错误。绝大多数情况校验位会对不上。
    """
    no = build_main_no(1234567890123456789, now=datetime(2026, 9, 30))
    digits = list(no)
    # 改动序号部分的一位
    digits[10] = "9" if digits[10] != "9" else "8"
    typo = "".join(digits)
    assert is_valid_main_no(typo) is False


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "M123",
        "X202609301234567890",  # 前缀不对
        "M20260930123456789A",  # 含非数字
        "M202613321234567890",  # 月份非法（且长度可能对但解析会失败）
    ],
)
def test_main_no_rejects_bad_input(bad: str) -> None:
    assert is_valid_main_no(bad) is False


def test_sub_no() -> None:
    no = build_main_no(42, now=datetime(2026, 9, 30))
    assert build_sub_no(no, 1) == f"{no}-1"
    assert build_sub_no(no, 12) == f"{no}-12"


def test_sub_no_index_must_start_at_one() -> None:
    with pytest.raises(ValueError):
        build_sub_no("M12345678901234567890", 0)


def test_pay_and_delivery_no_have_distinct_prefixes() -> None:
    """三种号的前缀不同，排查问题时一眼能分辨。"""
    now = datetime(2026, 9, 30)
    assert build_pay_no(1, now=now).startswith("P")
    assert build_delivery_no(1, now=now).startswith("D")
    assert build_main_no(1, now=now).startswith("M")


def test_main_no_deterministic() -> None:
    """同样的输入永远同样的号（单测里可复现）。"""
    a = build_main_no(123, now=datetime(2026, 5, 6))
    b = build_main_no(123, now=datetime(2026, 5, 6))
    assert a == b


# ============================================================
# 状态机 —— 穷举所有组合
# ============================================================

# 合法流转的期望结果。这张表就是 docs/07 §4.2 的状态流转图
EXPECTED: dict[tuple[SubOrderStatus, OrderEvent], SubOrderStatus | None] = {
    (S.WAIT_PAY, E.PAY_SUCCESS): S.WAIT_DELIVER,
    (S.WAIT_PAY, E.USER_CANCEL): S.CLOSED,
    (S.WAIT_PAY, E.TIMEOUT_CANCEL): S.CLOSED,
    (S.WAIT_DELIVER, E.SHIP): S.WAIT_RECEIVE,
    (S.WAIT_DELIVER, E.PARTIAL_SHIP): S.WAIT_DELIVER,
    (S.WAIT_DELIVER, E.APPLY_REFUND): S.REFUNDING,
    (S.WAIT_RECEIVE, E.CONFIRM_RECEIVE): S.FINISHED,
    (S.WAIT_RECEIVE, E.AUTO_RECEIVE): S.FINISHED,
    (S.WAIT_RECEIVE, E.APPLY_REFUND): S.REFUNDING,
    (S.FINISHED, E.APPLY_AFTERSALE): S.REFUNDING,
    (S.REFUNDING, E.REFUND_SUCCESS): S.REFUNDED,
    (S.REFUNDING, E.REFUND_REJECT): None,
    (S.REFUNDING, E.USER_REVOKE): None,
}


def test_transitions_table_matches_expected() -> None:
    """状态机表与文档 §4.2 的流转图**逐条对齐**。

    这条测试的价值在于：以后有人往 TRANSITIONS 里加了一条，
    必须同时在这里说明它来自文档的哪一条 —— 防止悄悄加出一条不该有的边。
    """
    actual: dict[tuple[SubOrderStatus, OrderEvent], SubOrderStatus | None] = {}
    for from_status, events in TRANSITIONS.items():
        for event, to_status in events.items():
            actual[(from_status, event)] = to_status
    assert actual == EXPECTED


def test_every_illegal_combination_rejected() -> None:
    """**穷举**所有 (状态, 事件) 组合，非法的必须全部被拒。

    手工列用例必然有遗漏，而遗漏的那条就是线上的一次非法流转
    （比如对已关闭的订单执行确认收货）。
    """
    all_statuses = list(SubOrderStatus)
    all_events = list(OrderEvent)
    checked = 0

    for from_status in all_statuses:
        for event in all_events:
            checked += 1
            legal = (from_status, event) in EXPECTED
            if legal:
                continue
            with pytest.raises(BizError) as exc:
                next_status(from_status, event, restore_to=S.WAIT_DELIVER)
            assert exc.value.code is ErrorCode.ORDER_STATUS_INVALID, (
                f"{from_status.name} + {event} 应当被拒绝"
            )

    # 7 个状态 × 12 个事件
    assert checked == len(all_statuses) * len(all_events)


def test_terminal_states_have_no_outgoing_edges() -> None:
    """终态不能有任何出边 —— 已关闭/已退款的订单不该再流转。"""
    assert TRANSITIONS[S.CLOSED] == {}
    assert TRANSITIONS[S.REFUNDED] == {}


def test_refund_reject_requires_restore_to() -> None:
    """``REFUND_REJECT`` 的目标是"原状态"，不给 restore_to 就该报错。"""
    with pytest.raises(BizError) as exc:
        next_status(S.REFUNDING, E.REFUND_REJECT)
    assert "source_status" in exc.value.message


@pytest.mark.parametrize("restore", [S.WAIT_DELIVER, S.WAIT_RECEIVE, S.FINISHED])
def test_refund_reject_restores_to_source(restore: SubOrderStatus) -> None:
    """20→60 和 30→60 被拒后要回到**各自**的原状态。"""
    assert next_status(S.REFUNDING, E.REFUND_REJECT, restore_to=restore) is restore
    assert next_status(S.REFUNDING, E.USER_REVOKE, restore_to=restore) is restore


def test_can_transit_helper() -> None:
    assert can_transit(S.WAIT_PAY, E.PAY_SUCCESS) is True
    assert can_transit(S.CLOSED, E.PAY_SUCCESS) is False


# ============================================================
# 母单状态聚合
# ============================================================


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({S.WAIT_PAY}, S.WAIT_PAY),
        ({S.WAIT_DELIVER}, S.WAIT_DELIVER),
        ({S.FINISHED}, S.FINISHED),
        ({S.CLOSED}, S.CLOSED),
        ({S.REFUNDED}, S.REFUNDED),
        # 混合：按"最落后"的状态展示
        ({S.WAIT_PAY, S.FINISHED}, S.WAIT_PAY),
        ({S.WAIT_DELIVER, S.WAIT_RECEIVE}, S.WAIT_DELIVER),
        ({S.WAIT_RECEIVE, S.FINISHED}, S.WAIT_RECEIVE),
        # ★ "部分退款"的典型：一件已退款、一件待收货 → 显示待收货
        ({S.REFUNDED, S.WAIT_RECEIVE}, S.WAIT_RECEIVE),
        ({S.REFUNDED, S.FINISHED}, S.FINISHED),
        # 只剩关闭与退款
        ({S.CLOSED, S.REFUNDED}, S.REFUNDED),
        ({S.CLOSED, S.FINISHED}, S.FINISHED),
    ],
)
def test_aggregate_main_status(statuses: set[SubOrderStatus], expected: SubOrderStatus) -> None:
    assert aggregate_main_status(statuses) is expected


def test_aggregate_rejects_empty() -> None:
    with pytest.raises(ValueError):
        aggregate_main_status(set())


# ============================================================
# 母单 pay_status
# ============================================================


def test_pay_status_unpaid() -> None:
    assert derive_pay_status(0, 0, sub_count=2, refunded_subs=0) == PAY_UNPAID


def test_pay_status_paid() -> None:
    assert derive_pay_status(10000, 0, sub_count=2, refunded_subs=0) == PAY_PAID


def test_pay_status_partial_refund() -> None:
    assert derive_pay_status(10000, 3000, sub_count=2, refunded_subs=1) == PAY_PARTIAL_REFUND


def test_pay_status_full_refund() -> None:
    assert derive_pay_status(10000, 10000, sub_count=2, refunded_subs=2) == PAY_FULL_REFUND


def test_pay_status_full_refund_requires_all_subs() -> None:
    """只退了一个子单但金额恰好退完 —— 仍算部分退款（子单没退完）。"""
    assert derive_pay_status(10000, 10000, sub_count=2, refunded_subs=1) == PAY_PARTIAL_REFUND
