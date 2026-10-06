"""promotion 模块的数据访问层。

只读写 ``promotion`` schema 下的表。

**超发的第二道防线就在这一层**：领券的三个写操作全部是**条件更新**，
任何一步 ``rowcount == 0`` 就说明超发或超限，抛异常让整个事务回滚
（docs/04 §5.2）。Redis 是第一道，这里是第二道 —— Redis 挂了也不会超发。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.promotion.models import (
    BANNER_ENABLED,
    CODE_LOCKED,
    ISSUE_BIZ_KEY_PREFIX,
    Banner,
    CouponCode,
    CouponFlow,
    CouponTemplate,
    CouponUserQuota,
    PromoActivity,
    PromoStackRule,
)


# ============================================================
# 券模板
# ============================================================
async def get_template(session: AsyncSession, tpl_id: int) -> CouponTemplate | None:
    return await session.get(CouponTemplate, tpl_id)


async def list_receivable_templates(session: AsyncSession, *, now: datetime) -> list[CouponTemplate]:
    """券中心：可以主动领取、且还在进行中的券。

    ``get_type=1``（主动领取）才出现在券中心 —— 系统发放、兑换码那几种不该被领。
    """
    result = await session.scalars(
        select(CouponTemplate)
        .where(
            CouponTemplate.get_type == 1,
            CouponTemplate.status == 2,
            CouponTemplate.valid_end.isnot(None),
            CouponTemplate.valid_end > now,
        )
        .order_by(CouponTemplate.threshold, CouponTemplate.id.desc())
    )
    return list(result)


async def list_templates_by_ids(
    session: AsyncSession, tpl_ids: Sequence[int]
) -> list[CouponTemplate]:
    if not tpl_ids:
        return []
    return list(await session.scalars(select(CouponTemplate).where(CouponTemplate.id.in_(tpl_ids))))


async def list_admin_templates(
    session: AsyncSession,
    *,
    status: int | None,
    cursor: int | None,
    limit: int,
) -> list[CouponTemplate]:
    """运营列表：全部券模板，最新在前（不是只有可领取的那些）。

    ★ 多取一条：调用方用"返回条数 > limit"判断 ``hasMore``，
    不用额外跑一次 count，也不会在正好取满时误判成还有下一页。
    """
    stmt: Select = select(CouponTemplate)
    if status is not None:
        stmt = stmt.where(CouponTemplate.status == status)
    if cursor is not None:
        stmt = stmt.where(CouponTemplate.id < cursor)
    stmt = stmt.order_by(CouponTemplate.id.desc()).limit(limit + 1)
    return list(await session.scalars(stmt))


# ============================================================
# 促销活动（运营）
# ============================================================
async def list_admin_activities(
    session: AsyncSession,
    *,
    status: int | None,
    cursor: int | None,
    limit: int,
) -> list[PromoActivity]:
    """运营列表：全部活动，最新在前。多取一条的约定同 ``list_admin_templates``。"""
    stmt: Select = select(PromoActivity)
    if status is not None:
        stmt = stmt.where(PromoActivity.status == status)
    if cursor is not None:
        stmt = stmt.where(PromoActivity.id < cursor)
    stmt = stmt.order_by(PromoActivity.id.desc()).limit(limit + 1)
    return list(await session.scalars(stmt))


async def insert_template(session: AsyncSession, tpl: CouponTemplate) -> CouponTemplate:
    session.add(tpl)
    await session.flush()
    return tpl


async def get_activity(session: AsyncSession, activity_id: int) -> PromoActivity | None:
    return await session.get(PromoActivity, activity_id)


async def claim_template_quota(session: AsyncSession, tpl_id: int) -> bool:
    """模板总量 +1。**条件是超发的第一道 DB 防线**。

    三个条件缺一不可：
    - ``issued_count < total_count`` —— 不超发
    - ``status = 2`` —— 只发进行中的
    - ``valid_end > now()`` —— 不发票已过期的
    """
    result = await session.execute(
        text(
            "UPDATE promotion.coupon_template "
            "SET issued_count = issued_count + 1, updated_at = now() "
            "WHERE id = :tpl_id AND issued_count < total_count "
            "  AND status = 2 AND (valid_end IS NULL OR valid_end > now())"
        ),
        {"tpl_id": tpl_id},
    )
    return result.rowcount > 0


async def claim_user_quota(session: AsyncSession, tpl_id: int, user_id: int, limit: int) -> bool:
    """用户限领 +1。条件是**限领的第二道 DB 防线**。

    用 upsert + ``WHERE received < limit``：超限时 ``rowcount = 0``。
    限领 1 张和 N 张走同一段代码 —— 这是单独建计数表而不是靠唯一索引的原因
    （唯一索引在限领 N 张时会误拦，docs/04 §5.2）。
    """
    stmt = pg_insert(CouponUserQuota).values(
        coupon_template_id=tpl_id, user_id=user_id, received=1
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["coupon_template_id", "user_id"],
        set_={"received": CouponUserQuota.received + 1},
        where=CouponUserQuota.received < limit,
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


# ============================================================
# 券实例
# ============================================================
async def get_code(session: AsyncSession, code_id: int) -> CouponCode | None:
    return await session.get(CouponCode, code_id)


async def list_user_codes(
    session: AsyncSession, user_id: int, *, status: int | None = None
) -> list[CouponCode]:
    stmt: Select = select(CouponCode).where(CouponCode.user_id == user_id)
    if status is not None:
        stmt = stmt.where(CouponCode.status == status)
    # 未使用的排前面，同状态按到期时间升序（快过期的先用）
    stmt = stmt.order_by(CouponCode.status, CouponCode.valid_end)
    return list(await session.scalars(stmt))


async def list_user_codes_by_ids(
    session: AsyncSession, user_id: int, code_ids: Sequence[int]
) -> list[CouponCode]:
    if not code_ids:
        return []
    return list(
        await session.scalars(
            select(CouponCode).where(
                CouponCode.user_id == user_id, CouponCode.id.in_(code_ids)
            )
        )
    )


async def insert_code(session: AsyncSession, code: CouponCode) -> CouponCode:
    session.add(code)
    await session.flush()
    return code


async def count_codes_of_template(session: AsyncSession, tpl_id: int) -> int:
    """该模板已发出的券实例数。对账用（与 ``issued_count`` 比对）。"""
    return int(
        await session.scalar(
            select(func.count()).select_from(CouponCode).where(
                CouponCode.coupon_template_id == tpl_id
            )
        )
        or 0
    )


async def lock_code(
    session: AsyncSession, code_id: int, user_id: int, order_main_no: str
) -> bool:
    """锁券（下单占用）。

    ★ 条件全部写进 ``WHERE``，不是"先查再改" —— 并发下两个订单同时锁同一张券，
    只有一个的 ``rowcount`` 是 1。
    这个 UPDATE 与订单写入在**同一个事务**里，订单落库失败时锁自动回滚，
    不需要额外的解锁补偿（docs/04 §6）。
    """
    result = await session.execute(
        text(
            "UPDATE promotion.coupon_code "
            "SET status = 2, locked_order_no = :order_no, locked_at = now() "
            "WHERE id = :code_id AND user_id = :user_id "
            "  AND status = 1 "
            "  AND valid_start <= now() AND valid_end >= now()"
        ),
        {"code_id": code_id, "user_id": user_id, "order_no": order_main_no},
    )
    return result.rowcount > 0


async def list_locked_codes_of_order(
    session: AsyncSession, order_main_no: str
) -> list[CouponCode]:
    """这张订单锁住的券。

    关单要解锁、支付成功要核销，都得先找到它们 —— 订单上只存了"用了哪几张券"
    的金额，没有券 id，所以按 ``locked_order_no`` 反查。
    """
    return list(
        await session.scalars(
            select(CouponCode).where(
                CouponCode.locked_order_no == order_main_no, CouponCode.status == CODE_LOCKED
            )
        )
    )


async def unlock_code(session: AsyncSession, code_id: int, user_id: int) -> bool:
    """解锁（订单取消 / 超时 / 支付失败）。条件是 ``status = 2``。"""
    result = await session.execute(
        update(CouponCode)
        .where(CouponCode.id == code_id, CouponCode.user_id == user_id, CouponCode.status == 2)
        .values(status=1, locked_order_no=None, locked_at=None)
    )
    return result.rowcount > 0


async def use_code(
    session: AsyncSession, code_id: int, user_id: int, order_main_no: str, use_amount: int
) -> bool:
    """核销（支付成功）。条件是 ``status = 2``（必须先被这张单锁住）。"""
    result = await session.execute(
        text(
            "UPDATE promotion.coupon_code "
            "SET status = 3, used_order_no = :order_no, used_at = now(), "
            "    use_amount = :amount, locked_order_no = NULL "
            "WHERE id = :code_id AND user_id = :user_id AND status = 2 "
            "  AND locked_order_no = :order_no"
        ),
        {
            "code_id": code_id,
            "user_id": user_id,
            "order_no": order_main_no,
            "amount": use_amount,
        },
    )
    return result.rowcount > 0


async def refund_code(session: AsyncSession, code_id: int, user_id: int) -> bool:
    """整单退款后把券退回。条件是 ``status = 3``（已使用才能退）。

    退回到"未使用"而不是新建一张 —— 券的有效期不变（过期了就不能再用，
    由 ``valid_end`` 判定）。
    """
    result = await session.execute(
        update(CouponCode)
        .where(CouponCode.id == code_id, CouponCode.user_id == user_id, CouponCode.status == 3)
        .values(status=1, used_order_no=None, used_at=None)
    )
    return result.rowcount > 0


async def bump_template_used_count(session: AsyncSession, tpl_id: int) -> None:
    await session.execute(
        update(CouponTemplate)
        .where(CouponTemplate.id == tpl_id)
        .values(used_count=CouponTemplate.used_count + 1, updated_at=func.now())
    )


async def expire_codes(session: AsyncSession, *, batch: int = 1000) -> int:
    """把已过期的未使用券置为过期（docs/04 §9）。

    分批 + ``FOR UPDATE SKIP LOCKED``：与下单锁券并发时跳过正在被锁的行，
    不会互相阻塞。命中部分索引 ``idx_coupon_code_expire``，不全表扫。
    """
    result = await session.execute(
        text(
            "UPDATE promotion.coupon_code SET status = 4 "
            "WHERE id IN ("
            "  SELECT id FROM promotion.coupon_code "
            "  WHERE status = 1 AND valid_end < now() "
            "  ORDER BY valid_end LIMIT :batch FOR UPDATE SKIP LOCKED"
            ")"
        ),
        {"batch": batch},
    )
    return result.rowcount


async def find_stale_locks(session: AsyncSession, *, minutes: int = 40) -> list[CouponCode]:
    """找出被锁太久没释放的券（订单早没了，锁没解）。

    正常流程里关单会显式解锁；这里兜的是"关单流程本身失败了"。
    """
    return list(
        await session.scalars(
            select(CouponCode).where(
                CouponCode.status == 2,
                CouponCode.locked_at.isnot(None),
                CouponCode.locked_at < func.now() - text(f"interval '{minutes} minutes'"),
            )
        )
    )


async def find_overissued_templates(session: AsyncSession) -> list[CouponTemplate]:
    """超发检测（docs/04 §10 ④，最重要的一条）。

    有 ``CHECK`` 约束在，正常情况下**不可能**有结果。保留它是为了发现
    "约束被误删"或"数据被绕过应用直接修改"。
    """
    return list(
        await session.scalars(
            select(CouponTemplate).where(CouponTemplate.issued_count > CouponTemplate.total_count)
        )
    )


# ============================================================
# 券流水
# ============================================================
async def try_insert_flow(session: AsyncSession, flow: CouponFlow) -> bool:
    """写一条券状态流水，**顺便完成幂等占键**。返回 True 表示首次处理。

    一条 INSERT 同时做两件事：``coupon_flow.biz_key`` 上的 UNIQUE 约束
    让重复的转换在这里被挡住（``rowcount == 0``），不需要再单独建幂等键表 ——
    这与 inventory 的 `stock_flow` + `stock_biz_key` 是同一个模式，
    那边的两张表是因为流水表要分区、分区表的唯一约束必须包含分区键（docs/03 §8）。
    券流水不分区，所以一张表就够了。
    """
    stmt = pg_insert(CouponFlow).values(
        coupon_code_id=flow.coupon_code_id,
        from_status=flow.from_status,
        to_status=flow.to_status,
        order_no=flow.order_no,
        # ★ 这里必须显式兜底：Core 的 insert 不经过 ORM，模型的 Python 默认值不生效。
        #   调用方不传 use_amount 时属性是 None，会直接撞非空约束。
        use_amount=flow.use_amount or 0,
        biz_key=flow.biz_key,
        operator=flow.operator,
        remark=flow.remark,
    )
    result = await session.execute(stmt.on_conflict_do_nothing(index_elements=["biz_key"]))
    return result.rowcount == 1


async def list_issue_records(
    session: AsyncSession, *, cursor: int | None, limit: int
) -> list[tuple[CouponFlow, CouponCode, CouponTemplate]]:
    """客服补发的流水，最新在前。

    **一行 = 一张券**（``biz_key`` 是 ``ISSUE:{code_id}``，一次发 5 张就是 5 行）——
      审计记录本来就该逐条，这样能顺着查到具体那张券码。

    ★ 三表一次 join 取全：流水给出"谁在何时发的"，券码给出"发给谁"，
      模板给出"发的是哪张券"。分三次查会让列表页变成 N+1。

    ★ 多取一条：调用方用"返回条数 > limit"判断还有没有下一页，不用额外跑 count。
    """
    stmt: Select = (
        select(CouponFlow, CouponCode, CouponTemplate)
        .join(CouponCode, CouponCode.id == CouponFlow.coupon_code_id)
        .join(CouponTemplate, CouponTemplate.id == CouponCode.coupon_template_id)
        .where(CouponFlow.biz_key.startswith(ISSUE_BIZ_KEY_PREFIX))
        .order_by(CouponFlow.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        stmt = stmt.where(CouponFlow.id < cursor)
    rows = await session.execute(stmt)
    return [(flow, code, tpl) for flow, code, tpl in rows.all()]


async def count_issues_for_user_template(
    session: AsyncSession, *, user_id: int, tpl_id: int
) -> int:
    """这个用户在**这个模板**上累计被补发过多少张。

    ★ 不限时间窗：要挡的是"给一个小号反复刷"，那是累积行为 ——
      加个时间窗反而能隔天绕过去。
    """
    total = await session.scalar(
        select(func.count())
        .select_from(CouponFlow)
        .join(CouponCode, CouponCode.id == CouponFlow.coupon_code_id)
        .where(
            CouponFlow.biz_key.startswith(ISSUE_BIZ_KEY_PREFIX),
            CouponCode.user_id == user_id,
            CouponCode.coupon_template_id == tpl_id,
        )
    )
    return int(total or 0)


async def count_issues_since(session: AsyncSession, *, operator: str, since: datetime) -> int:
    """某个运营从 ``since`` 起一共补发了多少张。"""
    total = await session.scalar(
        select(func.count())
        .select_from(CouponFlow)
        .where(
            CouponFlow.biz_key.startswith(ISSUE_BIZ_KEY_PREFIX),
            CouponFlow.operator == operator,
            CouponFlow.created_at >= since,
        )
    )
    return int(total or 0)


async def issue_summary_since(
    session: AsyncSession, *, since: datetime
) -> list[tuple[str | None, int]]:
    """从 ``since`` 起按操作人分组的补发张数，多的在前。"""
    rows = await session.execute(
        select(CouponFlow.operator, func.count())
        .where(
            CouponFlow.biz_key.startswith(ISSUE_BIZ_KEY_PREFIX),
            CouponFlow.created_at >= since,
        )
        .group_by(CouponFlow.operator)
        .order_by(func.count().desc())
    )
    return [(operator, int(n)) for operator, n in rows]


# ============================================================
# 促销活动与叠加规则
# ============================================================
async def list_active_activities(
    session: AsyncSession, *, now: datetime, levels: Sequence[int] | None = None
) -> list[PromoActivity]:
    stmt: Select = select(PromoActivity).where(
        PromoActivity.status == 2,
        PromoActivity.start_at <= now,
        PromoActivity.end_at > now,
    )
    if levels is not None:
        stmt = stmt.where(PromoActivity.level.in_(levels))
    stmt = stmt.order_by(PromoActivity.level, PromoActivity.priority.desc(), PromoActivity.id)
    return list(await session.scalars(stmt))


async def refresh_activity_status(session: AsyncSession) -> int:
    """按时间窗把活动推进到"进行中 / 已结束"。返回**真正发生变化**的行数。

    ★ 为什么必须有这一步：``status`` 是建活动那一刻按当时的窗口**算一次就写死**的
      （``router.create_activity``），之后没有任何地方改它。而算价查询硬性要求
      ``status = 2``（``list_active_activities``）—— 于是**建一个"未开始"的活动，
      它永远不会开始**：过了 ``start_at`` 列表还显示"未开始"，下单也不生效。
      这个任务补上 1 → 2 → 3 的推进。

    ``status = 4``（已作废）不在 ``WHERE`` 里，所以作废过的活动不会被这里复活 ——
    这是"作废"能压过时间窗的原因。

    ★ ``AND a.status <> target.want`` 不是多余的：这条 SQL 每分钟跑一次，
      没有它就会把每个在跑的活动每分钟重写一遍，``updated_at`` 于是变成
      "最后一次跑 cron 的时间"，而不是"最后一次改配置的时间"。
    """
    result = await session.execute(
        text(
            "WITH target AS ("
            "  SELECT id, CASE WHEN end_at <= now() THEN 3 ELSE 2 END AS want"
            "    FROM promotion.promo_activity"
            "   WHERE status IN (1, 2) AND start_at <= now()"
            ") "
            "UPDATE promotion.promo_activity a "
            "   SET status = target.want, updated_at = now() "
            "  FROM target "
            " WHERE a.id = target.id AND a.status <> target.want"
        )
    )
    return result.rowcount


async def refresh_coupon_template_status(session: AsyncSession) -> int:
    """按有效期把券模板推进到"未开始 / 进行中 / 已结束"。返回**真正变化**的行数。

    ★ 和活动同一个毛病：``status`` 只在建模板那一刻写一次（而且写死成"进行中"，
      见 ``router.create_template``），之后没人改 —— 于是**过期的券在运营列表里
      永远显示"进行中"**，运营分不清"这张券还能不能领"。状态列不准不致命
      （能不能领由 ``valid_end > now()`` 和 ``claim_template_quota`` 管），
      但运营就是照着这一列判断的。

    ``status = 4``（已作废）同样不在 ``WHERE`` 里，作废的不会被复活。
    ``valid_start`` / ``valid_end`` 为 ``NULL`` 的（「领取后 N 天」型）落在
    ``ELSE`` 分支，恒为进行中 —— 它们没有统一的时间窗。
    """
    result = await session.execute(
        text(
            "WITH target AS ("
            "  SELECT id, CASE"
            "           WHEN valid_end IS NOT NULL AND valid_end <= now() THEN 3"
            "           WHEN valid_start IS NOT NULL AND valid_start > now() THEN 1"
            "           ELSE 2"
            "         END AS want"
            "    FROM promotion.coupon_template"
            "   WHERE status IN (1, 2)"
            ") "
            "UPDATE promotion.coupon_template t "
            "   SET status = target.want, updated_at = now() "
            "  FROM target "
            " WHERE t.id = target.id AND t.status <> target.want"
        )
    )
    return result.rowcount


async def insert_activity(session: AsyncSession, activity: PromoActivity) -> PromoActivity:
    session.add(activity)
    await session.flush()
    return activity


async def list_stack_rules(session: AsyncSession, *, now: datetime) -> list[PromoStackRule]:
    """取当前生效的叠加规则。"""
    return list(
        await session.scalars(
            select(PromoStackRule).where(
                (PromoStackRule.effective_from.is_(None))
                | (PromoStackRule.effective_from <= now),
                (PromoStackRule.effective_to.is_(None)) | (PromoStackRule.effective_to > now),
            )
        )
    )


# ============================================================
# 首页 Banner
# ============================================================
def _banner_order() -> tuple:
    """排序：sort 小的在前，同序按 id 保证确定性（分页与展示都不会跳）。"""
    return (Banner.sort, Banner.id)


async def list_banners(session: AsyncSession, *, only_enabled: bool = False) -> list[Banner]:
    stmt = select(Banner)
    if only_enabled:
        stmt = stmt.where(Banner.status == BANNER_ENABLED)
    return list(await session.scalars(stmt.order_by(*_banner_order())))


async def get_banner(session: AsyncSession, banner_id: int) -> Banner | None:
    return await session.get(Banner, banner_id)


async def insert_banner(session: AsyncSession, banner: Banner) -> Banner:
    session.add(banner)
    await session.flush()
    return banner


async def update_banner_fields(session: AsyncSession, banner_id: int, values: dict[str, Any]) -> None:
    if not values:
        return
    values["updated_at"] = func.now()
    await session.execute(update(Banner).where(Banner.id == banner_id).values(**values))


async def delete_banner(session: AsyncSession, banner_id: int) -> None:
    await session.execute(delete(Banner).where(Banner.id == banner_id))
