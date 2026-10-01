"""review 模块的 ORM 模型。

对应 docs/12-review.md §2.1 与 §6。

**两张表**：``review``（首评与追评共用一张表，靠 ``is_follow_up`` 区分）与
``review_reply``（商家/平台回复）。

**评论的粒度是订单项，不是商品**：用户买同一 SKU 三次（三个订单项）可以评三次 ——
每次评论对应一次真实购买。所以唯一键建在 ``order_item_id`` 上而不是
``(user_id, spu_id)``。

比文档多一列 ``shop_id``：商家要按店铺列评价、校验回复归属，冗余它就能单表索引扫描，
不必每次跨模块查 ``product.spu.shop_id``（本仓库到处冗余 ``shop_id``，风格一致）。

比文档多一列 ``rank_score``：「推荐排序」不适合实时计算（无法走索引），发布时把
「有内容 / 有图 / 被点赞」折算成分数存下来。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# 评价状态文案。**与 core.enums.ReviewStatus 一一对应**，
# 放在这里让 schemas 与异常消息共用同一份文案
REVIEW_STATUS_TEXT: dict[int, str] = {
    0: "待审核",
    1: "已发布",
    2: "已屏蔽",
    3: "审核不通过",
}

# 评分范围
MIN_SCORE = 1
MAX_SCORE = 5
# 几星算好评（统计「好评率」用）
GOOD_SCORE = 4

# 图片数量上限（docs/12 §2.1 的 CHECK 与 docs/15 §4 的数量限制）
MAX_IMAGES = 9
# 内容长度上限
MAX_CONTENT_LEN = 2000

# 评价窗口：签收后 N 天内可评（docs/12 §2.2）
REVIEW_WINDOW_DAYS = 30
# 追评窗口：首评后 N 天内可追评一次（docs/12 §5）
FOLLOW_UP_WINDOW_DAYS = 30

# 单用户每日评价上限（docs/12 §11）
DAILY_REVIEW_LIMIT = 20
# 签收后 N 秒内评价算"疑似没时间体验"（docs/12 §11）
SUSPECT_WITHIN_SECONDS = 60

# 回复类型
REPLY_MERCHANT = 1
REPLY_PLATFORM = 2

REPLY_TYPE_TEXT: dict[int, str] = {
    REPLY_MERCHANT: "商家回复",
    REPLY_PLATFORM: "平台回复",
}

# 回复状态
REPLY_PENDING = 0
REPLY_PUBLISHED = 1
REPLY_DELETED = 2

# 一条评价最多几条商家回复（防刷屏，docs/12 §6）
MAX_MERCHANT_REPLIES = 3

# 「推荐排序」的权重（docs/12 §3.1 的思路，具体数值是本项目的取值）
RANK_WITH_CONTENT = 1000
RANK_WITH_IMAGE = 500
RANK_SUSPECT_PENALTY = -500


class Review(Base):
    """商品评价。首评与追评共用一张表。

    ``is_follow_up`` + ``parent_id`` 由 ``ck_review_follow_up``（``is_follow_up =
    (parent_id IS NOT NULL)``）绑死，避免出现"标记为首评却带了 parent_id"的脏数据
    绕过两个唯一索引。
    """

    __tablename__ = "review"
    __table_args__ = (
        # ★ 每个订单项只有一条首评 —— "只能评论一次"的核心保证
        Index(
            "uk_review_order_item",
            "order_item_id",
            unique=True,
            postgresql_where=text("NOT is_follow_up"),
        ),
        # ★ 一条首评只有一条追评
        Index(
            "uk_review_follow_up",
            "parent_id",
            unique=True,
            postgresql_where=text("is_follow_up"),
        ),
        # 商品详情页评价列表（按时间倒序）
        Index(
            "idx_review_spu_list",
            "spu_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 1 AND NOT is_follow_up"),
        ),
        # 「推荐排序」
        Index(
            "idx_review_spu_rank",
            "spu_id",
            text("rank_score DESC"),
            text("id DESC"),
            postgresql_where=text("status = 1 AND NOT is_follow_up"),
        ),
        # 「按评分筛选」
        Index(
            "idx_review_spu_score",
            "spu_id",
            "score",
            text("created_at DESC"),
            postgresql_where=text("status = 1 AND NOT is_follow_up"),
        ),
        Index("idx_review_user", "user_id", text("created_at DESC")),
        Index("idx_review_order", "order_main_no"),
        # 商家后台按店铺筛选
        Index("idx_review_shop_status", "shop_id", "status", text("created_at DESC")),
        # 运营审核队列
        Index("idx_review_status", "status", text("created_at DESC")),
        CheckConstraint(f"score BETWEEN {MIN_SCORE} AND {MAX_SCORE}", name="score_range"),
        CheckConstraint("is_follow_up = (parent_id IS NOT NULL)", name="follow_up"),
        CheckConstraint(
            f"jsonb_typeof(images) = 'array' AND jsonb_array_length(images) <= {MAX_IMAGES}",
            name="images_shape",
        ),
        {"schema": "review", "comment": "商品评价"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spu_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 冗余：商家后台按店铺查、回复时校验归属，都靠它单表扫描
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="冗余，免跨模块查")

    # 来源订单 —— 这几列是"购后评价"的证据
    order_item_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_sub_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_main_no: Mapped[str] = mapped_column(String(32), nullable=False)
    # 用户当时买的规格（商品改了规格，历史评价不能跟着变）
    sku_spec_snap: Mapped[str] = mapped_column(String(255), nullable=False, comment="如 暗夜黑;256G")
    # 购买件数（展示"买了几件"）
    buy_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    score: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment=f"1-{MAX_SCORE} 星"
    )
    content: Mapped[str | None] = mapped_column(String(MAX_CONTENT_LEN))
    images: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment=f"图片相对路径数组，最多 {MAX_IMAGES} 张",
    )
    anonymous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="匿名评价不展示昵称"
    )

    status: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("0"),
        comment="见 core.enums.ReviewStatus：0待审核 1已发布 2已屏蔽 3审核不通过",
    )
    need_second_audit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="机审放行但标记待抽检"
    )
    suspect: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="疑似刷评（签收后立刻评价等）"
    )
    audit_remark: Mapped[str | None] = mapped_column(String(255))

    like_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # 「推荐排序」的排序分：有内容/有图加分，疑似刷评减分。计数与点赞变化时更新
    rank_score: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="推荐排序用，发布时算好"
    )

    # 追评是一条独立记录，parent_id 指向首评
    is_follow_up: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("review.review.id")
    )

    # 评价奖励积分。本期无积分体系，恒为 0（与 aftersale.refund_points 同口径）
    points_awarded: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="本期恒为 0"
    )

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class ReviewReply(Base):
    """评价回复。商家回复与平台回复共用一张表，靠 ``reply_type`` 区分。

    ``status`` 里留了 ``0 待审``：回复将来也要过机审时不用改表结构（本期回复直接发布）。
    """

    __tablename__ = "review_reply"
    __table_args__ = (
        Index("idx_review_reply_review", "review_id", "status"),
        {"schema": "review", "comment": "评价回复"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    review_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review.review.id"), nullable=False
    )
    reply_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1商家回复 2平台回复"
    )
    replier_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="商家回复时是 shop_id，平台回复时是运营 user_id"
    )
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="0待审 1已发布 2已删除"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
