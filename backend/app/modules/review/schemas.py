"""review 模块的请求/响应模型。

金额这里没有；评分是 ``1..5`` 的整数，数量与计数是整数。
图片在**入库时**是相对路径数组（``review.review.images``），
对外则展开成 ``{path, url, thumbUrl}`` 对象 —— 库里保持文档规定的字符串数组形态，
展示需要的信息由 DTO 补齐。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.core.schemas import CamelModel, SnowflakeId
from app.modules.review.models import (
    MAX_CONTENT_LEN,
    MAX_IMAGES,
    MAX_SCORE,
    MIN_SCORE,
    REPLY_TYPE_TEXT,
    REVIEW_STATUS_TEXT,
    Review,
    ReviewReply,
)
from app.modules.review.state_machine import AUDIT_ACTION_TEXT


# ============================================================
# 请求
# ============================================================
class ReviewSubmitRequest(CamelModel):
    """提交首评。幂等键走 ``Idempotency-Key``（唯一索引是最终保证）。"""

    order_item_id: SnowflakeId
    score: int = Field(ge=MIN_SCORE, le=MAX_SCORE, description="1-5 星")
    content: str | None = Field(default=None, max_length=MAX_CONTENT_LEN)
    images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)
    anonymous: bool = False

    @field_validator("content")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = v.strip()
        return cleaned or None


class ReviewFollowUpRequest(CamelModel):
    """追评。不重新打分（沿用首评的星级）—— 追评讲的是"用了一段时间之后"。"""

    content: str = Field(min_length=1, max_length=MAX_CONTENT_LEN)
    images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)
    anonymous: bool | None = Field(default=None, description="不传则沿用首评")


class ReviewEligibilityRequest(CamelModel):
    order_item_id: SnowflakeId


class MerchantReplyRequest(CamelModel):
    content: str = Field(min_length=1, max_length=500)


class ReviewAuditRequest(CamelModel):
    action: str = Field(description="APPROVE / REJECT / BLOCK / UNBLOCK")
    remark: str | None = Field(default=None, max_length=255)


# ============================================================
# 响应
# ============================================================
class ReviewImageOut(CamelModel):
    path: str
    url: str = Field(description="原图 URL")
    thumb_url: str = Field(description="缩略图 URL，列表页用")


class ReviewReplyOut(CamelModel):
    reply_type: int
    reply_type_text: str
    content: str
    created_at: datetime


class ReviewOut(CamelModel):
    """一条评价（含它的追评与回复）。

    ``nickname`` 在匿名评价里是固定文案，``avatar`` 为空 —— 匿名是**服务端**决定要不要
    暴露，不能指望前端自己隐藏。
    """

    review_id: SnowflakeId
    score: int
    content: str | None = None
    images: list[ReviewImageOut] = Field(default_factory=list)
    anonymous: bool = False
    nickname: str
    avatar: str | None = None

    spec_text: str = Field(description="下单时的规格快照")
    buy_count: int = Field(description="买了几件")

    like_count: int = 0
    status: int
    status_text: str
    created_at: datetime

    is_follow_up: bool = False
    # 首评下方挂着的追评（展示上紧随首评）
    follow_up: ReviewOut | None = None
    replies: list[ReviewReplyOut] = Field(default_factory=list)

    # 运营审核视角才用得到
    suspect: bool = False
    need_second_audit: bool = False
    audit_remark: str | None = None


class ReviewListOut(CamelModel):
    items: list[ReviewOut]
    next_cursor: str | None = None
    has_more: bool = False


class ReviewStatsOut(CamelModel):
    """商品详情页的评分汇总。

    ★ **没有任何已发布评价时，``avg_score`` 与 ``good_rate`` 都是 null** ——
    显示 "5.0 分 / 100% 好评" 或 "0 分" 都是在误导用户（docs/12 §9）。
    """

    review_count: int
    avg_score: float | None = None
    good_rate: float | None = Field(default=None, description="4 星及以上占比，0~1")
    good_count: int = 0
    # 各星级条数，前端画评分分布条用（索引固定 1..5）
    score_distribution: dict[int, int] = Field(default_factory=dict)


class PendingReviewItemOut(CamelModel):
    """待评价的订单项。"""

    order_item_id: SnowflakeId
    spu_id: SnowflakeId
    sku_id: SnowflakeId
    title: str
    spec_text: str
    cover_image: str
    num: int
    receive_time: datetime | None = None


class PendingReviewListOut(CamelModel):
    items: list[PendingReviewItemOut]
    next_cursor: str | None = None
    has_more: bool = False


class ReviewEligibilityOut(CamelModel):
    """能否评价 + 为什么。前端进发评价页先问一次，避免填完才被拒。"""

    eligible: bool
    reason: str | None = Field(default=None, description="不可评价的错误码")
    message: str | None = Field(default=None, description="给用户看的原因")
    # 通过时要带上的信息：商品快照 + 是否还能追评
    order_item_id: SnowflakeId | None = None
    spu_id: SnowflakeId | None = None
    sku_id: SnowflakeId | None = None
    title: str | None = None
    spec_text: str | None = None
    cover_image: str | None = None
    num: int | None = None
    # 已评价过的订单项：把首评 id 带回去，让前端能直接去追评
    existing_review_id: SnowflakeId | None = None
    can_follow_up: bool = False


class AuditQueueItemOut(CamelModel):
    """审核队列里的一条（比普通评价多带审核需要的字段）。"""

    review: ReviewOut
    shop_id: SnowflakeId
    order_main_no: str
    reason_text: str = Field(description="命中的是什么：机审判定 / 抽检")


class AuditQueueOut(CamelModel):
    items: list[AuditQueueItemOut]
    next_cursor: str | None = None
    has_more: bool = False
    pending_count: int = Field(default=0, description="待审核总数（角标）")
    second_audit_count: int = Field(default=0, description="待抽检总数")


# ============================================================
# ORM → 响应
# ============================================================
ANONYMOUS_NAME = "匿名用户"


def mask_nickname(nickname: str) -> str:
    """昵称脱敏：只留首字符。评价列表是公开接口，不该暴露完整昵称。"""
    if not nickname:
        return ANONYMOUS_NAME
    return f"{nickname[0]}***" if len(nickname) > 1 else f"{nickname}***"


def to_image_out(path: str, *, prefix: str, thumb_path: str) -> ReviewImageOut:
    return ReviewImageOut(path=path, url=f"{prefix}{path}", thumb_url=f"{prefix}{thumb_path}")


def to_review_out(
    review: Review,
    *,
    media_prefix: str,
    thumb_of,
    nickname: str | None,
    avatar: str | None,
    replies: list[ReviewReply] | None = None,
    follow_up: Review | None = None,
    include_audit: bool = False,
) -> ReviewOut:
    """ORM → DTO。

    ``nickname``/``avatar`` 由调用方批量取好后传入（列表页不能一条一条查）。
    匿名评价在这里统一替换掉 —— 不暴露真实昵称是**服务端**的责任。
    """
    anonymous = bool(review.anonymous)
    return ReviewOut(
        review_id=review.id,
        score=review.score,
        content=review.content,
        images=[
            to_image_out(p, prefix=media_prefix, thumb_path=thumb_of(p))
            for p in (review.images or [])
        ],
        anonymous=anonymous,
        nickname=ANONYMOUS_NAME if anonymous else mask_nickname(nickname or ""),
        avatar=None if anonymous else avatar,
        spec_text=review.sku_spec_snap,
        buy_count=review.buy_count,
        like_count=review.like_count,
        status=review.status,
        status_text=REVIEW_STATUS_TEXT.get(int(review.status), "未知"),
        created_at=review.created_at,
        is_follow_up=bool(review.is_follow_up),
        follow_up=(
            to_review_out(
                follow_up,
                media_prefix=media_prefix,
                thumb_of=thumb_of,
                nickname=nickname,
                avatar=avatar,
                replies=None,
            )
            if follow_up is not None
            else None
        ),
        replies=[
            ReviewReplyOut(
                reply_type=r.reply_type,
                reply_type_text=REPLY_TYPE_TEXT.get(int(r.reply_type), "回复"),
                content=r.content,
                created_at=r.created_at,
            )
            for r in (replies or [])
        ],
        suspect=bool(review.suspect) if include_audit else False,
        need_second_audit=bool(review.need_second_audit) if include_audit else False,
        audit_remark=review.audit_remark if include_audit else None,
    )


def audit_action_text(action: str) -> str:
    return AUDIT_ACTION_TEXT.get(action, action)


__all__ = [
    "AuditQueueItemOut",
    "AuditQueueOut",
    "MerchantReplyRequest",
    "PendingReviewItemOut",
    "PendingReviewListOut",
    "ReviewAuditRequest",
    "ReviewEligibilityOut",
    "ReviewEligibilityRequest",
    "ReviewFollowUpRequest",
    "ReviewImageOut",
    "ReviewListOut",
    "ReviewOut",
    "ReviewReplyOut",
    "ReviewStatsOut",
    "ReviewSubmitRequest",
    "mask_nickname",
    "to_review_out",
]
