"""本地消息表（outbox）的投递循环 —— 见 docs/19 §3。

**它补的是什么**：``core.local_message`` 从第一天起就有人在写（trade / payment /
aftersale 共 6 个 topic），但**从来没有消费者** —— 表只写不读
（``product/service.py`` 与 ``payment/models.py`` 的注释自己都点明了这一点）。
这一轮把另一半接上：轮询 → 就地分派 → 落站内信 / 执行副作用。

★ **有意偏离 docs/13 §4 与 docs/14 §6**：那两处写的是"投递到 Redis Streams
  （XADD）+ 消费组（XREADGROUP/XACK）+ 五次进死信"。这里**不引入 Streams**，
  就在 worker 进程内分派。理由：那一层要等**第二个独立进程**也要消费同一批事件时
  才有意义（比如把单体拆成服务）；眼下唯一的消费者就是这个 worker，多一层只会
  多一份要各自对账的状态。``redis_keys.stream()`` / ``stream_dead()`` 因此继续闲置
  —— 真要拆服务时 ``local_message`` 仍是权威存储，可以重放。

**至少一次**：handler 成功后才把 ``status`` 置 1，所以进程在中间崩溃会重放。
  幂等由 handler 自己保证（站内信是 ``UNIQUE (biz_key)`` + ``ON CONFLICT DO NOTHING``）。
  ★ 因此 handler 内部的**部分写入**也能自愈：handler 写了一半就抛异常时，那些写入
  与"失败计数"一起提交，重试时被幂等吸收 —— 所以 handler 不必自己开保存点。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.modules.core.models import LocalMessage, OpsAlert
from app.modules.notify.handlers import DISPATCH

logger = get_logger(__name__)

JOB = "deliver_outbox"

# 一轮最多处理多少条。批小一点让每轮的锁持有时间可控
BATCH = 200
# 第几次失败后弃置（写 P1 告警，不再重试）
MAX_ATTEMPTS = 15
BASE_BACKOFF_SECONDS = 10
MAX_BACKOFF_SECONDS = 3600
ALERT_SOURCE = "core.outbox.delivery"


async def deliver_outbox(ctx: dict[str, Any]) -> dict[str, int]:
    """投递一轮。cron 每 5 秒叫一次（``worker/main.py``）。

    取件用 ``FOR UPDATE SKIP LOCKED``：多副本部署时两个 worker 既不会互相等，
    也不会重复处理同一行。
    """
    session_factory = ctx["session_factory"]
    delivered = 0
    failed = 0

    async with session_factory() as session, session.begin():
        rows = list(
            (
                await session.scalars(
                    select(LocalMessage)
                    .where(
                        LocalMessage.status.in_((0, 2)),
                        LocalMessage.next_retry_at <= func.now(),
                    )
                    .order_by(LocalMessage.id)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for row in rows:
            handler = DISPATCH.get(row.topic)
            try:
                if handler is None:
                    # 不认识的 topic **不能静默丢弃**：可能只是 worker 比 api 旧，
                    # 下次发布就好了。走同样的退避路径，第 15 次才弃置并告警。
                    raise RuntimeError(f"未注册的 outbox topic: {row.topic}")
                await handler(session, payload=row.payload or {}, biz_key=row.biz_key)
            except Exception as exc:
                failed += 1
                _mark_failed(session, row, exc)
            else:
                row.status = 1
                row.error_msg = None
                delivered += 1

    if delivered or failed:
        logger.info("outbox 投递完成", extra={"delivered": delivered, "failed": failed})
    return {"delivered": delivered, "failed": failed}


def _mark_failed(session: AsyncSession, row: LocalMessage, exc: Exception) -> None:
    """退避重试；到 ``MAX_ATTEMPTS`` 就弃置并写告警。"""
    row.retry_count = int(row.retry_count or 0) + 1
    row.error_msg = f"{type(exc).__name__}: {exc}"[:512]

    if row.retry_count >= MAX_ATTEMPTS:
        row.status = 3
        session.add(
            OpsAlert(
                level=2,
                source=ALERT_SOURCE,
                title=f"outbox 消息投递失败已弃置：{row.topic}",
                detail={
                    "topic": row.topic,
                    "bizKey": row.biz_key,
                    "retryCount": row.retry_count,
                    "error": row.error_msg,
                },
            )
        )
        logger.error(
            "outbox 消息弃置",
            extra={"topic": row.topic, "bizKey": row.biz_key, "retryCount": row.retry_count},
        )
        return

    row.status = 2
    delay = min(
        BASE_BACKOFF_SECONDS * (2 ** (row.retry_count - 1)),
        MAX_BACKOFF_SECONDS,
    )
    row.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
    logger.warning(
        "outbox 投递失败，稍后重试",
        extra={
            "topic": row.topic,
            "bizKey": row.biz_key,
            "retryCount": row.retry_count,
            "delay": delay,
            "error": row.error_msg,
        },
    )
