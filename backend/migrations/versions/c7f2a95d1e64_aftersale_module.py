"""aftersale module

Revision ID: c7f2a95d1e64
Revises: b3c9e26f8a41
Create Date: 2026-10-01 21:00:00.000000

售后单与售后单项，以及为退款链路在既有表上补的列。
见 docs/08-aftersale.md 与 docs/13-schema.md §3。

``aftersale`` schema 已由 baseline 迁移创建（4eccd9482ee8），这里不重复建。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c7f2a95d1e64'
down_revision: Union[str, Sequence[str], None] = 'b3c9e26f8a41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True, precision=3)

# 「进行中」的售后状态。部分唯一索引用它，终态不占用
ACTIVE_STATUSES = "(10, 20, 30, 40, 50, 60)"


def upgrade() -> None:
    """Upgrade schema."""
    # ==================================================================
    # 售后单
    # ==================================================================
    op.create_table(
        "refund_order",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("refund_no", sa.String(length=32), nullable=False, comment="售后单号"),
        sa.Column("order_sub_no", sa.String(length=32), nullable=False),
        sa.Column("order_main_no", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "request_id", sa.String(length=160), nullable=False, comment="申请售后的幂等键"
        ),
        sa.Column(
            "refund_type", sa.SmallInteger(), nullable=False, comment="见 REFUND_TYPE_TEXT"
        ),
        sa.Column("reason_type", sa.SmallInteger(), nullable=False, comment="见 REASON_TEXT"),
        sa.Column("reason_desc", sa.String(length=255), nullable=True),
        sa.Column(
            "images",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="凭证图片",
        ),
        sa.Column(
            "refund_amount",
            sa.BigInteger(),
            nullable=False,
            comment="商品退款金额（不含运费）",
        ),
        sa.Column(
            "refund_freight",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="退还的运费",
        ),
        sa.Column(
            "freight_bearer",
            sa.SmallInteger(),
            server_default=sa.text("2"),
            nullable=False,
            comment="退货运费承担方 1买家 2商家 3平台",
        ),
        sa.Column(
            "refund_points",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="返还积分。本期无积分体系，恒为 0",
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("10"),
            nullable=False,
            comment="见 core.enums.RefundStatus",
        ),
        sa.Column(
            "source_status",
            sa.SmallInteger(),
            nullable=False,
            comment="申请时的子单状态，拒绝/撤销时恢复用",
        ),
        sa.Column("merchant_remark", sa.String(length=255), nullable=True),
        sa.Column("reject_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "quality_result",
            sa.SmallInteger(),
            nullable=True,
            comment="1合格 2不合格，见 QUALITY_RESULT_TEXT",
        ),
        sa.Column("quality_remark", sa.String(length=255), nullable=True),
        sa.Column(
            "quality_images",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="质检留证照片",
        ),
        sa.Column("return_express", sa.String(length=32), nullable=True, comment="退货快递公司"),
        sa.Column("return_express_no", sa.String(length=64), nullable=True, comment="退货快递单号"),
        sa.Column("apply_time", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("merchant_handle_time", TS, nullable=True),
        sa.Column("return_time", TS, nullable=True),
        sa.Column("receive_time", TS, nullable=True),
        sa.Column("quality_time", TS, nullable=True),
        sa.Column("refund_time", TS, nullable=True),
        sa.Column("close_time", TS, nullable=True),
        sa.Column("deadline", TS, nullable=True, comment="当前环节的截止时间，终态为 NULL"),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "refund_amount >= 0", name=op.f("ck_refund_order_refund_amount_non_negative")
        ),
        sa.CheckConstraint(
            "refund_freight >= 0", name=op.f("ck_refund_order_refund_freight_non_negative")
        ),
        sa.CheckConstraint(
            "refund_points >= 0", name=op.f("ck_refund_order_refund_points_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refund_order")),
        sa.UniqueConstraint("refund_no", name="uk_refund_no"),
        schema="aftersale",
        comment="售后单（退款与退货）",
    )
    op.create_index(
        "uk_refund_request",
        "refund_order",
        ["user_id", "request_id"],
        unique=True,
        schema="aftersale",
    )
    # ★ 一个子单同时只能有一个进行中的售后；终态不占用，可以再发起新的
    op.create_index(
        "uk_refund_sub_active",
        "refund_order",
        ["order_sub_no"],
        unique=True,
        schema="aftersale",
        postgresql_where=sa.text(f"status IN {ACTIVE_STATUSES}"),
    )
    op.create_index(
        "idx_refund_user_status",
        "refund_order",
        ["user_id", "status", sa.text("created_at DESC")],
        schema="aftersale",
    )
    op.create_index(
        "idx_refund_shop_status",
        "refund_order",
        ["shop_id", "status", sa.text("created_at DESC")],
        schema="aftersale",
    )
    op.create_index("idx_refund_main", "refund_order", ["order_main_no"], schema="aftersale")
    op.create_index("idx_refund_sub", "refund_order", ["order_sub_no"], schema="aftersale")
    # 超时扫描：只含还带 deadline 的进行中售后，体积极小
    op.create_index(
        "idx_refund_deadline",
        "refund_order",
        ["deadline"],
        schema="aftersale",
        postgresql_where=sa.text("deadline IS NOT NULL"),
    )

    # ==================================================================
    # 售后单项
    # ==================================================================
    op.create_table(
        "refund_item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("refund_no", sa.String(length=32), nullable=False),
        sa.Column("order_item_id", sa.BigInteger(), nullable=False),
        sa.Column("sku_id", sa.BigInteger(), nullable=False),
        sa.Column("refund_num", sa.Integer(), nullable=False, comment="本次退的件数"),
        sa.Column(
            "refund_amount", sa.BigInteger(), nullable=False, comment="该行的商品退款金额（分）"
        ),
        sa.Column(
            "refund_points",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="本期恒为 0",
        ),
        sa.Column("spu_title_snap", sa.String(length=120), nullable=False),
        sa.Column("sku_spec_snap", sa.String(length=255), nullable=False),
        sa.Column("cover_image_snap", sa.String(length=255), nullable=False),
        sa.Column(
            "stock_restored",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="库存是否已回补",
        ),
        sa.Column("restore_time", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("refund_num > 0", name=op.f("ck_refund_item_refund_num_positive")),
        sa.CheckConstraint(
            "refund_amount >= 0", name=op.f("ck_refund_item_refund_amount_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["refund_no"],
            ["aftersale.refund_order.refund_no"],
            name=op.f("fk_refund_item_refund_no"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refund_item")),
        sa.UniqueConstraint("refund_no", "order_item_id", name="uk_refund_item"),
        schema="aftersale",
        comment="售后单项",
    )
    op.create_index(
        "idx_refund_item_order_item", "refund_item", ["order_item_id"], schema="aftersale"
    )

    # ==================================================================
    # trade.order_item：退款数量与金额
    # ==================================================================
    # 三列都是 DEFAULT 0，PG 11+ 的 ADD COLUMN NOT NULL DEFAULT 是 catalog-only，
    # 不重写表；历史行也就天然满足约束，无需回填
    op.add_column(
        "order_item",
        sa.Column(
            "refunding_num",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="申请中、尚未退款完成的件数。申请时预占，成功时转为 refunded_num",
        ),
        schema="trade",
    )
    op.add_column(
        "order_item",
        sa.Column(
            "refunded_num",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已退款完成的件数",
        ),
        schema="trade",
    )
    op.add_column(
        "order_item",
        sa.Column(
            "refunded_amount",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已退的商品款。差额法要看它",
        ),
        schema="trade",
    )
    op.create_check_constraint(
        op.f("ck_order_item_refund_num_conserved"),
        "order_item",
        "refunded_num + refunding_num <= num",
        schema="trade",
    )
    op.create_check_constraint(
        op.f("ck_order_item_refund_amount_conserved"),
        "order_item",
        "refunded_amount >= 0 AND refunded_amount <= payable_amount",
        schema="trade",
    )

    # ==================================================================
    # payment.payment：累计已退
    # ==================================================================
    op.add_column(
        "payment",
        sa.Column(
            "refunded_amount",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="累计已退（分）",
        ),
        schema="payment",
    )
    op.create_check_constraint(
        op.f("ck_payment_refund_conserved"),
        "payment",
        "refunded_amount >= 0 AND refunded_amount <= paid_amount",
        schema="payment",
    )

    # ==================================================================
    # payment.payment_refund：资金退款单
    # ==================================================================
    op.create_table(
        "payment_refund",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("refund_no", sa.String(length=32), nullable=False, comment="资金退款单号"),
        sa.Column(
            "refund_biz_no",
            sa.String(length=40),
            nullable=False,
            comment="业务退款号：售后单号，或 LATE:{pay_no}",
        ),
        sa.Column("pay_no", sa.String(length=32), nullable=False),
        sa.Column("order_main_no", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False, comment="退款金额（分）"),
        sa.Column(
            "channel",
            sa.String(length=16),
            server_default=sa.text("'mock'"),
            nullable=False,
            comment="与 payment.channel 一致",
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="见 PAY_REFUND_STATUS_TEXT",
        ),
        sa.Column("channel_refund_no", sa.String(length=64), nullable=True, comment="渠道退款单号"),
        sa.Column("fail_reason", sa.String(length=255), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_retry_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("create_time", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("success_time", TS, nullable=True),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_refund_amount_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_refund")),
        sa.UniqueConstraint("refund_no", name="uk_payment_refund_no"),
        # ★ 幂等的核心：一个售后单（或一笔晚付）只能有一次资金退款
        sa.UniqueConstraint("refund_biz_no", name="uk_payment_refund_biz"),
        schema="payment",
        comment="资金退款单",
    )
    op.create_index("idx_payment_refund_pay", "payment_refund", ["pay_no"], schema="payment")
    # 重试扫描：只含待退款与失败的，体积极小
    op.create_index(
        "idx_payment_refund_retry",
        "payment_refund",
        ["next_retry_at"],
        schema="payment",
        postgresql_where=sa.text("status IN (0, 3)"),
    )

    # ==================================================================
    # inventory.sku_stock：残次品池
    # ==================================================================
    op.add_column(
        "sku_stock",
        sa.Column(
            "defective",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="残次品，不计入 total 恒等式也不可售（退货质检不合格时增加）",
        ),
        schema="inventory",
    )
    op.create_check_constraint(
        op.f("ck_sku_stock_defective_non_negative"),
        "sku_stock",
        "defective >= 0",
        schema="inventory",
    )

    # ==================================================================
    # promotion.coupon_code：退款退券时的延长计数
    # ==================================================================
    op.add_column(
        "coupon_code",
        sa.Column(
            "extend_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="有效期被延长的次数",
        ),
        schema="promotion",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("coupon_code", "extend_count", schema="promotion")

    op.drop_constraint(
        op.f("ck_sku_stock_defective_non_negative"), "sku_stock", type_="check", schema="inventory"
    )
    op.drop_column("sku_stock", "defective", schema="inventory")

    op.drop_index("idx_payment_refund_retry", table_name="payment_refund", schema="payment")
    op.drop_index("idx_payment_refund_pay", table_name="payment_refund", schema="payment")
    op.drop_table("payment_refund", schema="payment")
    op.drop_constraint(
        op.f("ck_payment_refund_conserved"), "payment", type_="check", schema="payment"
    )
    op.drop_column("payment", "refunded_amount", schema="payment")

    op.drop_constraint(
        op.f("ck_order_item_refund_amount_conserved"),
        "order_item",
        type_="check",
        schema="trade",
    )
    op.drop_constraint(
        op.f("ck_order_item_refund_num_conserved"), "order_item", type_="check", schema="trade"
    )
    op.drop_column("order_item", "refunded_amount", schema="trade")
    op.drop_column("order_item", "refunded_num", schema="trade")
    op.drop_column("order_item", "refunding_num", schema="trade")

    op.drop_index("idx_refund_item_order_item", table_name="refund_item", schema="aftersale")
    op.drop_table("refund_item", schema="aftersale")

    op.drop_index("idx_refund_deadline", table_name="refund_order", schema="aftersale")
    op.drop_index("idx_refund_sub", table_name="refund_order", schema="aftersale")
    op.drop_index("idx_refund_main", table_name="refund_order", schema="aftersale")
    op.drop_index("idx_refund_shop_status", table_name="refund_order", schema="aftersale")
    op.drop_index("idx_refund_user_status", table_name="refund_order", schema="aftersale")
    op.drop_index("uk_refund_sub_active", table_name="refund_order", schema="aftersale")
    op.drop_index("uk_refund_request", table_name="refund_order", schema="aftersale")
    op.drop_table("refund_order", schema="aftersale")
