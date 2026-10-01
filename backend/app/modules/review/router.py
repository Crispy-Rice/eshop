"""review 模块的 HTTP 路由。

买家 ``/api/reviews`` 与商品详情的评价区（``/api/spus/{id}/reviews``）、
商家 ``/api/merchant/reviews``、运营 ``/api/admin/reviews``。

★ **评价列表与评分汇总是公开接口**（docs/15 §4 明列的例外）——
不挂登录态也能看，否则未登录用户浏览商品时看不到任何评价。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.context import CurrentUser
from app.core.deps import CurrentUserDep, DbSession, IdempotencyKeyDep, require_role
from app.core.errors import BizError, ErrorCode
from app.core.response import ApiResponse
from app.modules.account.deps import CurrentShopIdDep
from app.modules.review import repository as repo
from app.modules.review import service
from app.modules.review.schemas import (
    AuditQueueOut,
    MerchantReplyRequest,
    PendingReviewListOut,
    ReviewAuditRequest,
    ReviewEligibilityOut,
    ReviewEligibilityRequest,
    ReviewFollowUpRequest,
    ReviewListOut,
    ReviewOut,
    ReviewStatsOut,
    ReviewSubmitRequest,
)
from app.modules.review.state_machine import AuditAction

router = APIRouter()

AdminDep = Annotated[CurrentUser, Depends(require_role("admin"))]


# ============================================================
# 买家
# ============================================================
@router.get(
    "/api/reviews/pending",
    response_model=ApiResponse[PendingReviewListOut],
    summary="待评价的订单项",
)
async def list_pending(
    session: DbSession,
    user: CurrentUserDep,
    cursor: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=20, ge=1, le=50),
) -> ApiResponse[PendingReviewListOut]:
    """待评价列表。按**订单项**判定，不看 ``order_sub.is_reviewed`` ——
    那是子单级布尔，表达不了"3 件只评了 1 件"。"""
    return ApiResponse.ok(
        await service.list_pending(
            session,
            user.id,
            cursor=int(cursor) if cursor else None,
            limit=limit,
        )
    )


@router.post(
    "/api/reviews/eligibility",
    response_model=ApiResponse[ReviewEligibilityOut],
    summary="评价资格预检",
)
async def check_eligibility(
    session: DbSession, body: ReviewEligibilityRequest, user: CurrentUserDep
) -> ApiResponse[ReviewEligibilityOut]:
    """进发评价页前先问一次，让用户**填表之前**就知道能不能评、为什么不能。

    已评价过的订单项会带回首评 id 与 ``canFollowUp``，前端可以直接引导去追评。
    """
    return ApiResponse.ok(
        await service.check_eligibility(
            session, user_id=user.id, order_item_id=int(body.order_item_id)
        )
    )


@router.post(
    "/api/reviews",
    response_model=ApiResponse[ReviewOut],
    summary="提交评价（需 Idempotency-Key）",
)
async def submit_review(
    session: DbSession,
    body: ReviewSubmitRequest,
    user: CurrentUserDep,
    _idem: IdempotencyKeyDep,
) -> ApiResponse[ReviewOut]:
    """提交评价。

    幂等由 ``uk_review_order_item``（每个订单项一条首评）保证；
    ``Idempotency-Key`` 让"同一次提交的重试"能被识别出来。

    机审通过的直接发布，命中敏感词的转待审核。
    """
    return ApiResponse.ok(await service.submit(session, user_id=user.id, req=body))


@router.post(
    "/api/reviews/{review_id}/follow-up",
    response_model=ApiResponse[ReviewOut],
    summary="追评",
)
async def submit_follow_up(
    session: DbSession, review_id: int, body: ReviewFollowUpRequest, user: CurrentUserDep
) -> ApiResponse[ReviewOut]:
    """追评。首评后 30 天内一次，由 ``uk_review_follow_up`` 兜底。"""
    return ApiResponse.ok(
        await service.submit_follow_up(
            session, user_id=user.id, review_id=review_id, req=body
        )
    )


@router.get("/api/reviews/mine", response_model=ApiResponse[ReviewListOut], summary="我的评价")
async def list_my_reviews(
    session: DbSession,
    user: CurrentUserDep,
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=10, ge=1, le=50),
) -> ApiResponse[ReviewListOut]:
    return ApiResponse.ok(
        await service.list_my_reviews(session, user.id, cursor=cursor, limit=limit)
    )


# ============================================================
# 商品详情的评价区（**公开接口，不需要登录**）
# ============================================================
@router.get(
    "/api/spus/{spu_id}/reviews",
    response_model=ApiResponse[ReviewListOut],
    summary="商品评价列表（公开）",
)
async def list_spu_reviews(
    session: DbSession,
    spu_id: int,
    sort: str = Query(default=repo.SORT_LATEST, pattern="^(latest|recommend)$"),
    # 对外就叫 filter（docs/15 §2.7），内部换个名字避开内建函数
    filter_: str = Query(default=repo.FILTER_ALL, alias="filter", pattern="^(all|good|with_image)$"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=10, ge=1, le=50),
) -> ApiResponse[ReviewListOut]:
    """商品评价列表。**公开接口** —— 未登录用户浏览商品时也要能看到评价。

    ``sort=latest|recommend``、``filter=all|good|with_image``，三种组合各有
    对应的部分索引，都是游标分页。
    """
    return ApiResponse.ok(
        await service.list_spu_reviews(
            session, spu_id, sort=sort, filter_=filter_, cursor=cursor, limit=limit
        )
    )


@router.get(
    "/api/spus/{spu_id}/review-stats",
    response_model=ApiResponse[ReviewStatsOut],
    summary="商品评分汇总（公开）",
)
async def get_spu_review_stats(
    session: DbSession, spu_id: int
) -> ApiResponse[ReviewStatsOut]:
    """评分汇总。

    **无评价时 ``avgScore`` 与 ``goodRate`` 都是 null** —— 前端据此显示"暂无评价"，
    不要显示成 5.0 分或 0 分。
    """
    return ApiResponse.ok(await service.get_spu_stats(session, spu_id))


# ============================================================
# 商家
# ============================================================
@router.get(
    "/api/merchant/reviews",
    response_model=ApiResponse[ReviewListOut],
    summary="商家评价列表",
)
async def list_shop_reviews(
    session: DbSession,
    shop_id: CurrentShopIdDep,
    status: int | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> ApiResponse[ReviewListOut]:
    return ApiResponse.ok(
        await service.list_shop_reviews(
            session, shop_id, status=status, cursor=cursor, limit=limit
        )
    )


@router.post(
    "/api/merchant/reviews/{review_id}/reply",
    response_model=ApiResponse[None],
    summary="回复评价",
)
async def reply_review(
    session: DbSession,
    review_id: int,
    body: MerchantReplyRequest,
    shop_id: CurrentShopIdDep,
) -> ApiResponse[None]:
    """回复自己店铺的评价。一条评价最多 3 条商家回复（防刷屏）。

    只能回复本店铺商品的评价 —— 锁评价行后校验 ``shop_id``，不符返回"不存在"。
    """
    await service.reply(session, shop_id=shop_id, review_id=review_id, content=body.content)
    return ApiResponse.ok(None)


# ============================================================
# 运营
# ============================================================
@router.get(
    "/api/admin/reviews/audit-queue",
    response_model=ApiResponse[AuditQueueOut],
    summary="评价审核队列",
)
async def list_audit_queue(
    session: DbSession,
    _admin: AdminDep,
    status: int = Query(default=0, description="0待审核 1已发布 2已屏蔽 3审核不通过"),
    second_audit_only: bool = Query(default=False, description="只看待抽检的"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> ApiResponse[AuditQueueOut]:
    """审核队列。``secondAuditOnly=true`` 看"机审放行但标记待抽检"的那批。"""
    return ApiResponse.ok(
        await service.list_audit_queue(
            session,
            status=status,
            second_audit_only=second_audit_only,
            cursor=cursor,
            limit=limit,
        )
    )


@router.post(
    "/api/admin/reviews/{review_id}/audit",
    response_model=ApiResponse[None],
    summary="处置评价（通过 / 驳回 / 屏蔽 / 解除屏蔽）",
)
async def audit_review(
    session: DbSession, review_id: int, body: ReviewAuditRequest, _admin: AdminDep
) -> ApiResponse[None]:
    """处置一条评价。

    ★ 这一步会**同步更新商品的评价统计**（发布 +1、屏蔽 −1、解除 +1、驳回不动），
    所以状态与计数不会漂。
    """
    try:
        action = AuditAction(body.action)
    except ValueError as exc:
        raise BizError(ErrorCode.VALIDATION_ERROR, "不支持的操作") from exc
    await service.transit(session, review_id, action, remark=body.remark)
    return ApiResponse.ok(None)
