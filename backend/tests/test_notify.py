"""outbox 投递循环 + 站内信的集成测试（真 PG）。

投递循环直接调（不走 ARQ），这样能精确控制"投了几轮"。三件事要钉住：

1. 三个业务 topic 落到站内信，并把 ``local_message.status`` 置 1
2. 演进类 topic **登记但不必发**；未注册的 topic 走退避，到第 15 次弃置并写告警
3. **至少一次 + 幂等**：同一条消息重放两次，站内信只有一条
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session_factory
from app.modules.core import outbox
from app.worker.outbox_delivery import MAX_ATTEMPTS, deliver_outbox


async def _insert_message(
    session: AsyncSession,
    *,
    topic: str,
    biz_key: str,
    payload: dict | None = None,
    status: int = 0,
    retry_count: int = 0,
) -> None:
    await session.execute(
        text(
            "INSERT INTO core.local_message"
            " (topic, biz_key, payload, status, retry_count, next_retry_at)"
            " VALUES (:topic, :biz, CAST(:payload AS jsonb), :status, :retry, now())"
        ),
        {
            "topic": topic,
            "biz": biz_key,
            "payload": json.dumps(payload or {}),
            "status": status,
            "retry": retry_count,
        },
    )
    await session.commit()


async def _deliver() -> dict[str, int]:
    return await deliver_outbox({"session_factory": get_session_factory()})


async def _scalar(session: AsyncSession, sql: str) -> object:
    return await session.scalar(text(sql))


# ============================================================
# 1：三个业务 topic 落站内信
# ============================================================
async def test_order_paid_writes_site_message_and_marks_sent(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _insert_message(
        session,
        topic=outbox.TOPIC_ORDER_PAID,
        biz_key="PAID:M202601010001",
        payload={"orderMainNo": "M202601010001", "payNo": "P1", "userId": 777, "amount": 12300},
    )

    assert await _deliver() == {"delivered": 1, "failed": 0}

    row = (
        await session.execute(
            text(
                "SELECT user_id, msg_type, title, link_type, link_value, is_read"
                " FROM notify.site_message"
            )
        )
    ).mappings().one()
    assert row["user_id"] == 777
    assert row["msg_type"] == "ORDER_PAID"
    assert row["link_type"] == "ORDER"
    assert row["link_value"] == "M202601010001"
    assert row["is_read"] is False

    msg = (
        await session.execute(
            text("SELECT status, retry_count, error_msg FROM core.local_message")
        )
    ).mappings().one()
    assert msg["status"] == 1, "投递成功后必须置 1，否则每轮都会重投"
    assert msg["retry_count"] == 0
    assert msg["error_msg"] is None


@pytest.mark.parametrize(
    ("topic", "payload", "expected_msg_type", "expected_link"),
    [
        (
            outbox.TOPIC_ORDER_CLOSED,
            {"orderMainNo": "M1", "userId": 5},
            "ORDER_CLOSED",
            "ORDER",
        ),
        (
            outbox.TOPIC_AFTERSALE_REFUND_SUCCEEDED,
            {"refundNo": "R1", "orderMainNo": "M1", "userId": 5, "amount": 100},
            "REFUND_SUCCEEDED",
            "REFUND",
        ),
    ],
)
async def test_other_business_topics_write_site_message(
    client: AsyncClient,
    session: AsyncSession,
    topic: str,
    payload: dict,
    expected_msg_type: str,
    expected_link: str,
) -> None:
    await _insert_message(session, topic=topic, biz_key=f"{topic}:k1", payload=payload)
    assert await _deliver() == {"delivered": 1, "failed": 0}
    row = (
        await session.execute(text("SELECT msg_type, link_type FROM notify.site_message"))
    ).mappings().one()
    assert row["msg_type"] == expected_msg_type
    assert row["link_type"] == expected_link


async def test_evolving_topics_are_marked_sent_without_message(
    client: AsyncClient, session: AsyncSession
) -> None:
    """演进类（订单/售后的每一次状态流转）**不发站内信** —— 那只会变成噪音。"""
    await _insert_message(
        session,
        topic=outbox.TOPIC_SUB_STATUS_CHANGED,
        biz_key="SUB:M1:20",
        payload={"orderSubNo": "M1-1", "orderMainNo": "M1", "userId": 5, "from": 10, "to": 20},
    )
    await _insert_message(
        session,
        topic=outbox.TOPIC_AFTERSALE_STATUS_CHANGED,
        biz_key="AS:R1:30",
        payload={"refundNo": "R1", "orderMainNo": "M1", "from": 20, "to": 30},
    )
    assert await _deliver() == {"delivered": 2, "failed": 0}
    assert await _scalar(session, "SELECT count(*) FROM notify.site_message") == 0
    assert await _scalar(session, "SELECT count(*) FROM core.local_message WHERE status = 1") == 2


async def test_redis_release_topic_is_a_noop(client: AsyncClient, session: AsyncSession) -> None:
    """★ ``inventory.redis_release`` 是**有意留空**的（见 handlers 的说明）。

    它现在被标记为"已处理"但不做任何事 —— 与这一轮之前的行为**完全一致**
    （Redis 回补仍由 ``reconcile_stock`` 兜着）。这条用例把这个取舍钉住，
    免得以后有人以为"没实现"是漏了，顺手补上一个半吊子的 Redis 释放。
    """
    await _insert_message(
        session,
        topic=outbox.TOPIC_INVENTORY_REDIS_RELEASE,
        biz_key="CANCEL:M1",
        payload={"orderMainNo": "M1"},
    )
    assert await _deliver() == {"delivered": 1, "failed": 0}
    assert await _scalar(session, "SELECT count(*) FROM notify.site_message") == 0


# ============================================================
# 2：退避与弃置
# ============================================================
async def test_unknown_topic_retries_with_backoff(client: AsyncClient, session: AsyncSession) -> None:
    """未注册的 topic **不能静默丢弃**：可能只是 worker 比 api 旧。"""
    await _insert_message(session, topic="nope.unknown", biz_key="X1")

    assert await _deliver() == {"delivered": 0, "failed": 1}

    row = (
        await session.execute(
            text("SELECT status, retry_count, error_msg, next_retry_at > now() AS later"
                 " FROM core.local_message")
        )
    ).mappings().one()
    assert row["status"] == 2
    assert row["retry_count"] == 1
    assert row["later"] is True, "失败后 next_retry_at 应该推到将来，否则会立刻重试"
    assert "未注册的 outbox topic" in row["error_msg"]
    assert await _scalar(session, "SELECT count(*) FROM ops.alert") == 0, "还没到弃置阈值"


async def test_message_is_abandoned_after_max_attempts_with_alert(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _insert_message(
        session, topic="nope.unknown", biz_key="X2", retry_count=MAX_ATTEMPTS - 1
    )

    assert await _deliver() == {"delivered": 0, "failed": 1}

    row = (
        await session.execute(
            text("SELECT status, retry_count FROM core.local_message")
        )
    ).mappings().one()
    assert row["status"] == 3, "到阈值必须弃置，否则这条会永远占着队列"
    assert row["retry_count"] == MAX_ATTEMPTS

    alert = (
        await session.execute(
            text("SELECT level, source, title, detail FROM ops.alert")
        )
    ).mappings().one()
    assert alert["level"] == 2
    assert alert["source"] == "core.outbox.delivery"
    assert alert["detail"]["topic"] == "nope.unknown"


async def test_future_retry_is_not_picked_up(client: AsyncClient, session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO core.local_message"
            " (topic, biz_key, payload, status, retry_count, next_retry_at)"
            " VALUES (:topic, 'F1', '{}'::jsonb, 2, 1, now() + interval '1 hour')"
        ),
        {"topic": outbox.TOPIC_ORDER_PAID},
    )
    await session.commit()
    assert await _deliver() == {"delivered": 0, "failed": 0}
    assert await _scalar(session, "SELECT count(*) FROM notify.site_message") == 0


# ============================================================
# 3：至少一次 + 幂等
# ============================================================
async def test_delivery_is_idempotent_on_replay(client: AsyncClient, session: AsyncSession) -> None:
    """★ 这一条是整套设计的地基：投递是**至少一次**，所以 handler 必须幂等。

    重放的方式就是把状态手工退回 0（模拟"处理成功但 status 没来得及提交"）。
    """
    await _insert_message(
        session,
        topic=outbox.TOPIC_ORDER_PAID,
        biz_key="PAID:MREPLAY",
        payload={"orderMainNo": "MREPLAY", "payNo": "P9", "userId": 9, "amount": 1},
    )
    assert await _deliver() == {"delivered": 1, "failed": 0}
    assert await _scalar(session, "SELECT count(*) FROM notify.site_message") == 1

    await session.execute(
        text("UPDATE core.local_message SET status = 0, next_retry_at = now()")
    )
    await session.commit()

    assert await _deliver() == {"delivered": 1, "failed": 0}
    assert (
        await _scalar(session, "SELECT count(*) FROM notify.site_message")
    ) == 1, "同一个 biz_key 重放不该产生第二条站内信"


async def test_biz_key_is_scoped_by_topic(client: AsyncClient, session: AsyncSession) -> None:
    """两个 topic 用同一个 biz_key 时必须各发一条 —— 站内信的键带 topic 前缀。"""
    await _insert_message(
        session,
        topic=outbox.TOPIC_ORDER_PAID,
        biz_key="M-SAME",
        payload={"orderMainNo": "M-SAME", "payNo": "P1", "userId": 9, "amount": 1},
    )
    await _insert_message(
        session,
        topic=outbox.TOPIC_ORDER_CLOSED,
        biz_key="M-SAME",
        payload={"orderMainNo": "M-SAME", "userId": 9},
    )
    assert await _deliver() == {"delivered": 2, "failed": 0}
    assert await _scalar(session, "SELECT count(*) FROM notify.site_message") == 2


# ============================================================
# 站内信读侧（这里顺带覆盖一次，端到端在 test_support.py）
# ============================================================
async def test_notifications_endpoint_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/notifications")
    assert resp.status_code == 401
