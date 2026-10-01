"""review module

Revision ID: d4a8f13b7c52
Revises: c7f2a95d1e64
Create Date: 2026-10-01 22:00:00.000000

商品评价与评价回复，以及把 product.spu.avg_score 改成"零评价为 NULL"。
见 docs/12-review.md §2.1 / §6。

``review`` schema 已由 baseline 迁移创建（4eccd9482ee8），这里不重复建。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd4a8f13b7c52'
down_revision: Union[str, Sequence[str], None] = 'c7f2a95d1e64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True, precision=3)

# 只索引"已发布且是首评"的那些行 —— 商品详情页的三条查询都用它
PUBLISHED_FIRST = "status = 1 AND NOT is_follow_up"


def upgrade() -> None:
    """Upgrade schema."""
    # ==================================================================
    # 商品评价
    # ==================================================================
    op.create_table(
        "review",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("spu_id", sa.BigInteger(), nullable=False),
        sa.Column("sku_id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False, comment="冗余，免跨模块查"),
        sa.Column("order_item_id", sa.BigInteger(), nullable=False),
        sa.Column("order_sub_no", sa.String(length=32), nullable=False),
        sa.Column("order_main_no", sa.String(length=32), nullable=False),
        sa.Column(
            "sku_spec_snap",
            sa.String(length=255),
            nullable=False,
            comment="如 暗夜黑;256G",
        ),
        sa.Column("buy_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False, comment="1-5 星"),
        sa.Column("content", sa.String(length=2000), nullable=True),
        sa.Column(
            "images",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="图片相对路径数组，最多 9 张",
        ),
        sa.Column(
            "anonymous",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="匿名评价不展示昵称",
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="见 core.enums.ReviewStatus：0待审核 1已发布 2已屏蔽 3审核不通过",
        ),
        sa.Column(
            "need_second_audit",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="机审放行但标记待抽检",
        ),
        sa.Column(
            "suspect",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="疑似刷评（签收后立刻评价等）",
        ),
        sa.Column("audit_remark", sa.String(length=255), nullable=True),
        sa.Column("like_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reply_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "rank_score",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="推荐排序用，发布时算好",
        ),
        sa.Column("is_follow_up", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "points_awarded",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="本期恒为 0",
        ),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("score BETWEEN 1 AND 5", name=op.f("ck_review_score_range")),
        sa.CheckConstraint(
            "is_follow_up = (parent_id IS NOT NULL)", name=op.f("ck_review_follow_up")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(images) = 'array' AND jsonb_array_length(images) <= 9",
            name=op.f("ck_review_images_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["review.review.id"], name=op.f("fk_review_parent_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review")),
        schema="review",
        comment="商品评价",
    )
    # ★ 每个订单项只有一条首评 —— "只能评论一次"的核心保证
    op.create_index(
        "uk_review_order_item",
        "review",
        ["order_item_id"],
        unique=True,
        schema="review",
        postgresql_where=sa.text("NOT is_follow_up"),
    )
    # ★ 一条首评只有一条追评
    op.create_index(
        "uk_review_follow_up",
        "review",
        ["parent_id"],
        unique=True,
        schema="review",
        postgresql_where=sa.text("is_follow_up"),
    )
    op.create_index(
        "idx_review_spu_list",
        "review",
        ["spu_id", sa.text("created_at DESC"), sa.text("id DESC")],
        schema="review",
        postgresql_where=sa.text(PUBLISHED_FIRST),
    )
    op.create_index(
        "idx_review_spu_rank",
        "review",
        ["spu_id", sa.text("rank_score DESC"), sa.text("id DESC")],
        schema="review",
        postgresql_where=sa.text(PUBLISHED_FIRST),
    )
    op.create_index(
        "idx_review_spu_score",
        "review",
        ["spu_id", "score", sa.text("created_at DESC")],
        schema="review",
        postgresql_where=sa.text(PUBLISHED_FIRST),
    )
    op.create_index(
        "idx_review_user", "review", ["user_id", sa.text("created_at DESC")], schema="review"
    )
    op.create_index("idx_review_order", "review", ["order_main_no"], schema="review")
    op.create_index(
        "idx_review_shop_status",
        "review",
        ["shop_id", "status", sa.text("created_at DESC")],
        schema="review",
    )
    op.create_index(
        "idx_review_status", "review", ["status", sa.text("created_at DESC")], schema="review"
    )

    # ==================================================================
    # 评价回复
    # ==================================================================
    op.create_table(
        "review_reply",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("review_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "reply_type", sa.SmallInteger(), nullable=False, comment="1商家回复 2平台回复"
        ),
        sa.Column(
            "replier_id",
            sa.BigInteger(),
            nullable=False,
            comment="商家回复时是 shop_id，平台回复时是运营 user_id",
        ),
        sa.Column("content", sa.String(length=500), nullable=False),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="0待审 1已发布 2已删除",
        ),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_id"], ["review.review.id"], name=op.f("fk_review_reply_review_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_reply")),
        schema="review",
        comment="评价回复",
    )
    op.create_index(
        "idx_review_reply_review", "review_reply", ["review_id", "status"], schema="review"
    )

    # ==================================================================
    # product.spu.avg_score：零评价必须是 NULL，而不是 5.00
    # ==================================================================
    # 原来的 `NOT NULL DEFAULT 5.00` 是个坑：没有任何评价的商品会读出 5.00，
    # 前端就显示"5.0 分"—— 正是 docs/12 §9 要避免的"无评价不展示好评率"。
    # 改成"零评价 ⇒ NULL"的数据库不变量，展示层与接口层都不必再各自判断。
    op.alter_column(
        "spu",
        "avg_score",
        existing_type=sa.Numeric(precision=3, scale=2),
        nullable=True,
        server_default=None,
        comment="平均分；无评价时为 NULL",
        schema="product",
    )
    op.execute("UPDATE product.spu SET avg_score = NULL WHERE review_count = 0")


def downgrade() -> None:
    """Downgrade schema."""
    # 回滚时把 NULL 补回 5.00，否则 SET NOT NULL 会失败
    op.execute("UPDATE product.spu SET avg_score = 5.00 WHERE avg_score IS NULL")
    op.alter_column(
        "spu",
        "avg_score",
        existing_type=sa.Numeric(precision=3, scale=2),
        nullable=False,
        server_default=sa.text("5.00"),
        schema="product",
    )

    op.drop_index("idx_review_reply_review", table_name="review_reply", schema="review")
    op.drop_table("review_reply", schema="review")

    op.drop_index("idx_review_status", table_name="review", schema="review")
    op.drop_index("idx_review_shop_status", table_name="review", schema="review")
    op.drop_index("idx_review_order", table_name="review", schema="review")
    op.drop_index("idx_review_user", table_name="review", schema="review")
    op.drop_index("idx_review_spu_score", table_name="review", schema="review")
    op.drop_index("idx_review_spu_rank", table_name="review", schema="review")
    op.drop_index("idx_review_spu_list", table_name="review", schema="review")
    op.drop_index("uk_review_follow_up", table_name="review", schema="review")
    op.drop_index("uk_review_order_item", table_name="review", schema="review")
    op.drop_table("review", schema="review")
