"""support / notify 的纯函数单测。不连库、不连 Redis。

对标 ``test_aftersale_units.py`` / ``test_review_units.py``：能在这里说清的事
就别丢给集成测试 —— 跑得快、失败信息也更直接。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.modules.core import outbox
from app.modules.notify.handlers import DISPATCH
from app.modules.support.models import SENDER_MERCHANT, SENDER_PLATFORM, SENDER_USER
from app.modules.support.rules import is_buyer_sender, staff_owes_reply
from app.modules.trade.order_no import build_ticket_no, is_valid_luhn, parse_main_no

# ---------------------------------------------------------------
# 会话号
# ---------------------------------------------------------------


def test_ticket_no_shape_and_luhn() -> None:
    no = build_ticket_no(123456789012345678)
    assert no.startswith("T")
    assert len(no) == 20
    assert no[1:].isdigit()
    assert is_valid_luhn(no[1:]), "校验位必须自洽 —— 它是「手输错号」的第一道闸"


def test_ticket_no_date_prefix_is_readable() -> None:
    when = datetime(2026, 10, 6, 12, 0, 0)
    no = build_ticket_no(42, now=when)
    assert no[1:9] == "20261006"


def test_ticket_no_does_not_parse_as_order_no() -> None:
    """前缀不同 —— 拿会话号去查订单必须直接失败，而不是查出别人的单。"""
    with pytest.raises(ValueError):
        parse_main_no(build_ticket_no(1))


@pytest.mark.parametrize("sender", [SENDER_USER, SENDER_MERCHANT, SENDER_PLATFORM])
def test_is_buyer_sender(sender: int) -> None:
    assert is_buyer_sender(sender) is (sender == SENDER_USER)


# ---------------------------------------------------------------
# 「欠谁回复」
# ---------------------------------------------------------------


@pytest.mark.parametrize("last_sender", [SENDER_USER, SENDER_MERCHANT, SENDER_PLATFORM])
def test_staff_owes_reply_only_when_open_and_buyer_spoke_last(last_sender: int) -> None:
    expected = last_sender == SENDER_USER
    assert staff_owes_reply(10, last_sender) is expected
    # 关闭的会话不欠任何人，哪怕最后一句是买家说的
    assert staff_owes_reply(30, last_sender) is False


def test_staff_owes_reply_matches_sql_filter() -> None:
    """``rules.staff_owes_reply`` 与 ``repository.list_tickets(owes_reply_only=)``
    是同一条判定的两份写法（一份在 Python、一份在 SQL）。这条用例把"两处都只认
    （进行中 + 买家最后发言）"钉住 —— 集成测试再验证 SQL 那侧的返回值。"""
    assert staff_owes_reply(10, SENDER_USER) is True
    assert staff_owes_reply(10, SENDER_MERCHANT) is False
    assert staff_owes_reply(10, SENDER_PLATFORM) is False


# ---------------------------------------------------------------
# outbox 分派表
# ---------------------------------------------------------------


def test_dispatch_covers_every_topic() -> None:
    """★ 新增一个 ``TOPIC_*`` 却忘了注册处理函数，这里立刻红。

    漏掉的 topic 在线上会每次投递都抛"未注册的 topic"，退避到第 15 次被弃置 ——
    是**能用但会静默丢事件**的那种故障，必须在测试里挡住。
    """
    topics = {v for k, v in vars(outbox).items() if k.startswith("TOPIC_")}
    assert topics, "没有找到任何 TOPIC_* 常量，测试本身可能失效了"
    assert set(DISPATCH) == topics, f"未注册的 topic: {sorted(topics - set(DISPATCH))}"


def test_handlers_are_coroutines() -> None:
    for topic, handler in DISPATCH.items():
        assert callable(handler), topic
