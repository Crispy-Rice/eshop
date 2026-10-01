"""promotion module

Revision ID: f1a8c53d7e26
Revises: e7c4a81f2b93
Create Date: 2026-10-01 17:20:00.000000

优惠券与促销活动，见 docs/04-coupon.md 与 docs/05-promotion-engine.md。

建完表后**顺便灌入 9 条默认叠加规则**（docs/05 §4.1）——它们是引用数据，
空着的话所有优惠默认互斥，算价结果会不合理。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f1a8c53d7e26'
down_revision: Union[str, Sequence[str], None] = 'e7c4a81f2b93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 默认叠加规则（docs/05 §4.1）。设计原则：默认互斥、显式声明可叠。
# (type_a, type_b, stackable, priority, rule_name)
DEFAULT_STACK_RULES: list[tuple[str, str, bool, int, str]] = [
    ("PROMO_ITEM", "PROMO_ORDER_SHOP", True, 10, "单品促销 + 店铺满减，可叠"),
    ("PROMO_ITEM", "COUPON_PLATFORM", True, 10, "单品促销 + 平台券，可叠"),
    ("PROMO_ORDER_SHOP", "PROMO_ORDER_PLATFORM", True, 10, "店铺活动 + 平台活动，可叠"),
    ("COUPON_SHOP", "COUPON_PLATFORM", True, 10, "店铺券 + 平台券，可叠"),
    ("PROMO_ORDER_SHOP", "COUPON_SHOP", False, 20, "店铺活动与店铺券互斥"),
    ("PROMO_ORDER_PLATFORM", "COUPON_PLATFORM", False, 20, "平台活动与平台券互斥（防资损）"),
    ("COUPON_PLATFORM", "COUPON_PLATFORM", False, 20, "平台券之间互斥（同层只能一张）"),
    ("COUPON_SHOP", "COUPON_SHOP", False, 20, "同店券之间互斥"),
    ("POINT", "COUPON_PLATFORM", True, 10, "积分与券可叠（积分放最后）"),
    ("PROMO_ITEM", "PROMO_ITEM", False, 20, "单品促销之间互斥（取最优）"),
]


def upgrade() -> None:
    """Upgrade schema."""
    # ------------------------------------------------------------------
    # 券模板
    # ------------------------------------------------------------------
    op.create_table(
        "coupon_template",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "shop_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="0=平台券，>0=店铺券",
        ),
        sa.Column("name", sa.String(length=64), nullable=False, comment='如"满200减30"'),
        sa.Column(
            "type", sa.SmallInteger(), nullable=False, comment="1满减 2折扣 3无门槛 4兑换 5运费"
        ),
        sa.Column(
            "get_type",
            sa.SmallInteger(),
            nullable=False,
            comment="1主动领取 2系统发放 3兑换码 4活动 5新客",
        ),
        sa.Column(
            "discount_value",
            sa.BigInteger(),
            nullable=False,
            comment="满减=减免额(分)；折扣=折扣率(8500=85折)",
        ),
        sa.Column(
            "max_discount",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="折扣券封顶(分)，0=不限",
        ),
        sa.Column(
            "threshold",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="使用门槛(分)，0=无门槛",
        ),
        sa.Column("total_count", sa.Integer(), nullable=False, comment="发行总量"),
        sa.Column(
            "issued_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已发放（DB 账本）",
        ),
        sa.Column("used_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "per_user_limit",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="每人限领",
        ),
        sa.Column(
            "valid_type", sa.SmallInteger(), nullable=False, comment="1固定区间 2领取后N天"
        ),
        sa.Column(
            "valid_start", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=True
        ),
        sa.Column("valid_end", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=True),
        sa.Column("valid_days", sa.Integer(), nullable=True, comment="领取后有效天数"),
        sa.Column(
            "scope_type",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1全场 2指定商品 3指定类目 4指定店铺",
        ),
        sa.Column(
            "scope_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="spuId/skuId/categoryId/shopId 数组",
        ),
        sa.Column(
            "exclude_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="排除范围",
        ),
        sa.Column(
            "stackable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="可与同层其它优惠叠加",
        ),
        sa.Column(
            "exclusive_with_promo",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="与活动互斥",
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1未开始 2进行中 3已结束 4已作废",
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # ★ DB 层的超发硬约束
        sa.CheckConstraint(
            "issued_count <= total_count", name=op.f("ck_coupon_template_issued_not_exceed")
        ),
        sa.CheckConstraint(
            "issued_count >= 0 AND used_count >= 0",
            name=op.f("ck_coupon_template_counts_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coupon_template")),
        schema="promotion",
        comment="优惠券模板",
    )
    op.create_index(
        "idx_coupon_tpl_status_time",
        "coupon_template",
        ["status", "valid_end"],
        schema="promotion",
    )
    op.create_index(
        "idx_coupon_tpl_shop", "coupon_template", ["shop_id", "status"], schema="promotion"
    )

    # ------------------------------------------------------------------
    # 券实例
    # ------------------------------------------------------------------
    op.create_table(
        "coupon_code",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("coupon_template_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "code", sa.String(length=32), nullable=False, comment="可读券码，客服核销用"
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="见 CODE_STATUS_TEXT",
        ),
        sa.Column(
            "valid_start",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            nullable=False,
        ),
        sa.Column(
            "valid_end", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=False
        ),
        sa.Column("locked_order_no", sa.String(length=32), nullable=True),
        sa.Column("locked_at", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=True),
        sa.Column("used_order_no", sa.String(length=32), nullable=True),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=True),
        sa.Column(
            "use_amount", sa.BigInteger(), nullable=True, comment="实际抵扣金额(分)"
        ),
        sa.Column(
            "source",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1领取 2系统 3兑换 4活动 5退回",
        ),
        sa.Column(
            "received_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("valid_end >= valid_start", name=op.f("ck_coupon_code_valid_range")),
        sa.ForeignKeyConstraint(
            ["coupon_template_id"],
            ["promotion.coupon_template.id"],
            name=op.f("fk_coupon_code_coupon_template_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coupon_code")),
        sa.UniqueConstraint("code", name="uk_coupon_code"),
        schema="promotion",
        comment="用户优惠券实例",
    )
    op.create_index(
        "idx_coupon_code_user_status",
        "coupon_code",
        ["user_id", "status", "valid_end"],
        schema="promotion",
    )
    op.create_index(
        "idx_coupon_code_tpl_user",
        "coupon_code",
        ["coupon_template_id", "user_id"],
        schema="promotion",
    )
    op.create_index(
        "idx_coupon_code_locked",
        "coupon_code",
        ["locked_order_no"],
        schema="promotion",
        postgresql_where=sa.text("locked_order_no IS NOT NULL"),
    )
    # 过期扫描只扫"未使用"的券
    op.create_index(
        "idx_coupon_code_expire",
        "coupon_code",
        ["valid_end"],
        schema="promotion",
        postgresql_where=sa.text("status = 1"),
    )

    # ------------------------------------------------------------------
    # 用户领取计数（限领的 DB 保证）
    # ------------------------------------------------------------------
    op.create_table(
        "coupon_user_quota",
        sa.Column("coupon_template_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("received", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint(
            "coupon_template_id", "user_id", name="pk_coupon_user_quota"
        ),
        schema="promotion",
    )

    # ------------------------------------------------------------------
    # 领券流水
    # ------------------------------------------------------------------
    op.create_table(
        "coupon_receive_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("coupon_template_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("coupon_code_id", sa.BigInteger(), nullable=True),
        sa.Column("channel", sa.String(length=32), server_default=sa.text("''"), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column(
            "idempotency_key", sa.String(length=64), nullable=True, comment="客户端重试的幂等键"
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coupon_receive_log")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uk_coupon_receive_log_idempotency_key")),
        schema="promotion",
        comment="领券流水",
    )
    op.create_index(
        "idx_coupon_receive_tpl_user",
        "coupon_receive_log",
        ["coupon_template_id", "user_id"],
        schema="promotion",
    )
    op.create_index(
        "idx_coupon_receive_ip_time", "coupon_receive_log", ["ip", "created_at"], schema="promotion"
    )

    # ------------------------------------------------------------------
    # 券状态流水
    # ------------------------------------------------------------------
    op.create_table(
        "coupon_flow",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("coupon_code_id", sa.BigInteger(), nullable=False),
        sa.Column("from_status", sa.SmallInteger(), nullable=False),
        sa.Column("to_status", sa.SmallInteger(), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=True),
        sa.Column("use_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "biz_key", sa.String(length=64), nullable=False, comment="幂等键，主键冲突即已处理"
        ),
        sa.Column("operator", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coupon_flow")),
        sa.UniqueConstraint("biz_key", name=op.f("uk_coupon_flow_biz_key")),
        schema="promotion",
    )
    op.create_index(
        "idx_coupon_flow_code", "coupon_flow", ["coupon_code_id", "created_at"], schema="promotion"
    )
    op.create_index("idx_coupon_flow_order", "coupon_flow", ["order_no"], schema="promotion")

    # ------------------------------------------------------------------
    # 促销活动
    # ------------------------------------------------------------------
    op.create_table(
        "promo_activity",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "level", sa.SmallInteger(), nullable=False, comment="0单品 1店铺 2平台"
        ),
        sa.Column(
            "type",
            sa.String(length=32),
            nullable=False,
            comment="PROMO_ITEM / PROMO_ORDER_SHOP …",
        ),
        sa.Column(
            "calc_type", sa.SmallInteger(), nullable=False, comment="1直降 2折扣 3特价"
        ),
        sa.Column(
            "discount_value",
            sa.BigInteger(),
            nullable=False,
            comment="直降=减免额(分)；折扣=折扣率(8500=85折)；特价=定价(分)",
        ),
        sa.Column(
            "max_discount",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="封顶(分)，0=不限",
        ),
        sa.Column(
            "threshold",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="订单级门槛(分)",
        ),
        sa.Column(
            "shop_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="0=平台活动",
        ),
        sa.Column(
            "scope_type",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="同券的 scope_type",
        ),
        sa.Column("scope_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("start_at", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=False),
        sa.Column("end_at", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=False),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1未开始 2进行中 3已结束 4已作废",
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("end_at > start_at", name=op.f("ck_promo_activity_time_range")),
        sa.CheckConstraint(
            "discount_value >= 0", name=op.f("ck_promo_activity_value_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_activity")),
        schema="promotion",
        comment="促销活动",
    )
    op.create_index(
        "idx_promo_act_status_time",
        "promo_activity",
        ["status", "start_at", "end_at"],
        schema="promotion",
    )
    op.create_index(
        "idx_promo_act_level", "promo_activity", ["level", "status"], schema="promotion"
    )
    op.create_index(
        "idx_promo_act_shop", "promo_activity", ["shop_id", "status"], schema="promotion"
    )

    # ------------------------------------------------------------------
    # 叠加规则矩阵 + 灌入默认规则
    # ------------------------------------------------------------------
    op.create_table(
        "promo_stack_rule",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("rule_name", sa.String(length=64), nullable=False),
        sa.Column("type_a", sa.String(length=32), nullable=False, comment="优惠类型A"),
        sa.Column("type_b", sa.String(length=32), nullable=False, comment="优惠类型B"),
        sa.Column(
            "stackable", sa.Boolean(), nullable=False, comment="true可叠 false互斥"
        ),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "shop_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="0=全局规则",
        ),
        sa.Column("effective_from", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=True),
        sa.Column("effective_to", postgresql.TIMESTAMP(timezone=True, precision=3), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_stack_rule")),
        sa.UniqueConstraint("type_a", "type_b", "shop_id", name="uk_stack_rule_pair"),
        schema="promotion",
        comment="优惠叠加规则矩阵",
    )

    rules_table = sa.table(
        "promo_stack_rule",
        sa.column("id", sa.BigInteger),
        sa.column("rule_name", sa.String),
        sa.column("type_a", sa.String),
        sa.column("type_b", sa.String),
        sa.column("stackable", sa.Boolean),
        sa.column("priority", sa.Integer),
        sa.column("shop_id", sa.BigInteger),
        schema="promotion",
    )
    # 用固定 id 而不是雪花：它们是**引用数据**，同一条规则在所有环境应当同 id
    op.bulk_insert(
        rules_table,
        [
            {
                "id": 1000 + i,
                "rule_name": name,
                "type_a": a,
                "type_b": b,
                "stackable": stackable,
                "priority": priority,
                "shop_id": 0,
            }
            for i, (a, b, stackable, priority, name) in enumerate(DEFAULT_STACK_RULES)
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("promo_stack_rule", schema="promotion")
    op.drop_index("idx_promo_act_shop", table_name="promo_activity", schema="promotion")
    op.drop_index("idx_promo_act_level", table_name="promo_activity", schema="promotion")
    op.drop_index("idx_promo_act_status_time", table_name="promo_activity", schema="promotion")
    op.drop_table("promo_activity", schema="promotion")

    op.drop_index("idx_coupon_flow_order", table_name="coupon_flow", schema="promotion")
    op.drop_index("idx_coupon_flow_code", table_name="coupon_flow", schema="promotion")
    op.drop_table("coupon_flow", schema="promotion")

    op.drop_index(
        "idx_coupon_receive_ip_time", table_name="coupon_receive_log", schema="promotion"
    )
    op.drop_index(
        "idx_coupon_receive_tpl_user", table_name="coupon_receive_log", schema="promotion"
    )
    op.drop_table("coupon_receive_log", schema="promotion")

    op.drop_table("coupon_user_quota", schema="promotion")

    op.drop_index("idx_coupon_code_expire", table_name="coupon_code", schema="promotion")
    op.drop_index("idx_coupon_code_locked", table_name="coupon_code", schema="promotion")
    op.drop_index("idx_coupon_code_tpl_user", table_name="coupon_code", schema="promotion")
    op.drop_index("idx_coupon_code_user_status", table_name="coupon_code", schema="promotion")
    op.drop_table("coupon_code", schema="promotion")

    op.drop_index("idx_coupon_tpl_shop", table_name="coupon_template", schema="promotion")
    op.drop_index("idx_coupon_tpl_status_time", table_name="coupon_template", schema="promotion")
    op.drop_table("coupon_template", schema="promotion")
