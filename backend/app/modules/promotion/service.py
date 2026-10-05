"""优惠券的领域逻辑（docs/04）。

**超发的两道防线在这里汇合**：

- 第一道 Redis Lua（``coupon_receive.lua``）：原子地检查库存 + 限领 + 扣减。
  拦住绝大多数并发请求，代价是一次网络往返。
- 第二道 DB 条件更新：``WHERE issued_count < total_count`` 和
  ``WHERE received < per_user_limit``。任何一步 ``rowcount == 0`` 就回滚整个事务。

**为什么先 Redis 后 DB**（docs/04 §5.1）：反过来会在"DB 提交成功但 Redis 扣减
失败"时超发；当前顺序最坏是**少发** —— 少发可以人工补，超发收不回来。

券的生命周期转换（锁/解/核销/退回）用 ``coupon_flow.biz_key`` 的 UNIQUE 约束
做幂等，与 inventory 的 ``stock_flow`` 同一套路。
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_keys as rk
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.redis import get_lua, get_redis
from app.core.snowflake import next_id
from app.modules.promotion import repository as repo
from app.modules.promotion.models import (
    CODE_LOCKED,
    CODE_SOURCE_MANUAL,
    CODE_SOURCE_SYSTEM,
    CODE_STATUS_TEXT,
    CODE_UNUSED,
    CODE_USED,
    ISSUE_BIZ_KEY_PREFIX,
    ISSUE_MAX_PER_OPERATOR_24H,
    ISSUE_MAX_PER_USER_TPL,
    VALID_DAYS_AFTER,
    CouponCode,
    CouponFlow,
    CouponTemplate,
    CouponUserQuota,
)

logger = get_logger(__name__)

# 幂等结果的 TTL。要覆盖"用户点了一次、网络卡住、几分钟后重试"的场景
IDEM_TTL_SECONDS = 24 * 3600
# 预热时模板缓存的 TTL。过期后脚本会返回"未预热"，由调用方重新预热
META_TTL_SECONDS = 3600


# ============================================================
# Redis 预热
# ============================================================
async def warm_template(redis: Redis, tpl: CouponTemplate) -> None:
    """把模板的计数与元信息推进 Redis。

    **必须在发券前完成**：脚本遇到未预热的模板会直接返回 ``NOT_WARMED``
    而不是"当作没库存" —— 后者会让用户看到"已抢光"，而前者能触发回源重试。
    预热是幂等的：``stock`` 用 ``SETNX`` 只在缺失时写，不会把已经发出去的额度重置回去。
    """
    if tpl.valid_end is None:
        return
    meta_key = rk.coupon_tpl_meta(tpl.id)
    pipe = redis.pipeline()
    pipe.hset(
        meta_key,
        mapping={
            "tplId": tpl.id,
            "status": tpl.status,
            "startTs": int(tpl.valid_start.timestamp()) if tpl.valid_start else 0,
            "validEnd": int(tpl.valid_end.timestamp()),
            "threshold": tpl.threshold,
            "perUserLimit": tpl.per_user_limit,
        },
    )
    pipe.expire(meta_key, META_TTL_SECONDS)
    # ★ SETNX 而不是 SET：重复预热不能把剩余额度重置回去，
    #   否则已经发出去的券会被"找回"，变成超发
    pipe.setnx(rk.coupon_tpl_stock(tpl.id), max(0, tpl.total_count - tpl.issued_count))
    await pipe.execute()


async def _ensure_warmed(redis: Redis, session: AsyncSession, tpl: CouponTemplate) -> None:
    if not await redis.exists(rk.coupon_tpl_meta(tpl.id)):
        await warm_template(redis, tpl)


async def _rollback_redis(redis: Redis, tpl_id: int, user_id: int, idem_key: str) -> None:
    """补偿：把 Lua 扣掉的额度还回去。

    DB 事务失败时调用。用 ``coupon:rollback:{idem}`` 做幂等 —— 重试不会还两次
    （还两次就会超发，那正是我们要防的）。
    """
    rollback_key = f"coupon:rollback:{idem_key}"
    if not await redis.set(rollback_key, "1", nx=True, ex=IDEM_TTL_SECONDS):
        return  # 已经补偿过
    pipe = redis.pipeline()
    pipe.incr(rk.coupon_tpl_stock(tpl_id))
    pipe.hincrby(rk.coupon_tpl_user_count(tpl_id), str(user_id), -1)
    await pipe.execute()


# ============================================================
# 领券
# ============================================================
async def receive(
    session: AsyncSession,
    *,
    user_id: int,
    tpl_id: int,
    idem_key: str,
    ip: str | None = None,
    channel: str = "web",
) -> CouponCode:
    """领取一张券。

    流程（docs/04 §5）：Redis 原子扣减 → DB 三个条件更新 → 失败则补偿回滚 Redis。
    """
    redis = get_redis()
    lua = get_lua()

    tpl = await repo.get_template(session, tpl_id)
    if tpl is None:
        raise BizError(ErrorCode.COUPON_NOT_FOUND)

    await _ensure_warmed(redis, session, tpl)

    now = int(time.time())
    end_ts = int(tpl.valid_end.timestamp()) if tpl.valid_end else 0

    code, _token = await lua.coupon_receive(
        keys=[
            rk.coupon_tpl_stock(tpl_id),
            rk.coupon_tpl_user_count(tpl_id),
            rk.coupon_tpl_idem(tpl_id, idem_key),
            rk.coupon_tpl_meta(tpl_id),
        ],
        args=[user_id, tpl.per_user_limit, idem_key, IDEM_TTL_SECONDS, now, end_ts],
    )

    if code == 1:
        raise BizError(ErrorCode.COUPON_SOLD_OUT)
    if code == 2:
        raise BizError(ErrorCode.COUPON_LIMIT_EXCEEDED, f"每人限领 {tpl.per_user_limit} 张")
    if code == 4:
        raise BizError(ErrorCode.ACTIVITY_ENDED, "活动未开始或已结束")
    if code == 5:
        # 未预热：可能是缓存刚过期，重试一次预热
        await warm_template(redis, tpl)
        raise BizError(ErrorCode.SYSTEM_BUSY, "活动太火爆，请稍后再试")
    if code == 3:
        # 幂等命中：这张券之前已经发过，找出它返回，而不是再发一张
        existing = await _find_by_idem(session, tpl_id, user_id, idem_key)
        if existing is not None:
            return existing
        # 极端情况：Redis 说发过了但 DB 没有（上次 DB 写失败且补偿也没跑）。
        # 这时不能凭空造一张券，让用户重新点一次（新的 idem_key）。
        raise BizError(ErrorCode.SYSTEM_BUSY, "上次领取未完成，请重新试一次")

    # ---------- DB 侧（第二道防线）----------
    try:
        # ① 用户限领
        if not await repo.claim_user_quota(session, tpl_id, user_id, tpl.per_user_limit):
            raise BizError(ErrorCode.COUPON_LIMIT_EXCEEDED, f"每人限领 {tpl.per_user_limit} 张")

        # ② 模板总量（含时间与状态校验，与 Redis 侧重复但必须重复）
        if not await repo.claim_template_quota(session, tpl_id):
            raise BizError(ErrorCode.COUPON_SOLD_OUT)

        # ③ 生成券实例
        code_row = await repo.insert_code(
            session, _build_code(tpl, user_id, source=CODE_SOURCE_MANUAL)
        )
    except Exception:
        # DB 失败 → 把 Redis 的额度还回去，否则这批额度凭空消失（少发）
        await _rollback_redis(redis, tpl_id, user_id, idem_key)
        raise

    # ④ 领取流水（idempotency_key 上的唯一约束是"重试放大"的 DB 兜底）
    await _log_receive(session, tpl_id, user_id, code_row.id, idem_key, ip, channel)

    return code_row


def _build_code(tpl: CouponTemplate, user_id: int, *, source: int) -> CouponCode:
    """按模板生成券实例，并把有效期**固化到实例上**。

    「领取后 N 天」这种有效期必须在领取时算成绝对区间存下来 ——
    模板改了不影响已发出的券（docs/04 §11 的"规则不漂移"）。
    """
    now = datetime.now(UTC)
    if tpl.valid_type == VALID_DAYS_AFTER:
        days = tpl.valid_days or 7
        valid_start, valid_end = now, now + timedelta(days=days)
    else:
        valid_start = tpl.valid_start or now
        valid_end = tpl.valid_end or (now + timedelta(days=7))

    return CouponCode(
        id=next_id(),
        coupon_template_id=tpl.id,
        user_id=user_id,
        code=_gen_coupon_code(),
        status=CODE_UNUSED,
        valid_start=valid_start,
        valid_end=valid_end,
        source=source,
    )


def _gen_coupon_code() -> str:
    """可读券码。用雪花 ID 的十六进制，天然唯一且长度可控。"""
    return f"C{next_id():x}".upper()[:32]


async def _find_by_idem(
    session: AsyncSession, tpl_id: int, user_id: int, idem_key: str
) -> CouponCode | None:
    """幂等命中时把上次发的券找回来。"""
    code_id = await session.scalar(
        text(
            "SELECT coupon_code_id FROM promotion.coupon_receive_log "
            "WHERE idempotency_key = :k AND coupon_template_id = :tpl AND user_id = :uid"
        ),
        {"k": idem_key, "tpl": tpl_id, "uid": user_id},
    )
    if code_id is None:
        return None
    return await repo.get_code(session, int(code_id))


async def _log_receive(
    session: AsyncSession,
    tpl_id: int,
    user_id: int,
    code_id: int,
    idem_key: str,
    ip: str | None,
    channel: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO promotion.coupon_receive_log "
            "(coupon_template_id, user_id, coupon_code_id, channel, ip, idempotency_key) "
            "VALUES (:tpl, :uid, :cid, :channel, :ip, :idem) "
            "ON CONFLICT (idempotency_key) DO NOTHING"
        ),
        {
            "tpl": tpl_id,
            "uid": user_id,
            "cid": code_id,
            "channel": channel,
            "ip": ip,
            "idem": idem_key,
        },
    )


async def issue_by_admin(
    session: AsyncSession,
    *,
    tpl_id: int,
    user_id: int,
    operator: str,
    count: int = 1,
) -> list[CouponCode]:
    """客服手工补发（docs/04 §11）。

    **不走 Redis 计数**，也不占活动额度 —— ``issued_count`` 不增加。
    理由：补发是"平台欠用户的"，不该消耗活动库存（否则会挤占正常用户的名额）。

    ★ 但"不占活动额度"不等于"没有上限"——下面两道闸是补发自己该有的配额。
      它们是**事前拒**：事后再查，券已经发出去了。阈值见 ``models`` 里的常量。
    """
    tpl = await repo.get_template(session, tpl_id)
    if tpl is None:
        raise BizError(ErrorCode.COUPON_NOT_FOUND)

    already = await repo.count_issues_for_user_template(session, user_id=user_id, tpl_id=tpl_id)
    if already + count > ISSUE_MAX_PER_USER_TPL:
        raise BizError(
            ErrorCode.VALIDATION_ERROR,
            f"该用户在这个模板上已补发 {already} 张，单用户上限 {ISSUE_MAX_PER_USER_TPL} 张",
        )

    recent = await repo.count_issues_since(
        session, operator=operator, since=datetime.now(UTC) - timedelta(hours=24)
    )
    if recent + count > ISSUE_MAX_PER_OPERATOR_24H:
        raise BizError(
            ErrorCode.VALIDATION_ERROR,
            f"你 24 小时内已补发 {recent} 张，上限 {ISSUE_MAX_PER_OPERATOR_24H} 张",
        )

    codes = [
        await repo.insert_code(session, _build_code(tpl, user_id, source=CODE_SOURCE_SYSTEM))
        for _ in range(count)
    ]
    for c in codes:
        await repo.try_insert_flow(
            session,
            CouponFlow(
                coupon_code_id=c.id,
                from_status=0,  # 0 表示"从未存在到已发出"
                to_status=CODE_UNUSED,
                biz_key=f"{ISSUE_BIZ_KEY_PREFIX}{c.id}",
                operator=operator,
                remark="客服补发",
            ),
        )
    return codes


async def list_issue_records(
    session: AsyncSession, *, cursor: str | None, limit: int
) -> dict[str, Any]:
    """客服补发的历史记录（最新在前）。

    ``items`` 里是 ``(流水, 券码, 模板)`` 三元组 —— **原始行**，由路由转出参模型。
    之所以不在这里拼 DTO：列表要显示**操作人昵称**与**收件人昵称**，而两者都在
    account 域，promotion 不能 import account（docs/01 §2）。拼装留给同时看得见
    两个模块的那一层，也就是路由。
    """
    rows = await repo.list_issue_records(
        session, cursor=_decode_cursor(cursor) if cursor else None, limit=limit
    )
    # repository 多取了一条，据此判断还有没有下一页
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "items": page,
        "has_more": has_more,
        "next_cursor": _encode_cursor(page[-1][0].id) if has_more and page else None,
    }


async def issue_summary_24h(session: AsyncSession) -> tuple[int, list[tuple[str | None, int]]]:
    """最近 24 小时的补发汇总：``(总张数, [(操作人, 张数), ...])``。

    放进记录接口一起返回，省得列表页为了一个"今日发了多少"再跑一趟。
    """
    by_operator = await repo.issue_summary_since(
        session, since=datetime.now(UTC) - timedelta(hours=24)
    )
    return sum(n for _, n in by_operator), by_operator


# ============================================================
# 券的查询
# ============================================================
async def list_my_coupons(
    session: AsyncSession, user_id: int, *, status: int | None = None
) -> list[dict]:
    """我的券列表。返回带模板信息与状态文案的视图。"""
    codes = await repo.list_user_codes(session, user_id, status=status)
    if not codes:
        return []
    tpls = {t.id: t for t in await repo.list_templates_by_ids(session, [c.coupon_template_id for c in codes])}
    now = datetime.now(UTC)
    result = []
    for c in codes:
        tpl = tpls.get(c.coupon_template_id)
        result.append(
            {
                "code": c,
                # 惰性过期（docs/04 §9）：查询时顺手纠正，别等定时任务
                # 实际状态与库里不一致时以"已过期"为准展示
                "expired": c.valid_end < now,
                "status_text": CODE_STATUS_TEXT.get(c.status, str(c.status)),
                "template": tpl,
            }
        )
    return result


async def list_receivable(session: AsyncSession, user_id: int) -> list[dict]:
    """券中心：可领取的券 + 当前用户已领了几张（判断是否还能领）。"""
    now = datetime.now(UTC)
    tpls = await repo.list_receivable_templates(session, now=now)
    if not tpls:
        return []

    # 已领数：从计数表批量取，不逐个查
    rows = await session.execute(
        select(CouponUserQuota.coupon_template_id, CouponUserQuota.received).where(
            CouponUserQuota.user_id == user_id,
            CouponUserQuota.coupon_template_id.in_([t.id for t in tpls]),
        )
    )
    received = {int(tpl_id): int(n) for tpl_id, n in rows}

    return [
        {
            "template": t,
            "received": received.get(t.id, 0),
            "can_receive": received.get(t.id, 0) < t.per_user_limit
            and t.issued_count < t.total_count,
            "remain": max(0, t.total_count - t.issued_count),
        }
        for t in tpls
    ]


# ============================================================
# 运营查询（只读）
# ============================================================
def _encode_cursor(row_id: int) -> str:
    """游标就是行 id 的 base64。

    模板/活动都按 ``id desc`` 翻页，没有排序字段要一起带，所以比订单那种
    ``isoformat|id`` 的复合游标简单。base64 只是为了让它看起来不像"可以
    随便猜的连续编号"。写法对齐 aftersale / product 两个模块的同名私有函数
    （项目里没有公共游标工具，各模块自带一份）。
    """
    return base64.urlsafe_b64encode(str(row_id).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return int(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, TypeError) as exc:
        raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新加载") from exc


async def list_admin_templates(
    session: AsyncSession, *, status: int | None, cursor: str | None, limit: int
) -> dict[str, Any]:
    """运营的券模板列表。``items`` 里是 ORM 对象，由路由负责转成出参模型。"""
    rows = await repo.list_admin_templates(
        session,
        status=status,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=limit,
    )
    # repository 多取了一条，这里据此判断还有没有下一页
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "items": page,
        "has_more": has_more,
        "next_cursor": _encode_cursor(page[-1].id) if has_more and page else None,
    }


async def list_admin_activities(
    session: AsyncSession, *, status: int | None, cursor: str | None, limit: int
) -> dict[str, Any]:
    """运营的促销活动列表。"""
    rows = await repo.list_admin_activities(
        session,
        status=status,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=limit,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "items": page,
        "has_more": has_more,
        "next_cursor": _encode_cursor(page[-1].id) if has_more and page else None,
    }


# ============================================================
# 生命周期（锁 / 解锁 / 核销 / 退回）
# ============================================================
async def lock(
    session: AsyncSession, *, code_id: int, user_id: int, order_main_no: str
) -> None:
    """下单时锁券。

    DB 的条件更新是权威；Redis 侧同步标记让结算页能快速判断。
    Redis 失败不影响正确性（它只是缓存），所以这里不因为 Redis 出错而回滚。
    """
    flow = CouponFlow(
        coupon_code_id=code_id,
        from_status=CODE_UNUSED,
        to_status=CODE_LOCKED,
        order_no=order_main_no,
        biz_key=f"LOCK:{order_main_no}:{code_id}",
    )
    if not await repo.try_insert_flow(session, flow):
        return  # 幂等：这笔锁定已经处理过

    if not await repo.lock_code(session, code_id, user_id, order_main_no):
        raise BizError(ErrorCode.COUPON_LOCKED, "优惠券不可用（已被占用或已过期）")

    try:
        await get_lua().coupon_lock(
            keys=[rk.coupon_code(code_id)],
            args=[order_main_no, 1800, int(time.time())],
        )
    except Exception:
        # Redis 只是缓存，失败不该让下单失败
        logger.warning("锁券时 Redis 标记失败，DB 已锁定", extra={"code_id": code_id})


async def unlock(
    session: AsyncSession, *, code_id: int, user_id: int, order_main_no: str
) -> None:
    """解锁（订单取消 / 超时 / 支付失败）。"""
    flow = CouponFlow(
        coupon_code_id=code_id,
        from_status=CODE_LOCKED,
        to_status=CODE_UNUSED,
        order_no=order_main_no,
        biz_key=f"UNLOCK:{order_main_no}:{code_id}",
    )
    if not await repo.try_insert_flow(session, flow):
        return
    await repo.unlock_code(session, code_id, user_id)
    await _sync_code_cache(code_id, "UNUSED")


async def use(
    session: AsyncSession,
    *,
    code_id: int,
    user_id: int,
    order_main_no: str,
    use_amount: int,
) -> None:
    """核销（支付成功）。"""
    flow = CouponFlow(
        coupon_code_id=code_id,
        from_status=CODE_LOCKED,
        to_status=CODE_USED,
        order_no=order_main_no,
        use_amount=use_amount,
        biz_key=f"USE:{order_main_no}:{code_id}",
    )
    if not await repo.try_insert_flow(session, flow):
        return
    if not await repo.use_code(session, code_id, user_id, order_main_no, use_amount):
        raise BizError(ErrorCode.COUPON_LOCKED, "优惠券状态异常，无法核销")
    code = await repo.get_code(session, code_id)
    if code is not None:
        await repo.bump_template_used_count(session, code.coupon_template_id)
    await _sync_code_cache(code_id, "USED")


# ============================================================
# 订单级的券生命周期（由 trade 调用）
# ============================================================
async def settle_by_order(
    session: AsyncSession, order_main_no: str, *, amount_by_code: dict[int, int]
) -> None:
    """支付成功：把这张单锁住的券**核销**。

    ``amount_by_code`` 是每张券实际抵扣了多少钱 —— 由 trade 从订单优惠快照里读出来
    传进来。**不让 promotion 自己去查订单表**：跨模块读别人的 schema 会让
    模块边界失效（docs/01 §2）。
    """
    locked = await repo.list_locked_codes_of_order(session, order_main_no)
    for code in locked:
        await use(
            session,
            code_id=int(code.id),
            user_id=int(code.user_id),
            order_main_no=order_main_no,
            use_amount=amount_by_code.get(int(code.id), 0),
        )


async def release_by_order(session: AsyncSession, order_main_no: str) -> None:
    """关单（取消 / 超时）：把这张单锁住的券**解锁**，让用户能再用。

    与 ``lock`` 一样按 ``biz_key`` 幂等，重复关单不会重复解锁。
    """
    locked = await repo.list_locked_codes_of_order(session, order_main_no)
    for code in locked:
        await unlock(
            session,
            code_id=int(code.id),
            user_id=int(code.user_id),
            order_main_no=order_main_no,
        )


async def refund(
    session: AsyncSession, *, code_id: int, user_id: int, refund_no: str
) -> None:
    """整单退款成功后把券退回。"""
    flow = CouponFlow(
        coupon_code_id=code_id,
        from_status=CODE_USED,
        to_status=CODE_UNUSED,
        biz_key=f"REFUND:{refund_no}:{code_id}",
        remark="整单退款退回",
    )
    if not await repo.try_insert_flow(session, flow):
        return
    await repo.refund_code(session, code_id, user_id)
    await _sync_code_cache(code_id, "UNUSED")


async def _sync_code_cache(code_id: int, status: str) -> None:
    """同步券的 Redis 缓存状态。失败只记日志 —— 缓存不是权威。"""
    try:
        redis = get_redis()
        key = rk.coupon_code(code_id)
        if await redis.exists(key):
            await redis.hset(key, "status", status)
    except Exception:
        logger.warning("券缓存状态同步失败", extra={"code_id": code_id, "status": status})
