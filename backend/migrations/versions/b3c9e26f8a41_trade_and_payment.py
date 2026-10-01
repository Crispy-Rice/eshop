"""trade and payment modules

Revision ID: b3c9e26f8a41
Revises: a2b7d14e6f38
Create Date: 2026-10-01 19:00:00.000000

订单母子单、发货单、状态流水，以及最小模拟支付。
见 docs/07-order-and-split.md §3 与 docs/09-payment.md
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3c9e26f8a41'
down_revision: Union[str, Sequence[str], None] = 'a2b7d14e6f38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True, precision=3)


def upgrade() -> None:
    """Upgrade schema."""
    # ==================================================================
    # 母单
    # ==================================================================
    op.create_table(
        "order_main",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("order_main_no", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "request_id", sa.String(length=160), nullable=False, comment="下单请求的幂等键"
        ),
        sa.Column("shop_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("total_amount", sa.BigInteger(), nullable=False, comment="商品原价总额"),
        sa.Column("item_discount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("shop_discount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("platform_discount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "coupon_amount",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="券优惠总额（含店铺券+平台券），是上面各项的子集",
        ),
        sa.Column("point_deduction", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("point_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("freight_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("payable_amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "discount_amount",
            sa.BigInteger(),
            sa.Computed(
                "item_discount + shop_discount + platform_discount + point_deduction",
                persisted=True,
            ),
            nullable=False,
            comment="优惠合计（生成列）",
        ),
        sa.Column(
            "paid_amount",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="实付（支付回调写入）",
        ),
        sa.Column(
            "refunded_amount",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="累计已退",
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("10"),
            nullable=False,
            comment="见 STATUS_TEXT",
        ),
        sa.Column(
            "pay_status",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="0未付 1已付 2部分退 3全退",
        ),
        sa.Column("receiver_name", sa.String(length=64), nullable=False),
        sa.Column("receiver_phone", sa.String(length=20), nullable=False),
        sa.Column("receiver_province", sa.String(length=32), nullable=False),
        sa.Column("receiver_city", sa.String(length=32), nullable=False),
        sa.Column("receiver_district", sa.String(length=32), nullable=False),
        sa.Column("receiver_detail", sa.String(length=255), nullable=False),
        sa.Column(
            "region_code", sa.String(length=16), nullable=False, comment="运费计算与统计用"
        ),
        sa.Column(
            "freight_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="运费计算明细快照（模板改了也能追溯）",
        ),
        sa.Column(
            "order_source",
            sa.SmallInteger(),
            server_default=sa.text("4"),
            nullable=False,
            comment="4=PC",
        ),
        sa.Column("buyer_remark", sa.String(length=255), nullable=True),
        sa.Column("create_time", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("pay_deadline", TS, nullable=False, comment="创建 + 30min"),
        sa.Column("pay_time", TS, nullable=True),
        sa.Column("finish_time", TS, nullable=True),
        sa.Column("close_time", TS, nullable=True),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "payable_amount = total_amount - item_discount - shop_discount "
            "- platform_discount - point_deduction + freight_amount AND payable_amount >= 0",
            name=op.f("ck_order_main_payable_conserved"),
        ),
        sa.CheckConstraint(
            "refunded_amount <= paid_amount", name=op.f("ck_order_main_refund_not_exceed_paid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_main")),
        sa.UniqueConstraint("order_main_no", name="uk_order_main_no"),
        schema="trade",
        comment="母单（支付与用户视角）",
    )
    op.create_index(
        "idx_order_main_user",
        "order_main",
        ["user_id", "status", sa.text("create_time DESC")],
        schema="trade",
    )
    # ★ 下单幂等的持久化兜底
    op.create_index(
        "uk_order_main_request",
        "order_main",
        ["user_id", "request_id"],
        unique=True,
        schema="trade",
    )
    # 超时关单扫描：部分索引，只含待付款订单
    op.create_index(
        "idx_order_main_pay_deadline",
        "order_main",
        ["pay_deadline"],
        schema="trade",
        postgresql_where=sa.text("status = 10"),
    )

    # ==================================================================
    # 子单
    # ==================================================================
    op.create_table(
        "order_sub",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "order_sub_no", sa.String(length=32), nullable=False, comment="母单号 + '-' + 序号"
        ),
        sa.Column("order_main_no", sa.String(length=32), nullable=False),
        sa.Column(
            "user_id", sa.BigInteger(), nullable=False, comment="冗余，便于按用户查"
        ),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("shop_name_snap", sa.String(length=64), nullable=False, comment="店铺名快照"),
        sa.Column("total_amount", sa.BigInteger(), nullable=False),
        sa.Column("item_discount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("shop_discount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("platform_discount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("coupon_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("point_deduction", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("freight_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("payable_amount", sa.BigInteger(), nullable=False),
        sa.Column("refunded_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("10"),
            nullable=False,
            comment="见 STATUS_TEXT",
        ),
        sa.Column(
            "delivery_status",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="0未发 1部分 2全部 3已签收",
        ),
        sa.Column("delivery_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "has_aftersale",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="是否有进行中的售后",
        ),
        sa.Column("can_aftersale", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_reviewed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("create_time", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("deliver_time", TS, nullable=True),
        sa.Column("receive_time", TS, nullable=True),
        sa.Column("finish_time", TS, nullable=True),
        sa.Column("close_time", TS, nullable=True),
        sa.Column("auto_finish_time", TS, nullable=True, comment="自动确认收货时间"),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "payable_amount = total_amount - item_discount - shop_discount "
            "- platform_discount - point_deduction + freight_amount AND payable_amount >= 0",
            name=op.f("ck_order_sub_payable_conserved"),
        ),
        sa.CheckConstraint(
            "refunded_amount <= payable_amount", name=op.f("ck_order_sub_refund_not_exceed_payable")
        ),
        sa.ForeignKeyConstraint(
            ["order_main_no"],
            ["trade.order_main.order_main_no"],
            name=op.f("fk_order_sub_order_main_no"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_sub")),
        sa.UniqueConstraint("order_sub_no", name="uk_order_sub_no"),
        schema="trade",
        comment="子单（商家履约视角）",
    )
    op.create_index("idx_order_sub_main", "order_sub", ["order_main_no"], schema="trade")
    op.create_index(
        "idx_order_sub_user",
        "order_sub",
        ["user_id", "status", sa.text("create_time DESC")],
        schema="trade",
    )
    op.create_index(
        "idx_order_sub_shop",
        "order_sub",
        ["shop_id", "status", sa.text("create_time DESC")],
        schema="trade",
    )
    op.create_index(
        "idx_order_sub_auto_finish",
        "order_sub",
        ["auto_finish_time"],
        schema="trade",
        postgresql_where=sa.text("status = 30"),
    )

    # ==================================================================
    # 订单项（商品快照）
    # ==================================================================
    op.create_table(
        "order_item",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("order_main_no", sa.String(length=32), nullable=False),
        sa.Column("order_sub_no", sa.String(length=32), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("spu_id", sa.BigInteger(), nullable=False),
        sa.Column("sku_id", sa.BigInteger(), nullable=False),
        sa.Column("spu_title_snap", sa.String(length=120), nullable=False),
        sa.Column(
            "sku_spec_snap", sa.String(length=255), nullable=False, comment="如 暗夜黑;256G"
        ),
        sa.Column("cover_image_snap", sa.String(length=255), nullable=False),
        sa.Column(
            "unit_price_snap", sa.BigInteger(), nullable=False, comment="下单时单价"
        ),
        sa.Column(
            "weight_g_snap", sa.Integer(), nullable=False, comment="退款算运费要用"
        ),
        sa.Column("num", sa.Integer(), nullable=False),
        sa.Column(
            "item_amount",
            sa.BigInteger(),
            nullable=False,
            comment="= unit_price_snap * num，行小计",
        ),
        sa.Column(
            "discount_amount",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="本行承担的总优惠",
        ),
        sa.Column("coupon_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("promo_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("point_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "payable_amount",
            sa.BigInteger(),
            nullable=False,
            comment="本行实付 = item_amount - discount_amount",
        ),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("num > 0", name=op.f("ck_order_item_num_positive")),
        sa.CheckConstraint(
            "discount_amount >= 0", name=op.f("ck_order_item_discount_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_item")),
        schema="trade",
        comment="订单项（商品快照）",
    )
    op.create_index("idx_order_item_sub", "order_item", ["order_sub_no"], schema="trade")
    op.create_index("idx_order_item_main", "order_item", ["order_main_no"], schema="trade")
    op.create_index("idx_order_item_sku", "order_item", ["sku_id"], schema="trade")

    # ==================================================================
    # 优惠快照
    # ==================================================================
    op.create_table(
        "order_discount_snapshot",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("order_main_no", sa.String(length=32), nullable=False),
        sa.Column("order_sub_no", sa.String(length=32), nullable=True, comment="空表示平台级"),
        sa.Column(
            "level", sa.SmallInteger(), nullable=False, comment="0单品 1店铺 2平台 3积分"
        ),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("source_name", sa.String(length=128), nullable=False, comment="快照名称"),
        sa.Column(
            "rule_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="规则快照：threshold/discountValue/rate/scope",
        ),
        sa.Column("discount_amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_discount_snapshot")),
        sa.UniqueConstraint(
            "order_main_no", "level", "source_type", "source_id", name="uk_discount_snap"
        ),
        schema="trade",
        comment="订单优惠快照（审计与退款依据）",
    )
    op.create_index(
        "idx_discount_snap_source",
        "order_discount_snapshot",
        ["source_type", "source_id"],
        schema="trade",
    )

    # ==================================================================
    # 发货单
    # ==================================================================
    op.create_table(
        "delivery_order",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("delivery_no", sa.String(length=32), nullable=False),
        sa.Column("order_sub_no", sa.String(length=32), nullable=False),
        sa.Column("order_main_no", sa.String(length=32), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("warehouse_id", sa.BigInteger(), nullable=False),
        sa.Column("express_company", sa.String(length=32), nullable=False),
        sa.Column("express_no", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1待发 2已发 3已签收",
        ),
        sa.Column("deliver_time", TS, nullable=True),
        sa.Column("receive_time", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_sub_no"],
            ["trade.order_sub.order_sub_no"],
            name=op.f("fk_delivery_order_order_sub_no"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_order")),
        sa.UniqueConstraint("delivery_no", name="uk_delivery_no"),
        sa.UniqueConstraint(
            "express_company", "express_no", name="uk_delivery_express"
        ),
        schema="trade",
        comment="发货单",
    )
    op.create_index("idx_delivery_sub", "delivery_order", ["order_sub_no"], schema="trade")

    op.create_table(
        "delivery_item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("delivery_no", sa.String(length=32), nullable=False),
        sa.Column("order_item_id", sa.BigInteger(), nullable=False),
        sa.Column("num", sa.Integer(), nullable=False),
        sa.CheckConstraint("num > 0", name=op.f("ck_delivery_item_num_positive")),
        sa.ForeignKeyConstraint(
            ["delivery_no"],
            ["trade.delivery_order.delivery_no"],
            name=op.f("fk_delivery_item_delivery_no"),
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"],
            ["trade.order_item.id"],
            name=op.f("fk_delivery_item_order_item_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_item")),
        sa.UniqueConstraint("delivery_no", "order_item_id", name="uk_delivery_item"),
        schema="trade",
    )

    # ==================================================================
    # 状态流水（不可变审计日志）
    # ==================================================================
    op.create_table(
        "order_state_flow",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "order_type", sa.SmallInteger(), nullable=False, comment="1母单 2子单 3售后单"
        ),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.SmallInteger(), nullable=False),
        sa.Column("to_status", sa.SmallInteger(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column(
            "operator_type",
            sa.SmallInteger(),
            nullable=False,
            comment="1用户 2商家 3系统 4平台客服",
        ),
        sa.Column("operator_id", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.Column(
            "extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="上下文快照"
        ),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_state_flow")),
        schema="trade",
    )
    op.create_index(
        "idx_state_flow_order", "order_state_flow", ["order_no", "created_at"], schema="trade"
    )

    # ==================================================================
    # 支付（最小模拟渠道）
    # ==================================================================
    op.create_table(
        "payment",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("pay_no", sa.String(length=32), nullable=False, comment="支付单号"),
        sa.Column(
            "order_main_no",
            sa.String(length=32),
            nullable=False,
            comment="跨 schema 不建外键（docs/13 §0.1）",
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False, comment="应付金额（分）"),
        sa.Column("paid_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "channel",
            sa.String(length=16),
            server_default=sa.text("'mock'"),
            nullable=False,
            comment="第一期只有 mock",
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="见 PAY_STATUS_TEXT",
        ),
        sa.Column("channel_trade_no", sa.String(length=64), nullable=True),
        sa.Column("pay_time", TS, nullable=True),
        sa.Column("close_time", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("amount >= 0", name=op.f("ck_payment_amount_non_negative")),
        sa.CheckConstraint("paid_amount >= 0", name=op.f("ck_payment_paid_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment")),
        sa.UniqueConstraint("pay_no", name="uk_payment_pay_no"),
        sa.UniqueConstraint("order_main_no", name="uk_payment_order_main_no"),
        schema="payment",
        comment="支付单",
    )
    op.create_index(
        "idx_payment_user",
        "payment",
        ["user_id", "status", sa.text("created_at DESC")],
        schema="payment",
    )

    op.create_table(
        "mock_channel_trade",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("out_trade_no", sa.String(length=32), nullable=False, comment="商户订单号"),
        sa.Column("pay_no", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="0待支付 1已支付",
        ),
        sa.Column("trade_no", sa.String(length=64), nullable=True, comment="渠道交易号"),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("paid_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mock_channel_trade")),
        sa.UniqueConstraint("out_trade_no", name="uk_mock_trade_out_trade_no"),
        schema="payment",
        comment="模拟渠道交易（仅 mock 模式）",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("mock_channel_trade", schema="payment")
    op.drop_index("idx_payment_user", table_name="payment", schema="payment")
    op.drop_table("payment", schema="payment")

    op.drop_index("idx_state_flow_order", table_name="order_state_flow", schema="trade")
    op.drop_table("order_state_flow", schema="trade")
    op.drop_table("delivery_item", schema="trade")
    op.drop_index("idx_delivery_sub", table_name="delivery_order", schema="trade")
    op.drop_table("delivery_order", schema="trade")
    op.drop_index("idx_discount_snap_source", table_name="order_discount_snapshot", schema="trade")
    op.drop_table("order_discount_snapshot", schema="trade")
    op.drop_index("idx_order_item_sku", table_name="order_item", schema="trade")
    op.drop_index("idx_order_item_main", table_name="order_item", schema="trade")
    op.drop_index("idx_order_item_sub", table_name="order_item", schema="trade")
    op.drop_table("order_item", schema="trade")
    op.drop_index("idx_order_sub_auto_finish", table_name="order_sub", schema="trade")
    op.drop_index("idx_order_sub_shop", table_name="order_sub", schema="trade")
    op.drop_index("idx_order_sub_user", table_name="order_sub", schema="trade")
    op.drop_index("idx_order_sub_main", table_name="order_sub", schema="trade")
    op.drop_table("order_sub", schema="trade")
    op.drop_index("idx_order_main_pay_deadline", table_name="order_main", schema="trade")
    op.drop_index("uk_order_main_request", table_name="order_main", schema="trade")
    op.drop_index("idx_order_main_user", table_name="order_main", schema="trade")
    op.drop_table("order_main", schema="trade")
