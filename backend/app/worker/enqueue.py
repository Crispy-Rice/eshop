"""向 ARQ 投递任务。

API 进程（非 worker）需要投递延迟任务时用它 —— 目前只有下单时投递
「按支付截止时间关闭订单」。worker 侧不需要这个模块。

**投递失败不是致命错误**：真正保证超时关单的是每 2 分钟一次的兜底扫描
（``trade.tasks.scan_timeout_orders``）。延迟任务只是让关单更准点 ——
用户不会在超时后还看到"还剩 3 分钟"。
"""

from __future__ import annotations

from datetime import datetime

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 进程内单例。ARQ 的连接池本身就是复用的，不必每次新建
_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue_at(job_name: str, *args: object, run_at: datetime) -> bool:
    """在指定时刻投递任务。返回是否投递成功。

    ``_job_id`` 由 ARQ 按函数名+参数推导；同一订单重复投递会被去重，
    正好符合"同一个订单只需要一个关单任务"。
    """
    try:
        pool = await get_pool()
        await pool.enqueue_job(job_name, *args, _defer_until=run_at)
    except Exception:
        # 兜底扫描会补上，这里只记日志不打断下单
        logger.warning("投递延迟任务失败，将由定时扫描兜底", exc_info=True)
        return False
    return True
