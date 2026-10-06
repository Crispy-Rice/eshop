"""outbox topic → 处理函数。投递循环（``worker/outbox_delivery.py``）照 ``DISPATCH`` 分派。

★ 注册表**必须覆盖 ``core.outbox`` 里全部 ``TOPIC_*`` 常量**：漏掉的 topic 每次投递
  都抛"未注册的 topic"，退避到第 15 次被弃置并写 P1 告警。有单测盯着这条
  （新增 topic 却忘了加 handler，测试立刻红）。

六张 taker 分三类：

| topic | 处理 |
|---|---|
| ``notify.order_closed`` / ``trade.order_paid`` / ``aftersale.refund_succeeded`` | 落**站内信** |
| ``trade.sub_status_changed`` / ``aftersale.status_changed`` | 演进类：**登记但不必发**（不做逐步进度播报） |
| ``inventory.redis_release`` | ★ **no-op**，理由见 ``_redis_release`` |

``biz_key`` 进站内信时**带上 topic 前缀**：``site_message.biz_key`` 是全局唯一，
  而 outbox 的唯一键只是 ``(topic, biz_key)`` —— 不加前缀，两张表里同名的键会撞。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgLinkType, SiteMsgType
from app.core.logging import get_logger
from app.modules.core import outbox
from app.modules.notify import service as notify_service
from app.modules.notify.models import MAX_BIZ_KEY_LEN

logger = get_logger(__name__)

Handler = Callable[..., Awaitable[None]]


def _site_biz(topic: str, biz_key: str) -> str:
    """站内信的幂等键。

    截断是安全的：两个不同 topic 的前缀不同，截断到 160 后仍不同；同一 topic 下
    两条 biz_key 若前 160 字符相同，那它们本来就是同一个 biz_key（它自己就是
    VARCHAR(160)）。
    """
    return f"{topic}:{biz_key}"[:MAX_BIZ_KEY_LEN]


async def _order_closed(
    session: AsyncSession, *, payload: dict[str, Any], biz_key: str
) -> None:
    order_main_no = str(payload["orderMainNo"])
    await notify_service.push(
        session,
        user_id=int(payload["userId"]),
        msg_type=SiteMsgType.ORDER_CLOSED,
        title="订单已关闭",
        body=f"订单 {order_main_no} 已关闭",
        biz_key=_site_biz(outbox.TOPIC_ORDER_CLOSED, biz_key),
        link_type=MsgLinkType.ORDER,
        link_value=order_main_no,
    )


async def _order_paid(session: AsyncSession, *, payload: dict[str, Any], biz_key: str) -> None:
    order_main_no = str(payload["orderMainNo"])
    await notify_service.push(
        session,
        user_id=int(payload["userId"]),
        msg_type=SiteMsgType.ORDER_PAID,
        title="支付成功",
        body=f"订单 {order_main_no} 已支付，等待商家发货",
        biz_key=_site_biz(outbox.TOPIC_ORDER_PAID, biz_key),
        link_type=MsgLinkType.ORDER,
        link_value=order_main_no,
    )


async def _refund_succeeded(
    session: AsyncSession, *, payload: dict[str, Any], biz_key: str
) -> None:
    refund_no = str(payload["refundNo"])
    await notify_service.push(
        session,
        user_id=int(payload["userId"]),
        msg_type=SiteMsgType.REFUND_SUCCEEDED,
        title="退款已到账",
        body=f"售后 {refund_no} 的退款已原路退回",
        biz_key=_site_biz(outbox.TOPIC_AFTERSALE_REFUND_SUCCEEDED, biz_key),
        link_type=MsgLinkType.REFUND,
        link_value=refund_no,
    )


async def _noop_sub_status(
    session: AsyncSession, *, payload: dict[str, Any], biz_key: str
) -> None:
    """子单状态流转：**登记已处理，不发站内信**。

    买家在订单页本来就能看到每一步状态；把七种流转都推成站内信只会把消息中心
    变成噪音。真要播报的是"支付成功/订单关闭/退款到账"这三件**结果**。
    """
    logger.debug("outbox 演进类事件（不发通知）", extra={"bizKey": biz_key})


async def _noop_aftersale_status(
    session: AsyncSession, *, payload: dict[str, Any], biz_key: str
) -> None:
    """售后状态流转：同上，只由 ``aftersale.refund_succeeded`` 那一条发通知。"""
    logger.debug("outbox 演进类事件（不发通知）", extra={"bizKey": biz_key})


async def _redis_release(
    session: AsyncSession, *, payload: dict[str, Any], biz_key: str
) -> None:
    """★ **有意留空** —— 不是忘了写。

    这个 topic 承诺的是 **Redis** 侧的预占回补（``trade`` 关单时 DB 侧的释放
    已经在同一个事务里做完了，见 ``trade/service.py`` 的关单路径，那里的注释写着
    "Redis 侧回补与通知走 outbox"）。

    今天 Redis 的漂移由 ``reconcile_stock``（每 5 分钟）兜着，所以**不实现它 =
    与这一轮之前的行为完全一致**。要改成即时释放，得先想清楚交易路径上
    "Redis 预占"的释放语义（幂等键、DB 已释放时 Redis 该不该再动），
    那是单独一轮的事（docs/19 的「明确不做」）。
    """
    logger.debug("outbox inventory.redis_release 暂无处理（由对账 cron 兜底）")


DISPATCH: dict[str, Handler] = {
    outbox.TOPIC_ORDER_CLOSED: _order_closed,
    outbox.TOPIC_ORDER_PAID: _order_paid,
    outbox.TOPIC_AFTERSALE_REFUND_SUCCEEDED: _refund_succeeded,
    outbox.TOPIC_SUB_STATUS_CHANGED: _noop_sub_status,
    outbox.TOPIC_AFTERSALE_STATUS_CHANGED: _noop_aftersale_status,
    outbox.TOPIC_INVENTORY_REDIS_RELEASE: _redis_release,
}
