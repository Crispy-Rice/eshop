"""review 模块的领域逻辑。

四件事：

1. **资格判定**（``check_eligibility``）—— "购后限制"的全部规则
2. **提交与追评** —— 唯一索引保证"每个订单项只能评一次"
3. **审核**（``transit``）—— 状态迁移与统计 delta **成对**发生
4. **商家回复** —— 行锁 + 计数，挡住并发的第 4 条回复

三条容易做错、这里刻意钉住的规则：

- **★ 不看子单状态是否 FINISHED**：部分退款会把子单推到 70「已退款」，
  用 FINISHED 兜会把剩余商品漏掉（docs/12 §9 明确要求"部分退款后剩余商品可评价"）。
  判定用 ``receive_time`` + **订单项级**退款数。
- **★ 插入用 ``ON CONFLICT DO NOTHING``**：唯一冲突不该让事务进入 aborted 状态，
  也不该让用户看到 500（docs/12 §2.2）。
- **★ 统计与状态成对**：所有状态变更都走 ``transit``，它在改状态的同时调
  ``product.apply_review_stat_delta``。绕过它就等于让计数漂。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import after_commit
from app.core.config import get_settings
from app.core.enums import ReviewStatus
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.core.redis_keys import review_daily
from app.core.snowflake import next_id
from app.modules.account import service as account_service
from app.modules.files import storage
from app.modules.product import service as product_service
from app.modules.review import calc
from app.modules.review import repository as repo
from app.modules.review.models import (
    DAILY_REVIEW_LIMIT,
    FOLLOW_UP_WINDOW_DAYS,
    MAX_MERCHANT_REPLIES,
    REPLY_MERCHANT,
    REPLY_PUBLISHED,
    REVIEW_WINDOW_DAYS,
    Review,
    ReviewReply,
)
from app.modules.review.schemas import (
    ANONYMOUS_NAME,
    AuditQueueItemOut,
    AuditQueueOut,
    PendingReviewItemOut,
    PendingReviewListOut,
    ReviewEligibilityOut,
    ReviewFollowUpRequest,
    ReviewListOut,
    ReviewOut,
    ReviewStatsOut,
    ReviewSubmitRequest,
    to_review_out,
)
from app.modules.review.sensitive import machine_audit
from app.modules.review.state_machine import AuditAction, next_status, stat_delta
from app.modules.trade import service as trade_service

logger = get_logger(__name__)

# 订单状态码。**不从 trade 模块 import 常量是为了避免一个循环**：
# trade.service 不需要认识 review，但 review 读 trade 的状态码是合理的。
_SUB_WAIT_RECEIVE = 30
_SUB_CLOSED = 50
_SUB_REFUNDING = 60


# ============================================================
# 资格判定
# ============================================================
async def check_eligibility(
    session: AsyncSession, *, user_id: int, order_item_id: int
) -> ReviewEligibilityOut:
    """能否评价这个订单项。**只读**。

    判定顺序照着"用户最可能踩到的那条"排：先看是不是自己的单，再看货到没到，
    最后才看窗口与是否评过。
    """

    def no(code: ErrorCode, message: str | None = None) -> ReviewEligibilityOut:
        return ReviewEligibilityOut(eligible=False, reason=str(code), message=message or code.default_message)

    found = await trade_service.get_item_with_sub(session, order_item_id)
    if found is None:
        return no(ErrorCode.ORDER_ITEM_NOT_FOUND)
    item, sub = found

    # ① 归属：不是自己的订单返回"不存在"，不区分"不存在"与"不是你的"（防探测）
    if int(sub.user_id) != user_id:
        return no(ErrorCode.ORDER_ITEM_NOT_FOUND)

    # ② 待收货：货还没到手，谈不上体验
    if int(sub.status) == _SUB_WAIT_RECEIVE:
        return no(ErrorCode.NOT_RECEIVED)

    # ③ 已关闭：订单没成交
    if int(sub.status) == _SUB_CLOSED:
        return no(ErrorCode.ORDER_CLOSED)

    # ④ 整单售后中：等售后有结论再评，否则评完又退款，内容就失真了
    if int(sub.status) == _SUB_REFUNDING:
        return no(ErrorCode.IN_AFTERSALE)

    # ⑤ 该订单项全部退掉了 —— **部分退款仍可评价**（docs/12 §9）
    if int(item.refunded_num) >= int(item.num):
        return no(ErrorCode.ITEM_REFUNDED)

    # ⑥ 该订单项正在售后中
    if int(item.refunding_num) > 0:
        return no(ErrorCode.IN_AFTERSALE)

    # ⑦ 从未签收（走到这里说明 2/3/4 都没拦，就是没发货的已付款单）
    if sub.receive_time is None:
        return no(ErrorCode.ORDER_NOT_FINISHED)

    # ⑧ 窗口：签收后 30 天内
    now = datetime.now(UTC)
    received = sub.receive_time if sub.receive_time.tzinfo else sub.receive_time.replace(tzinfo=UTC)
    if now > received + timedelta(days=REVIEW_WINDOW_DAYS):
        return no(
            ErrorCode.REVIEW_EXPIRED,
            f"评价期限已过（签收后 {REVIEW_WINDOW_DAYS} 天内可评价）",
        )

    # ⑨ 已评价？—— 这是**预检查**，真正保证是唯一索引。
    #    已评过时把首评 id 带回去，前端可以直接引导去追评
    existing = await repo.get_first_review_of_item(session, order_item_id)
    if existing is not None:
        can_follow = await _can_follow_up(session, existing, user_id)
        return ReviewEligibilityOut(
            eligible=False,
            reason=str(ErrorCode.ALREADY_REVIEWED),
            message="该商品已评价",
            order_item_id=item.id,
            existing_review_id=existing.id,
            can_follow_up=can_follow,
        )

    return ReviewEligibilityOut(
        eligible=True,
        order_item_id=item.id,
        spu_id=item.spu_id,
        sku_id=item.sku_id,
        title=item.spu_title_snap,
        spec_text=item.sku_spec_snap,
        cover_image=item.cover_image_snap,
        num=item.num,
    )


async def _can_follow_up(session: AsyncSession, parent: Review, user_id: int) -> bool:
    """首评能否追评：属于自己、已发布、在追评窗口内、还没追过。"""
    if int(parent.user_id) != user_id:
        return False
    if int(parent.status) != int(ReviewStatus.PUBLISHED):
        return False
    created = parent.created_at if parent.created_at.tzinfo else parent.created_at.replace(tzinfo=UTC)
    if datetime.now(UTC) > created + timedelta(days=FOLLOW_UP_WINDOW_DAYS):
        return False
    return await repo.get_follow_up_of(session, parent.id) is None


# ============================================================
# 提交首评
# ============================================================
async def submit(
    session: AsyncSession, *, user_id: int, req: ReviewSubmitRequest
) -> ReviewOut:
    """提交评价。调用方的事务内执行（``Idempotency-Key`` 由路由层校验）。"""
    await _assert_daily_limit(user_id)

    elig = await check_eligibility(session, user_id=user_id, order_item_id=int(req.order_item_id))
    if not elig.eligible:
        raise BizError(ErrorCode(elig.reason or str(ErrorCode.ORDER_ITEM_NOT_FOUND)), elig.message)

    calc.check_content(content=req.content, images=req.images, user_id=user_id)
    status, need_second_audit = machine_audit(req.content)

    found = await trade_service.get_item_with_sub(session, int(req.order_item_id))
    if found is None:  # pragma: no cover - 上面刚查过
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND)
    item, sub = found
    now = datetime.now(UTC)
    suspect = calc.is_suspect(sub.receive_time, now)

    review_id = await repo.insert_review(
        session,
        {
            "id": next_id(),
            "user_id": user_id,
            "spu_id": int(item.spu_id),
            "sku_id": int(item.sku_id),
            # 从订单项复制店铺：商家后台按店铺筛评价时就不必跨模块查商品
            "shop_id": int(item.shop_id),
            "order_item_id": int(item.id),
            "order_sub_no": item.order_sub_no,
            "order_main_no": item.order_main_no,
            "sku_spec_snap": item.sku_spec_snap,
            "buy_count": int(item.num),
            "score": req.score,
            "content": req.content,
            "images": list(req.images),
            "anonymous": req.anonymous,
            "status": status,
            "need_second_audit": need_second_audit,
            "suspect": suspect,
            "rank_score": calc.calc_rank_score(
                content=req.content, images=req.images, suspect=suspect
            ),
            "is_follow_up": False,
            "parent_id": None,
        },
    )
    if review_id is None:
        # ★ 唯一索引拦下了：并发提交或重复提交。**这不是系统错误**，是"已经评过了"
        raise BizError(ErrorCode.ALREADY_REVIEWED, "该商品已评价，不能重复评价")

    # 统计只在**已发布**时才动（待审核的先不计，审核通过时再计）
    await _apply_stat_if_published(
        session, review_id, status=status, score=req.score, is_follow_up=False
    )
    await _mark_sub_reviewed_if_done(session, item.order_sub_no)

    # 提交成功后累加当日计数。放提交后是因为 Redis 副作用无法随 PG 回滚，
    # 且"丢一次回调"只会少计，对限流而言是安全方向
    after_commit.defer(session, lambda: _bump_daily_count(user_id))

    logger.info(
        "评价已提交",
        extra={"reviewId": review_id, "orderItemId": int(item.id), "status": status},
    )
    return await _single_out(session, review_id)


# ============================================================
# 追评
# ============================================================
async def submit_follow_up(
    session: AsyncSession, *, user_id: int, review_id: int, req: ReviewFollowUpRequest
) -> ReviewOut:
    """追评。首评后 30 天内一次，由 ``uk_review_follow_up`` 兜底。"""
    parent = await repo.get_by_id(session, review_id)
    if parent is None or int(parent.user_id) != user_id or parent.is_follow_up:
        raise BizError(ErrorCode.NOT_FOUND, "评价不存在")
    if int(parent.status) != int(ReviewStatus.PUBLISHED):
        raise BizError(ErrorCode.REVIEW_STATUS_INVALID, "该评价当前状态不能追评")
    if not await _can_follow_up(session, parent, user_id):
        raise BizError(
            ErrorCode.ALREADY_FOLLOWED_UP,
            f"不能追评：可能已追评过，或已超过首评后 {FOLLOW_UP_WINDOW_DAYS} 天",
        )

    calc.check_content(content=req.content, images=req.images, user_id=user_id)
    status, need_second_audit = machine_audit(req.content)
    # 追评不重新打分，沿用首评的星级
    anonymous = parent.anonymous if req.anonymous is None else req.anonymous

    new_id = await repo.insert_follow_up(
        session,
        {
            "id": next_id(),
            "user_id": user_id,
            "spu_id": int(parent.spu_id),
            "sku_id": int(parent.sku_id),
            "shop_id": int(parent.shop_id),
            "order_item_id": int(parent.order_item_id),
            "order_sub_no": parent.order_sub_no,
            "order_main_no": parent.order_main_no,
            "sku_spec_snap": parent.sku_spec_snap,
            "buy_count": parent.buy_count,
            "score": parent.score,
            "content": req.content,
            "images": list(req.images),
            "anonymous": anonymous,
            "status": status,
            "need_second_audit": need_second_audit,
            "suspect": False,
            # 追评不计入统计，也就不需要排序分（跟随首评展示）
            "rank_score": 0,
            "is_follow_up": True,
            "parent_id": int(parent.id),
        },
    )
    if new_id is None:
        raise BizError(ErrorCode.ALREADY_FOLLOWED_UP, "该评价已追评过")

    logger.info("追评已提交", extra={"reviewId": new_id, "parentId": int(parent.id)})
    return await _single_out(session, new_id)


# ============================================================
# 审核（运营）—— 状态迁移与统计成对
# ============================================================
async def transit(
    session: AsyncSession,
    review_id: int,
    action: AuditAction,
    *,
    remark: str | None = None,
) -> Review:
    """处置一条评价。**所有状态变更都走这里**，因为要在同一个地方改统计。

    ★ 绕过这个函数直接 UPDATE ``status``，统计就会漂 —— 这正是把两者封在一起的
    原因。也正因如此，``repository.cas_status`` 不该在别处被调用。
    """
    review = await repo.get_for_update(session, review_id)
    if review is None:
        raise BizError(ErrorCode.NOT_FOUND, "评价不存在")

    from_status = ReviewStatus(int(review.status))
    to_status = next_status(from_status, action)

    if not await repo.cas_status(
        session,
        review_id,
        from_status=int(from_status),
        to_status=int(to_status),
        audit_remark=remark,
    ):
        raise BizError(ErrorCode.REVIEW_STATUS_INVALID, "评价状态已变化，请刷新后重试")

    count_delta, score_delta, good_delta = stat_delta(
        from_status, to_status, score=int(review.score), is_follow_up=bool(review.is_follow_up)
    )
    if count_delta or score_delta or good_delta:
        await product_service.apply_review_stat_delta(
            session,
            int(review.spu_id),
            count_delta=count_delta,
            score_delta=score_delta,
            good_delta=good_delta,
        )

    logger.info(
        "评价已处置",
        extra={
            "reviewId": review_id,
            "action": str(action),
            "from": int(from_status),
            "to": int(to_status),
        },
    )
    refreshed = await repo.get_by_id(session, review_id)
    if refreshed is None:  # pragma: no cover - 刚写过
        raise BizError(ErrorCode.NOT_FOUND, "评价不存在")
    return refreshed


# ============================================================
# 商家回复
# ============================================================
async def reply(
    session: AsyncSession, *, shop_id: int, review_id: int, content: str
) -> None:
    """商家回复自己店铺的评价。一条评价最多 3 条商家回复（防刷屏）。

    锁**评价行**而不是回复行：并发回复时第二个请求会等锁，拿到锁后重新计数，
    所以突破不了上限（docs/12 §6）。
    """
    review = await repo.get_for_update(session, review_id)
    if review is None or int(review.shop_id) != shop_id:
        # 不区分"不存在"与"不是你的店铺的评价" —— 别让人遍历探测
        raise BizError(ErrorCode.NOT_FOUND, "评价不存在")

    if await repo.count_merchant_replies(session, review_id) >= MAX_MERCHANT_REPLIES:
        raise BizError(
            ErrorCode.REPLY_LIMIT_EXCEEDED, f"每条评价最多回复 {MAX_MERCHANT_REPLIES} 次"
        )

    await repo.insert_reply(
        session,
        ReviewReply(
            review_id=review_id,
            reply_type=REPLY_MERCHANT,
            replier_id=shop_id,
            content=content,
            status=REPLY_PUBLISHED,
        ),
    )
    await repo.incr_reply_count(session, review_id)
    logger.info("商家已回复评价", extra={"reviewId": review_id, "shopId": shop_id})


# ============================================================
# 查询
# ============================================================
def _media_prefix() -> str:
    return get_settings().media_url_prefix


async def _single_out(session: AsyncSession, review_id: int) -> ReviewOut:
    review = await repo.get_by_id(session, review_id)
    if review is None:  # pragma: no cover - 刚写过
        raise BizError(ErrorCode.NOT_FOUND, "评价不存在")
    profiles = await account_service.list_user_profiles(session, [int(review.user_id)])
    nickname, avatar = profiles.get(int(review.user_id), (None, None))
    return to_review_out(
        review,
        media_prefix=_media_prefix(),
        thumb_of=storage.thumb_path_of,
        nickname=nickname,
        avatar=avatar,
        replies=(
            (await repo.list_replies_of(session, [review_id])).get(review_id, [])
            if not review.is_follow_up
            else []
        ),
        follow_up=(
            await repo.get_follow_up_of(session, review_id) if not review.is_follow_up else None
        ),
        include_audit=True,
    )


async def _decorate(
    session: AsyncSession, rows: list[Review], *, include_audit: bool = False
) -> list[ReviewOut]:
    """批量补齐昵称、追评、回复 —— **列表页绝不允许 N+1**。"""
    if not rows:
        return []
    profile = await account_service.list_user_profiles(
        session, list({int(r.user_id) for r in rows})
    )
    ids = [int(r.id) for r in rows]
    replies = await repo.list_replies_of(session, ids)
    follows = await repo.list_follow_ups(session, ids)
    return [
        to_review_out(
            row,
            media_prefix=_media_prefix(),
            thumb_of=storage.thumb_path_of,
            nickname=profile.get(int(row.user_id), (None, None))[0],
            avatar=profile.get(int(row.user_id), (None, None))[1],
            replies=replies.get(int(row.id), []),
            follow_up=follows.get(int(row.id)),
            include_audit=include_audit,
        )
        for row in rows
    ]


async def list_pending(
    session: AsyncSession, user_id: int, *, cursor: int | None = None, limit: int = 20
) -> PendingReviewListOut:
    """待评价列表。

    ★ **不能只看 ``order_sub.is_reviewed``**：它是子单级布尔，表达不了"一个子单里
    3 件只评了 1 件"。所以这里按**订单项**查候选，再减掉已评过的。
    """
    # 多取一点，因为减掉已评的之后可能不够一页；
    # 页短了也没关系 —— 游标照常前进，前端"加载更多"会继续取，不会漏项
    candidates = await trade_service.list_reviewable_items(
        session, user_id, within_days=REVIEW_WINDOW_DAYS, cursor=cursor, limit=limit * 2
    )
    reviewed = await repo.reviewed_item_ids(session, [int(i.id) for i in candidates])
    pending = [i for i in candidates if int(i.id) not in reviewed]
    page = pending[:limit]
    has_more = len(candidates) > len(page)

    return PendingReviewListOut(
        items=[
            PendingReviewItemOut(
                order_item_id=i.id,
                spu_id=i.spu_id,
                sku_id=i.sku_id,
                title=i.spu_title_snap,
                spec_text=i.sku_spec_snap,
                cover_image=i.cover_image_snap,
                num=i.num,
                # 签收时间在子单上，这里不额外查 —— 前端展示"什么时候签收的"用不上
                receive_time=None,
            )
            for i in page
        ],
        next_cursor=str(candidates[-1].id) if has_more and candidates else None,
        has_more=has_more,
    )


async def list_spu_reviews(
    session: AsyncSession,
    spu_id: int,
    *,
    sort: str = repo.SORT_LATEST,
    filter_: str = repo.FILTER_ALL,
    cursor: str | None = None,
    limit: int = 10,
) -> ReviewListOut:
    """商品详情页的评价列表。**公开接口，不需要登录**。"""
    import base64

    def decode(cursor_str: str) -> tuple:
        try:
            padded = cursor_str + "=" * (-len(cursor_str) % 4)
            raw = base64.urlsafe_b64decode(padded).decode()
            if sort == repo.SORT_RECOMMEND:
                rank, row_id = raw.split("|")
                return int(rank), int(row_id)
            stamp, row_id = raw.split("|")
            return datetime.fromisoformat(stamp), int(row_id)
        except (ValueError, TypeError) as exc:
            raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新加载") from exc

    parsed = decode(cursor) if cursor else None
    rows = await repo.list_published_for_spu(
        session,
        spu_id,
        sort=sort,
        filter_=filter_,
        cursor=parsed if sort != repo.SORT_RECOMMEND else None,
        recommend_cursor=parsed if sort == repo.SORT_RECOMMEND else None,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor = None
    if has_more and page:
        last = page[-1]
        raw = (
            f"{last.rank_score}|{last.id}"
            if sort == repo.SORT_RECOMMEND
            else f"{last.created_at.isoformat()}|{last.id}"
        )
        next_cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    return ReviewListOut(
        items=await _decorate(session, page),
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def get_spu_stats(session: AsyncSession, spu_id: int) -> ReviewStatsOut:
    """商品详情页的评分汇总。

    ★ **直接读 ``product.spu`` 上的冗余计数**，不在这里另做 COUNT ——
    两处各自聚合口径一定会漂（列表页读冗余、这里读实时，数字就对不上）。
    另外这里只读 spu 已有的三个计数，`score_distribution` 才需要查 review 表。
    """
    from app.modules.product import repository as product_repo

    spu = await product_repo.get_spu(session, spu_id)
    if spu is None:
        raise BizError(ErrorCode.NOT_FOUND, "商品不存在")

    count = int(spu.review_count)
    if count == 0:
        # 无评价：不给平均分也不给好评率（docs/12 §9）
        return ReviewStatsOut(review_count=0, score_distribution={})

    distribution = {
        int(score): int(n)
        for score, n in (
            await session.execute(
                select(Review.score, func.count())
                .where(
                    Review.spu_id == spu_id,
                    Review.status == int(ReviewStatus.PUBLISHED),
                    Review.is_follow_up.is_(False),
                )
                .group_by(Review.score)
            )
        ).all()
    }
    good = int(spu.good_review_count)
    return ReviewStatsOut(
        review_count=count,
        avg_score=float(spu.avg_score) if spu.avg_score is not None else None,
        good_rate=round(good / count, 4),
        good_count=good,
        score_distribution=distribution,
    )


async def list_my_reviews(
    session: AsyncSession, user_id: int, *, cursor: str | None = None, limit: int = 10
) -> ReviewListOut:
    """我的评价。**追评也列出来** —— 用户要能看到自己发过的全部内容。"""
    parsed = _decode_time_cursor(cursor)
    rows = await repo.list_for_user(session, user_id, cursor=parsed, limit=limit + 1)
    return await _paged(session, rows, limit=limit, cursor_of=_time_cursor)


async def list_shop_reviews(
    session: AsyncSession,
    shop_id: int,
    *,
    status: int | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> ReviewListOut:
    parsed = _decode_time_cursor(cursor)
    rows = await repo.list_for_shop(session, shop_id, status=status, cursor=parsed, limit=limit + 1)
    return await _paged(session, rows, limit=limit, cursor_of=_time_cursor, include_audit=True)


async def list_audit_queue(
    session: AsyncSession,
    *,
    status: int = int(ReviewStatus.PENDING_AUDIT),
    second_audit_only: bool = False,
    cursor: str | None = None,
    limit: int = 20,
) -> AuditQueueOut:
    """运营审核队列。``second_audit_only=True`` 时看"机审放行但待抽检"的那批。"""
    parsed = _decode_time_cursor(cursor)
    rows = await repo.list_audit_queue(
        session,
        status=status,
        second_audit_only=second_audit_only,
        cursor=parsed,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    decorated = await _decorate(session, page, include_audit=True)
    counts = await repo.count_by_status(session)

    return AuditQueueOut(
        items=[
            AuditQueueItemOut(
                review=out,
                shop_id=row.shop_id,
                order_main_no=row.order_main_no,
                reason_text=(
                    "机审命中高风险词"
                    if int(row.status) == int(ReviewStatus.PENDING_AUDIT)
                    else "机审放行，待抽检"
                ),
            )
            for row, out in zip(page, decorated, strict=True)
        ],
        next_cursor=_time_cursor(page[-1]) if has_more and page else None,
        has_more=has_more,
        pending_count=counts.get(int(ReviewStatus.PENDING_AUDIT), 0),
        second_audit_count=sum(
            1 for r in page if r.need_second_audit and r.status == int(ReviewStatus.PUBLISHED)
        ),
    )


# ============================================================
# 内部工具
# ============================================================
def _time_cursor(review: Review) -> str:
    import base64

    raw = f"{review.created_at.isoformat()}|{review.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_time_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    import base64

    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, row_id = base64.urlsafe_b64decode(padded).decode().split("|")
        return datetime.fromisoformat(stamp), int(row_id)
    except (ValueError, TypeError) as exc:
        raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新加载") from exc


async def _paged(
    session: AsyncSession, rows: list[Review], *, limit: int, cursor_of, include_audit: bool = False
) -> ReviewListOut:
    has_more = len(rows) > limit
    page = rows[:limit]
    return ReviewListOut(
        items=await _decorate(session, page, include_audit=include_audit),
        next_cursor=cursor_of(page[-1]) if has_more and page else None,
        has_more=has_more,
    )


async def _apply_stat_if_published(
    session: AsyncSession, review_id: int, *, status: int, score: int, is_follow_up: bool
) -> None:
    """新建的评价如果直接就是"已发布"，立刻计入统计。

    待审核的不计 —— 审核通过时 ``transit`` 会再调一次 delta。
    """
    if status != int(ReviewStatus.PUBLISHED):
        return
    review = await repo.get_by_id(session, review_id)
    if review is None:  # pragma: no cover
        return
    count_delta, score_delta, good_delta = stat_delta(
        ReviewStatus.PENDING_AUDIT,
        ReviewStatus.PUBLISHED,
        score=score,
        is_follow_up=is_follow_up,
    )
    if count_delta or score_delta or good_delta:
        await product_service.apply_review_stat_delta(
            session,
            int(review.spu_id),
            count_delta=count_delta,
            score_delta=score_delta,
            good_delta=good_delta,
        )


async def _mark_sub_reviewed_if_done(session: AsyncSession, order_sub_no: str) -> None:
    """该子单内**每个未全退的订单项**都有首评时，把 ``is_reviewed`` 置 true。

    只是展示用缓存（待评价角标）。若"评了第一件就置 true"，一个 3 件的订单
    角标立刻消失，剩两件用户再也找不到入口。
    """
    items = await trade_service.list_items_of_sub(session, order_sub_no)
    pending = [i for i in items if int(i.refunded_num) < int(i.num)]
    if not pending:
        return
    reviewed = await repo.reviewed_item_ids(session, [int(i.id) for i in pending])
    if len(reviewed) == len(pending):
        await trade_service.mark_sub_reviewed(session, order_sub_no, reviewed=True)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


async def _assert_daily_limit(user_id: int) -> None:
    """当日评价数超限就拒绝。

    **Redis 出问题必须放行** —— 评价是业务数据，Redis 只是限流器。
    这与库存/券不同（那些 Redis 是正确性闸门），不能让缓存故障阻断评价。
    """
    try:
        current = await get_redis().get(review_daily(user_id, _today()))
    except Exception:
        logger.warning("评价限流计数读取失败，本次放行", exc_info=True)
        return
    if current is not None and int(current) >= DAILY_REVIEW_LIMIT:
        raise BizError(
            ErrorCode.RATE_LIMITED, f"今日评价次数已达上限（{DAILY_REVIEW_LIMIT} 条）"
        )


async def _bump_daily_count(user_id: int) -> None:
    """提交成功后累加当日计数（跑在 after_commit 里，失败只记日志）。"""
    try:
        redis = get_redis()
        key = review_daily(user_id, _today())
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            # TTL 给 2 天：跨日与时钟漂移都留余量
            pipe.expire(key, 2 * 24 * 3600)
            await pipe.execute()
    except Exception:
        logger.warning("评价限流计数累加失败", exc_info=True)


__all__ = [
    "ANONYMOUS_NAME",
    "AuditAction",
    "check_eligibility",
    "get_spu_stats",
    "list_audit_queue",
    "list_my_reviews",
    "list_pending",
    "list_shop_reviews",
    "list_spu_reviews",
    "reply",
    "submit",
    "submit_follow_up",
    "transit",
]
