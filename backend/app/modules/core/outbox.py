"""本地消息表（outbox）的写入接口。

**解决的问题**：状态变更的事务里，有些副作用没法跟着回滚 —— 发站内信、
扣积分、回补 Redis 库存。如果把它们直接放在事务里执行：

- Redis 操作无法随 PG 事务回滚。**先回补 Redis 再提交事务**，一旦事务失败，
  Redis 已经多出库存 —— 直接超卖（docs/07 §7.3 点名了这个陷阱）。
- 发通知更糟：事务回滚了消息却已经发出去，用户收到"订单已创建"但订单不存在。

**做法**：把副作用写进 `core.local_message`（与业务变更**同一个事务**），
保证"业务提交 ⇔ 消息存在"，再由投递协程异步推到 Redis Streams。
消费端按 `biz_key` 幂等，于是得到**至少一次**的可靠投递，不需要引入独立 MQ。

与 `inventory` / `promotion` 的 `biz_key` 是同一套思路：
把幂等责任放到最底层，上层就可以简化。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.core.models import LocalMessage

# 已定义的事件主题。集中在这里，避免各处手写字符串拼错
TOPIC_ORDER_CLOSED = "notify.order_closed"
TOPIC_ORDER_PAID = "trade.order_paid"
TOPIC_SUB_STATUS_CHANGED = "trade.sub_status_changed"
TOPIC_INVENTORY_REDIS_RELEASE = "inventory.redis_release"
TOPIC_AFTERSALE_STATUS_CHANGED = "aftersale.status_changed"
TOPIC_AFTERSALE_REFUND_SUCCEEDED = "aftersale.refund_succeeded"


async def add(
    session: AsyncSession,
    *,
    topic: str,
    biz_key: str,
    payload: dict[str, Any],
) -> bool:
    """在调用方的事务里写一条待投递消息。返回是否新写入。

    ★ **必须在业务事务内调用**，这样"业务变更提交"和"消息存在"是原子的。
    写完不要自己 commit —— 事务边界由 ``get_session`` 依赖统一管理。

    ``UNIQUE (topic, biz_key)`` 让重复写入被静默忽略（``ON CONFLICT DO NOTHING``）：
    同一次状态变更被重放时，不会产生两条通知。
    """
    stmt = pg_insert(LocalMessage).values(
        topic=topic,
        biz_key=biz_key[:160],
        payload=payload,
        status=0,
        retry_count=0,
        next_retry_at=func.now(),
    )
    result = await session.execute(
        stmt.on_conflict_do_nothing(constraint="uk_local_message_topic_biz_key")
    )
    return result.rowcount == 1
