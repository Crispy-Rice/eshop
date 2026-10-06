"""ARQ Worker 配置与启动入口。

启动：
    cd backend && uv run arq app.worker.main.WorkerSettings

生产容器里由 compose 用同样的命令起一个独立的 worker 服务。
"""

from __future__ import annotations

from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.db import dispose_engine, get_session_factory
from app.core.logging import get_logger, setup_logging
from app.core.redis import close_redis, get_redis
from app.core.snowflake import start_snowflake, stop_snowflake
from app.modules.aftersale.tasks import (
    execute_refund,
    process_refund_timeouts,
    reconcile_refunds,
    retry_refunds,
)
from app.modules.inventory.tasks import (
    ensure_flow_partition,
    rebuild_stock_daily,
    reconcile_stock,
)
from app.modules.promotion.tasks import (
    expire_coupons,
    reconcile_coupons,
    refresh_promo_status,
    unlock_stale_coupons,
)
from app.modules.review.tasks import recompute_spu_stats
from app.modules.trade.tasks import (
    auto_receive,
    close_order_if_unpaid,
    scan_timeout_orders,
)
from app.worker.outbox_delivery import deliver_outbox

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
#   from app.modules.aftersale.tasks import ...
FUNCTIONS: list[Any] = [
    ping,
    reconcile_stock,
    rebuild_stock_daily,
    ensure_flow_partition,
    expire_coupons,
    reconcile_coupons,
    refresh_promo_status,
    unlock_stale_coupons,
    # 延迟任务：下单时按 pay_deadline 投递
    close_order_if_unpaid,
    scan_timeout_orders,
    auto_receive,
    # 售后：延迟任务（进"退款中"时投递）+ 三个定时任务
    execute_refund,
    retry_refunds,
    process_refund_timeouts,
    reconcile_refunds,
    # 评价：统计全量重算
    recompute_spu_stats,
    # outbox 投递：把 core.local_message 里积压的事件就地分派出去
    # （cron 每 5 秒叫一次，这里也登记一份，便于手工 enqueue 一次）
    deliver_outbox,
]

# 定时任务。
#   unique=True 保证多副本部署时同一时刻只有一个 worker 跑（否则两边的对账
#   会同时改同一个 sku 的 Redis 值，虽然结果相同但白费资源）。
CRON_JOBS: list[Any] = [
    # ---------- 库存 ----------
    # 漂移要尽快发现，所以 5 分钟一次（docs/03 §7）
    cron(reconcile_stock, minute=set(range(0, 60, 5)), second=7, unique=True),
    # 全量重建：低峰期，每日 04:10
    cron(rebuild_stock_daily, hour=4, minute=10, second=0, unique=True),
    # 建下月分区：每月 25 日，留足提前量
    cron(ensure_flow_partition, day=25, hour=3, minute=20, second=0, unique=True),
    # ---------- 促销 ----------
    # 券对账：比库存更敏感（涉及资金），10 分钟一次（docs/04 §10）
    cron(reconcile_coupons, minute=set(range(1, 60, 10)), second=17, unique=True),
    # 券过期：每小时扫一次即可，过期不是实时性要求高的事
    cron(expire_coupons, minute=23, second=0, unique=True),
    # 僵尸锁：券被锁超 40 分钟未释放，10 分钟扫一次
    cron(unlock_stale_coupons, minute=set(range(4, 60, 10)), second=37, unique=True),
    # 活动与券模板的状态推进：1 分钟一次。整点开始的活动迟一分钟才开始，
    # 就等于运营写的"零点开抢"第一天是假的 —— 与售后超时同样的理由。
    # 代价可忽略：两条"只在真的变化时才写"的批量 UPDATE，量级几十到几百行。
    cron(refresh_promo_status, minute=set(range(0, 60)), second=43, unique=True),
    # ---------- 订单 ----------
    # 超时关单兜底：延迟任务可能丢，2 分钟扫一次（命中部分索引，代价极低）
    cron(scan_timeout_orders, minute=set(range(0, 60, 2)), second=11, unique=True),
    # 自动确认收货：15 天量级的期限，10 分钟一次足够
    cron(auto_receive, minute=set(range(6, 60, 10)), second=47, unique=True),
    # ---------- 售后 ----------
    # 超时处理：四个环节共用一个 deadline，1 分钟一次才能保证时限"准点"
    cron(process_refund_timeouts, minute=set(range(0, 60, 1)), second=23, unique=True),
    # 退款重试兜底：即时投递可能丢，1 分钟一次（命中部分索引，代价极低）
    cron(retry_refunds, minute=set(range(2, 60, 3)), second=33, unique=True),
    # 资金对账：低峰期每日一次，有问题写 P0 告警
    cron(reconcile_refunds, hour=4, minute=40, second=0, unique=True),
    # ---------- 评价 ----------
    # 统计全量重算：增量靠业务代码，正确性靠它，低峰期每日一次
    cron(recompute_spu_stats, hour=3, minute=40, second=0, unique=True),
    # ---------- outbox 投递 ----------
    # 每 5 秒一轮。**只对"新积压"敏感**，所以可以稀疏；之所以不是每分钟：
    # 用户支付完马上就会去点消息中心，站内信迟到几秒是能感觉到的。
    # ★ minute=None 而不是省略 —— ARQ 的 cron 把省略的 minute 当成 0（只在
    #   每小时的 0 分跑），不是"每分钟"。省略它就变成一小时一次了。
    cron(deliver_outbox, minute=None, second=set(range(0, 60, 5)), unique=True),
]


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
