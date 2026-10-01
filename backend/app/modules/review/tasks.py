"""review 的定时任务：评价统计的全量重算。

**增量靠业务代码，正确性靠这个任务。** 每次评价发布/屏蔽时都会在同一事务里
增减 ``product.spu`` 的三个计数（快、实时），但只要有任何一个地方漏调或重调，
计数就会漂 — 而漂了的统计会直接展示给用户（"好评率 98%"）。所以每天低峰期
按 ``review.review`` 重算一遍，把漂移拉回来。

**跨 schema 写这一处是刻意的例外**：docs/12 §3.2 明确把"统计修正"列为
运维性质的对账任务，允许它读写 product 的表。业务代码不走这条路 ——
业务侧统一经 ``product.service.apply_review_stat_delta``。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.logging import get_logger
from app.modules.core.models import OpsAlert

logger = get_logger(__name__)

# ① 有已发布首评的 SPU：把四个统计字段校正到聚合值。
#    只更新**有偏差的行** —— 否则每天全表重写一遍，白白产生 WAL 与死元组
_FIX_SQL = text(
    """
    UPDATE product.spu s
    SET review_count      = r.cnt,
        review_score_sum  = r.score_sum,
        good_review_count = r.good_cnt,
        avg_score         = ROUND(r.score_sum::numeric / r.cnt, 2),
        updated_at        = now()
    FROM (
        SELECT spu_id,
               COUNT(*)                           AS cnt,
               SUM(score)                         AS score_sum,
               COUNT(*) FILTER (WHERE score >= 4) AS good_cnt
        FROM review.review
        WHERE status = 1 AND NOT is_follow_up
        GROUP BY spu_id
    ) r
    WHERE s.id = r.spu_id
      AND (s.review_count, s.review_score_sum, s.good_review_count)
          IS DISTINCT FROM (r.cnt, r.score_sum, r.good_cnt)
    """
)

# ② 一条已发布首评都没有、但计数非零的 SPU：归零。
#    上面那条 SQL 靠 FROM 连接，覆盖不到"聚合里根本没有这个 spu"的行 ——
#    而那正是最危险的一类漂移（评价被全部屏蔽/驳回后计数没减回去）
_ZERO_SQL = text(
    """
    UPDATE product.spu s
    SET review_count      = 0,
        review_score_sum  = 0,
        good_review_count = 0,
        avg_score         = NULL,
        updated_at        = now()
    WHERE (s.review_count <> 0 OR s.review_score_sum <> 0 OR s.good_review_count <> 0)
      AND NOT EXISTS (
          SELECT 1 FROM review.review r
          WHERE r.spu_id = s.id AND r.status = 1 AND NOT r.is_follow_up
      )
    """
)


async def recompute_spu_stats(ctx: dict[str, Any]) -> dict[str, Any]:
    """每日全量重算 SPU 的评价统计。有偏差就修正并写告警。"""
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        try:
            fixed = (await session.execute(_FIX_SQL)).rowcount or 0
            zeroed = (await session.execute(_ZERO_SQL)).rowcount or 0
            drift = int(fixed) + int(zeroed)
            if drift:
                # 漂移说明业务代码里有漏调 delta 的地方，值得看一眼
                session.add(
                    OpsAlert(
                        level=2,
                        source="review.stats_reconcile",
                        title=f"评价统计发现 {drift} 个商品计数漂移，已按明细重算",
                        detail={"fixed": int(fixed), "zeroed": int(zeroed)},
                    )
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    if drift:
        logger.warning("评价统计重算完成（有漂移）", extra={"fixed": fixed, "zeroed": zeroed})
    else:
        logger.info("评价统计重算完成（无漂移）")
    return {"fixed": int(fixed), "zeroed": int(zeroed)}
