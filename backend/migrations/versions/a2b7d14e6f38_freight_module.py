"""freight module

Revision ID: a2b7d14e6f38
Revises: f1a8c53d7e26
Create Date: 2026-10-01 18:10:00.000000

运费模板与规则，见 docs/06-freight.md §3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a2b7d14e6f38'
down_revision: Union[str, Sequence[str], None] = 'f1a8c53d7e26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ------------------------------------------------------------------
    # 运费模板
    # ------------------------------------------------------------------
    op.create_table(
        "freight_template",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "name", sa.String(length=64), nullable=False, comment='如"默认快递模板"'
        ),
        sa.Column(
            "charge_type",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1按重量 2按件数 3按体积",
        ),
        sa.Column(
            "first_unit",
            sa.Integer(),
            server_default=sa.text("1000"),
            nullable=False,
            comment="首重（克）",
        ),
        sa.Column("first_price", sa.BigInteger(), nullable=False, comment="首重价（分）"),
        sa.Column(
            "add_unit",
            sa.Integer(),
            server_default=sa.text("1000"),
            nullable=False,
            comment="续重单位（克）",
        ),
        sa.Column("add_price", sa.BigInteger(), nullable=False, comment="续重价（分/单位）"),
        sa.Column(
            "free_shipping",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="全场包邮",
        ),
        sa.Column(
            "free_threshold",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="满额包邮（分），0=不参与",
        ),
        sa.Column(
            "free_num",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="满件包邮，0=不参与",
        ),
        sa.Column(
            "merge_type",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="同仓多 SKU 的合并方式",
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1启用 2停用",
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
        sa.CheckConstraint("first_price >= 0", name=op.f("ck_freight_template_first_price_non_negative")),
        sa.CheckConstraint("add_price >= 0", name=op.f("ck_freight_template_add_price_non_negative")),
        sa.CheckConstraint("add_unit > 0", name=op.f("ck_freight_template_add_unit_positive")),
        sa.CheckConstraint("first_unit >= 0", name=op.f("ck_freight_template_first_unit_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_freight_template")),
        schema="freight",
        comment="运费模板",
    )
    op.create_index(
        "idx_freight_tpl_shop", "freight_template", ["shop_id", "status"], schema="freight"
    )

    # ------------------------------------------------------------------
    # 区域规则
    # ------------------------------------------------------------------
    op.create_table(
        "freight_region_rule",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "region_code",
            sa.String(length=16),
            nullable=False,
            comment='"0"=全国默认，"999999"=偏远',
        ),
        sa.Column(
            "region_level", sa.SmallInteger(), nullable=False, comment="1省 2市 3区"
        ),
        sa.Column("first_unit", sa.Integer(), nullable=False),
        sa.Column("first_price", sa.BigInteger(), nullable=False),
        sa.Column("add_unit", sa.Integer(), nullable=False),
        sa.Column("add_price", sa.BigInteger(), nullable=False),
        sa.Column(
            "free_shipping", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="越大越优先（市 > 省 > 全国）",
        ),
        sa.CheckConstraint("add_unit > 0", name=op.f("ck_freight_region_rule_add_unit_positive")),
        sa.CheckConstraint(
            "first_price >= 0 AND add_price >= 0",
            name=op.f("ck_freight_region_rule_prices_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["freight.freight_template.id"],
            name=op.f("fk_freight_region_rule_template_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_freight_region_rule")),
        sa.UniqueConstraint("template_id", "region_code", name="uk_freight_rule_tpl_region"),
        schema="freight",
        comment="模板-区域运费规则",
    )

    # ------------------------------------------------------------------
    # SKU 绑定
    # ------------------------------------------------------------------
    op.create_table(
        "sku_freight_bind",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("sku_id", sa.BigInteger(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "warehouse_id", sa.BigInteger(), nullable=False, comment="该绑定的适用仓库"
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="同 SKU 多模板时越大越优先",
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["freight.freight_template.id"],
            name=op.f("fk_sku_freight_bind_template_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sku_freight_bind")),
        sa.UniqueConstraint("sku_id", "template_id", "warehouse_id", name="uk_sku_freight_bind"),
        schema="freight",
        comment="SKU 与运费模板绑定（多对多）",
    )
    # 只索引启用的绑定，按优先级倒序取第一条
    op.create_index(
        "idx_sku_freight_active",
        "sku_freight_bind",
        ["sku_id", "warehouse_id", sa.text("priority DESC")],
        schema="freight",
        postgresql_where=sa.text("enabled"),
    )

    # ------------------------------------------------------------------
    # 不发货区域
    # ------------------------------------------------------------------
    op.create_table(
        "freight_exclude_region",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column("region_code", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True, comment='如"暂不配送"'),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["freight.freight_template.id"],
            name=op.f("fk_freight_exclude_region_template_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_freight_exclude_region")),
        sa.UniqueConstraint("template_id", "region_code", name="uk_freight_exclude_tpl_region"),
        schema="freight",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("freight_exclude_region", schema="freight")
    op.drop_index("idx_sku_freight_active", table_name="sku_freight_bind", schema="freight")
    op.drop_table("sku_freight_bind", schema="freight")
    op.drop_table("freight_region_rule", schema="freight")
    op.drop_index("idx_freight_tpl_shop", table_name="freight_template", schema="freight")
    op.drop_table("freight_template", schema="freight")
