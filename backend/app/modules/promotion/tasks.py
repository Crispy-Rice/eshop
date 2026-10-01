"""promotion 的定时任务：券过期、对账、僵尸锁清理。

准时注册在 ``app/worker/main.py`` 的 ``CRON_JOBS``。

对账是券体系里最重要的一环（docs/04 §10）：Redis 是第一道防线，
但它会漂移（重启丢写入、补偿没跑成）。**DB 是账本**，对账以 DB 为准修正 Redis。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core import redis_keys as rk
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.modules.core.models import OpsAlert
from app.modules.promotion import repository as repo
from app.modules.promotion.models import CODE_UNUSED, CouponCode

logger = get_logger(__name__)

# 单轮对账差多少条就升级为 P1 告警
RECONCILE_ALERT_THRESHOLD = 10


async def expire_coupons(ctx: dict[str, Any]) -> dict[str, Any]:
    """把已过期的"未使用"券置为已过期（docs/04 §9）。

    分批 + ``FOR UPDATE SKIP LOCKED``：与下单锁券并发时跳过正在被锁的行，
    互不阻塞。命中部分索引，只扫未使用的券，不全表扫。
    """
    session_factory = ctx["session_factory"]
    total = 0

    async with session_factory() as session:
        # 循环直到没有可过期的，避免单次大事务
        while True:
            n = await repo.expire_codes(session, batch=1000)
            await session.commit()
            total += n
            if n < 1000:
                break

    if total:
        logger.info("已过期券处理完成", extra={"count": total})
    return {"expired": total}


async def reconcile_coupons(ctx: dict[str, Any]) -> dict[str, Any]:
    """券的模板级对账（docs/04 §10）。

    比对「DB 已发出的券实例数」与「Redis 的剩余额度推出的已发数」，
    不一致就以 **DB 为准**重建 Redis 计数。

    同时做**超发检测**：``issued_count > total_count`` 有结果就是 P0 ——
    正常情况下 CHECK 约束保证不可能出现，保留它是为了发现"约束被误删"。
    """
    session_factory = ctx["session_factory"]
    redis = get_redis()
    checked = 0
    drifted: list[dict[str, Any]] = []

    async with session_factory() as session:
        # ① 超发检测（最重要）
        overissued = await repo.find_overissued_templates(session)
        if overissued:
            session.add(
                OpsAlert(
                    level=1,
                    source="promotion.reconcile",
                    title=f"发现 {len(overissued)} 个券模板超发",
                    detail={
                        "templates": [
                            {
                                "id": str(t.id),
                                "name": t.name,
                                "issued": t.issued_count,
                                "total": t.total_count,
                            }
                            for t in overissued
                        ]
                    },
                )
            )
            await session.commit()
            logger.error("券模板超发！", extra={"count": len(overissued)})

        # ② 抽样比对模板级计数
        from app.modules.promotion.models import CouponTemplate

        templates = list(await session.scalars(select(CouponTemplate).limit(200)))
        for tpl in templates:
            checked += 1
            # Redis 说还剩多少
            cached_stock = await redis.get(rk.coupon_tpl_stock(tpl.id))
            if cached_stock is None:
                continue  # 没预热，不是漂移
            redis_issued = tpl.total_count - int(cached_stock)
            if redis_issued == tpl.issued_count:
                continue

            drifted.append(
                {
                    "templateId": str(tpl.id),
                    "name": tpl.name,
                    "redis": redis_issued,
                    "db": tpl.issued_count,
                }
            )
            # ★ 以 DB 为准重建
            await redis.set(
                rk.coupon_tpl_stock(tpl.id), max(0, tpl.total_count - tpl.issued_count)
            )
            # 用户限领计数也一并重建（对齐流水里的实际领取数）
            await _rebuild_user_counts(session, redis, tpl.id)

        if drifted:
            session.add(
                OpsAlert(
                    level=1 if len(drifted) >= RECONCILE_ALERT_THRESHOLD else 2,
                    source="promotion.reconcile",
                    title=f"券模板计数漂移 {len(drifted)} 条，已按 DB 修正",
                    detail={"drifted": drifted[:50]},
                )
            )
        await session.commit()

    if drifted:
        logger.warning("券对账发现漂移并已修正", extra={"count": len(drifted), "checked": checked})
    return {"checked": checked, "drifted": len(drifted)}


async def _rebuild_user_counts(session: Any, redis: Any, tpl_id: int) -> None:
    """按 DB 重建某模板的用户限领计数。"""
    rows = await session.execute(
        select(CouponCode.user_id, func.count())
        .where(CouponCode.coupon_template_id == tpl_id)
        .group_by(CouponCode.user_id)
    )
    key = rk.coupon_tpl_user_count(tpl_id)
    await redis.delete(key)
    mapping = {str(uid): int(n) for uid, n in rows}
    if mapping:
        await redis.hset(key, mapping=mapping)
        # 计数不该比模板缓存活得更久，否则会留下没人清理的孤儿 key
        await redis.expire(key, 3600)


async def unlock_stale_coupons(ctx: dict[str, Any]) -> dict[str, Any]:
    """清理僵尸锁（docs/04 §11）。

    正常流程里关单会显式解锁，这里兜的是"关单流程本身失败了"：
    券被锁超过 40 分钟（订单早就超时关闭），但状态还是已锁定。
    """
    session_factory = ctx["session_factory"]
    redis = get_redis()
    unlocked = 0

    async with session_factory() as session:
        stale = await repo.find_stale_locks(session, minutes=40)
        for code in stale:
            code.status = CODE_UNUSED
            code.locked_order_no = None
            code.locked_at = None
            unlocked += 1
            # Redis 缓存同步清掉锁定信息
            key = rk.coupon_code(code.id)
            if await redis.exists(key):
                await redis.hset(key, "status", "UNUSED")
                await redis.hdel(key, "orderNo")
        if unlocked:
            session.add(
                OpsAlert(
                    level=2,
                    source="promotion.unlock_stale",
                    title=f"强制解锁 {unlocked} 张僵尸券",
                    detail={"count": unlocked},
                )
            )
        await session.commit()

    if unlocked:
        logger.warning("清理了僵尸锁定的券", extra={"count": unlocked})
    return {"unlocked": unlocked}
