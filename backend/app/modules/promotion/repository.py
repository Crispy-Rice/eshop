"""promotion 模块的数据访问层。

只读写 ``promotion`` schema 下的表。

**超发的第二道防线就在这一层**：领券的三个写操作全部是**条件更新**，
任何一步 ``rowcount == 0`` 就说明超发或超限，抛异常让整个事务回滚
（docs/04 §5.2）。Redis 是第一道，这里是第二道 —— Redis 挂了也不会超发。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.promotion.models import (
    CODE_LOCKED,
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
