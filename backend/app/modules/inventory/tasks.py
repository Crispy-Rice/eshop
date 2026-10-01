"""inventory 的定时任务：对账、重建、分区维护。

准时注册在 ``app/worker/main.py`` 的 ``CRON_JOBS``。启动方式::

    cd backend && uv run arq app.worker.main.WorkerSettings

对账是三层防线的**第三层**（docs/03 §4.1）：前两层（Redis 闸门、DB 条件更新）
保证正常情况下不出错，这一层负责把已经发生的漂移修回来。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.modules.core.models import OpsAlert
from app.modules.inventory import redis_stock
from app.modules.inventory.models import SkuStock

logger = get_logger(__name__)

# 单次对账发现多少条漂移就升级为 P1
DRIFT_ALERT_THRESHOLD = 10


async def reconcile_stock(ctx: dict[str, Any]) -> dict[str, Any]:
    """抽样比对 Redis 与 DB，以 **DB 为准**修正（docs/03 §7）。

    方向规则：Redis 大于 DB 会超卖（危险），小于 DB 会少卖（用户看到售罄
    但实际有货）——**两种都以 DB 为准**，因为 DB 才是账本。

    抽样而不是全量：全表扫描在百万行时太贵，而漂移通常是局部的。
    每日的低峰期会做一次全量重建兜底（``rebuild_stock_daily``）。
    """
    settings = ctx["settings"]
    session_factory = ctx["session_factory"]
    redis = get_redis()

    checked = 0
    drifted: list[dict[str, Any]] = []

    async with session_factory() as session:
        rows = list(
            await session.scalars(
                select(SkuStock).order_by(func.random()).limit(settings.inventory_reconcile_sample)
            )
        )
        for row in rows:
            checked += 1
            cached = await redis_stock.read_available(redis, row.sku_id, row.warehouse_id)
            # None = 分片未加载，属于正常的惰性状态，不算漂移
            if cached is None or cached == row.available:
                continue

            drifted.append(
                {
                    "skuId": row.sku_id,
                    "warehouseId": row.warehouse_id,
                    "redis": cached,
                    "db": row.available,
                    "direction": "redis 偏多（会超卖）" if cached > row.available else "redis 偏少（会少卖）",
                }
            )
            await redis_stock.rebuild_from_db(redis, row)

        if drifted:
            session.add(
                OpsAlert(
                    level=1 if len(drifted) >= DRIFT_ALERT_THRESHOLD else 2,
                    source="inventory.reconcile",
                    title=f"库存 Redis/DB 漂移 {len(drifted)} 条，已按 DB 修正",
                    detail={"drifted": drifted[:50]},
                )
            )
        await session.commit()

    if drifted:
        logger.warning("库存对账发现漂移并已修正", extra={"count": len(drifted), "checked": checked})

    return {"checked": checked, "drifted": len(drifted)}


async def rebuild_stock_daily(ctx: dict[str, Any]) -> dict[str, Any]:
    """每日低峰期全量重建：把 DB 的 available 重新 ``SET`` 到 Redis。

    用 ``SET`` 而不是 ``INCR`` —— 重建的语义是"覆盖"，不是"再扣一次"。
    它同时清掉 Redis 里的售罄标记：DB 说有货，就不该继续返回"已售罄"。

    Redis 重启丢最后一秒写入、或某次补偿没跑成功，都由这里兜底。
    """
    session_factory = ctx["session_factory"]
    redis = get_redis()
    rebuilt = 0

    async with session_factory() as session:
        rows = list(await session.scalars(select(SkuStock)))
        for row in rows:
            await redis_stock.rebuild_from_db(redis, row)
            rebuilt += 1

    logger.info("库存全量重建完成", extra={"rebuilt": rebuilt})
    return {"rebuilt": rebuilt}


async def ensure_flow_partition(ctx: dict[str, Any]) -> dict[str, Any]:
    """提前建好未来几个月的 ``stock_flow`` 分区。

    每月 25 日跑，建下个月（以及再往后两个月，防止某次任务没跑）。
    没有对应的分区时，往 ``stock_flow`` 插数据会直接报
    "no partition of relation ... found for row" —— 所以这个任务不能断。
    """
    from sqlalchemy import text

    from app.core.db import get_session_factory

    created: list[str] = []
    today = datetime.now(UTC)
    year, month = today.year, today.month

    async with get_session_factory()() as session:
        for offset in range(1, 4):
            target_year = year + (month - 1 + offset) // 12
            target_month = (month - 1 + offset) % 12 + 1
            suffix = f"{target_year:04d}{target_month:02d}"
            start, end = _month_bounds(target_year, target_month)

            exists = await session.scalar(
                text("SELECT 1 FROM pg_class WHERE relname = :name"),
                {"name": f"stock_flow_{suffix}"},
            )
            if exists:
                continue

            await session.execute(
                text(
                    f"CREATE TABLE inventory.stock_flow_{suffix} "
                    f"PARTITION OF inventory.stock_flow "
                    f"FOR VALUES FROM ('{start}') TO ('{end}')"
                )
            )
            created.append(f"stock_flow_{suffix}")
        await session.commit()

    if created:
        logger.info("已创建库存流水分区", extra={"partitions": created})
    return {"created": created}


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """返回某月的 ``[起, 止)`` 边界，止取的是下月 1 日。"""
    end_year = year + 1 if month == 12 else year
    end_month = 1 if month == 12 else month + 1
    return f"{year:04d}-{month:02d}-01", f"{end_year:04d}-{end_month:02d}-01"
