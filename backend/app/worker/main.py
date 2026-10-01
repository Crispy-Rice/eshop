"""ARQ Worker 配置与启动入口。

启动：
    cd backend && uv run arq app.worker.main.WorkerSettings

生产容器里由 compose 用同样的命令起一个独立的 worker 服务。
"""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.db import dispose_engine, get_session_factory
from app.core.logging import get_logger, setup_logging
from app.core.redis import close_redis, get_redis
from app.core.snowflake import start_snowflake, stop_snowflake

logger = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info("worker 启动中", extra={"env": settings.app_env, "version": settings.app_version})

    # 放进 ctx，任务函数通过 ctx["session_factory"] 拿会话
    ctx["settings"] = settings
    ctx["session_factory"] = get_session_factory()

    # worker 也会发号（退款单号、流水 ID 等），所以同样要租 worker id
    await start_snowflake(get_redis())

    logger.info("worker 已就绪")


async def shutdown(ctx: dict[str, Any]) -> None:
    logger.info("worker 关闭中")
    await stop_snowflake()
    await close_redis()
    await dispose_engine()
    logger.info("worker 已关闭")


async def ping(ctx: dict[str, Any]) -> str:
    """连通性自检：enqueue_job("ping") 有返回说明 worker 在线。"""
    return "pong"


# 任务函数注册表。模块实现后在这里追加：
#   from app.modules.trade.tasks import close_order_if_unpaid, scan_timeout_orders
FUNCTIONS: list[Any] = [
    ping,
]

# 定时任务。模块实现后追加，例如：
#   cron(scan_timeout_orders, minute=set(range(0, 60, 2)), unique=True)   每 2 分钟扫超时订单
#   cron(process_refund_timeouts, minute=None, second=0, unique=True)     每分钟扫售后超时
#   cron(daily_reconcile, hour=2, minute=10, unique=True)                 每日对账
CRON_JOBS: list[Any] = []


class WorkerSettings:
    functions = FUNCTIONS
    cron_jobs = CRON_JOBS

    on_startup = startup
    on_shutdown = shutdown

    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)

    max_jobs = 10
    job_timeout = 300  # 5 分钟，对账类任务要更长的在函数上单独声明
    keep_result = 3600
    max_tries = 5
    health_check_interval = 30

    # 任务失败时重试的退避由 ARQ 负责；最终仍失败的进入死信并告警
    # （docs/14-redis-keys.md §6）
