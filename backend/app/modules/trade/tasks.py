"""trade 的定时任务：超时关单、自动确认收货。

注册在 ``app/worker/main.py`` 的 ``CRON_JOBS``。

**两条关单路径**（docs/07 §7.2）：
- 延迟任务 ``close_order_if_unpaid`` —— 下单时按 ``pay_deadline`` 投递，准点
- 定时扫描 ``scan_timeout_orders`` —— 每 2 分钟兜底

它们调的是同一个幂等函数 ``service.close_order``，所以**重复执行无副作用**。
这就是"幂等让容错变简单"：不需要分布式锁、不需要"这条任务有没有跑过"的状态，
放心让两条路径都跑。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.modules.trade import service
from app.modules.trade.state_machine import OrderEvent

logger = get_logger(__name__)


async def close_order_if_unpaid(ctx: dict[str, Any], order_main_no: str) -> dict[str, Any]:
    """按支付截止时间关闭订单（下单时投递的延迟任务）。

    幂等：如果用户已经付了，``close_order`` 的 CAS 会失败并直接返回 False。
    """
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        try:
            closed = await service.close_order(
                session, order_main_no, event=OrderEvent.TIMEOUT_CANCEL
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    if closed:
        logger.info("延迟任务关闭超时订单", extra={"orderMainNo": order_main_no})
    return {"orderMainNo": order_main_no, "closed": closed}


async def scan_timeout_orders(ctx: dict[str, Any]) -> dict[str, Any]:
    """兜底扫描超时未支付的订单。每 2 分钟一次。

    命中部分索引 ``idx_order_main_pay_deadline``（只含待付款订单），
    所以这个高频任务几乎不花代价。
    """
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        try:
            closed = await service.close_timeout_orders(session)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {"closed": closed}


async def auto_receive(ctx: dict[str, Any]) -> dict[str, Any]:
    """发货后超期自动确认收货。每 10 分钟一次。

    频率比关单低：收货是 15 天量级的期限，晚几分钟没有影响。
    """
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        try:
            done = await service.auto_receive_expired(session)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {"received": done}
