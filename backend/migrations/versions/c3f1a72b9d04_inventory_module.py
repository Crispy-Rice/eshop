"""inventory module

Revision ID: c3f1a72b9d04
Revises: b7dc4e986eee
Create Date: 2026-10-01 15:40:00.000000

手写而非 autogenerate：``stock_flow`` 是**分区表**，
autogenerate 只能生成到 ``PARTITION BY`` 那一步，分区的 ``CREATE TABLE ... PARTITION OF``
它不会产生，而且分区是按月的、需要一次性建好未来几个月。

分区策略：迁移时建「当月 + 未来 3 个月」，之后的月份由
``app/modules/inventory/tasks.py`` 的 ``ensure_flow_partition`` 在每月 25 日补建。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c3f1a72b9d04'
down_revision: Union[str, Sequence[str], None] = 'b7dc4e986eee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 迁移时预建的分区月份（当月 + 未来 3 个月）
FLOW_PARTITIONS: list[tuple[str, str, str]] = [
    ("202610", "2026-10-01", "2026-11-01"),
    ("202611", "2026-11-01", "2026-12-01"),
    ("202612", "2026-12-01", "2027-01-01"),
    ("202701", "2027-01-01", "2027-02-01"),
]


def upgrade() -> None:
    """Upgrade schema."""
    # ------------------------------------------------------------------
    # 仓库
    # ------------------------------------------------------------------
    op.create_table(
        "warehouse",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False, comment="跨 schema 不建外键"),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "region_code",
            sa.String(length=16),
            server_default=sa.text("''"),
            nullable=False,
            comment="区划码，运费计算用",
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="每店铺最多一个",
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_warehouse")),
        sa.UniqueConstraint("shop_id", "name", name="uk_warehouse_shop_name"),
        schema="inventory",
    )
    # 「每店铺最多一个默认仓」用 partial unique index 表达：
    # 非默认仓的 is_default 全是 false，不参与唯一性判断
    op.create_index(
        "uk_warehouse_default",
        "warehouse",
        ["shop_id"],
        unique=True,
        schema="inventory",
        postgresql_where=sa.text("is_default"),
    )

    # ------------------------------------------------------------------
    # 分仓库存（热点行）
    # ------------------------------------------------------------------
    op.create_table(
        "sku_stock",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("sku_id", sa.BigInteger(), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("warehouse_id", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="可售，能被新订单预占",
        ),
        sa.Column(
            "locked",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已下单未支付",
        ),
        sa.Column(
            "frozen",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已支付待发货",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="每次变更 +1",
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
        sa.CheckConstraint(
            "available >= 0 AND locked >= 0 AND frozen >= 0 AND total >= 0",
            name=op.f("ck_sku_stock_non_negative"),
        ),
        # 恒等式：任何代码 Bug 破坏它都会让事务直接失败，而不是悄悄写进库
        sa.CheckConstraint(
            "total = available + locked + frozen",
            name=op.f("ck_sku_stock_identity"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sku_stock")),
        sa.UniqueConstraint("sku_id", "warehouse_id", name="uk_sku_stock_sku_wh"),
        schema="inventory",
        # 同一行被频繁 UPDATE：预留页内空间让更新尽量走 HOT（docs/13 §9.2）
        postgresql_with={"fillfactor": 80},
    )
    # 注意：**不要**给 available/locked/frozen 建索引，
    # 被索引的列一旦更新就不能走 HOT，热点扣减会产生大量索引写入
    op.create_index(
        "idx_sku_stock_shop_wh", "sku_stock", ["shop_id", "warehouse_id"], schema="inventory"
    )

    # ------------------------------------------------------------------
    # 幂等键（与流水表分开，理由见 models.py 模块 docstring）
    # ------------------------------------------------------------------
    op.create_table(
        "stock_biz_key",
        sa.Column(
            "biz_key",
            sa.String(length=64),
            nullable=False,
            comment="形如 LOCK:{orderSubNo}:{skuId}",
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("biz_key", name=op.f("pk_stock_biz_key")),
        schema="inventory",
    )
    op.create_index(
        "idx_stock_biz_key_created", "stock_biz_key", ["created_at"], schema="inventory"
    )

    # ------------------------------------------------------------------
    # 库存流水（按月分区，只追加）
    # ------------------------------------------------------------------
    op.create_table(
        "stock_flow",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("sku_id", sa.BigInteger(), nullable=False),
        sa.Column("warehouse_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "order_no",
            sa.String(length=32),
            nullable=True,
            comment="关联单据号，手工调整时为空",
        ),
        sa.Column("change_type", sa.SmallInteger(), nullable=False, comment="见 CHANGE_TYPE_TEXT"),
        sa.Column(
            "num",
            sa.Integer(),
            nullable=False,
            comment="正数增加、负数减少；0 无意义",
        ),
        sa.Column("before_qty", sa.Integer(), nullable=False, comment="变更前可售量"),
        sa.Column("after_qty", sa.Integer(), nullable=False, comment="变更后可售量"),
        sa.Column("biz_key", sa.String(length=64), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=True, comment="操作人/系统"),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 分区表的主键必须包含分区键
        sa.PrimaryKeyConstraint("id", "created_at", name="pk_stock_flow"),
        schema="inventory",
        postgresql_partition_by="RANGE (created_at)",
    )
    op.create_index(
        "idx_stock_flow_sku_time",
        "stock_flow",
        ["sku_id", "created_at"],
        schema="inventory",
    )
    op.create_index("idx_stock_flow_order", "stock_flow", ["order_no"], schema="inventory")

    for suffix, start, end in FLOW_PARTITIONS:
        op.execute(
            f"CREATE TABLE inventory.stock_flow_{suffix} "
            f"PARTITION OF inventory.stock_flow "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )


def downgrade() -> None:
    """Downgrade schema."""
    # 分区随父表一起删除，不用单独 drop
    op.drop_index("idx_stock_flow_order", table_name="stock_flow", schema="inventory")
    op.drop_index("idx_stock_flow_sku_time", table_name="stock_flow", schema="inventory")
    op.drop_table("stock_flow", schema="inventory")

    op.drop_index("idx_stock_biz_key_created", table_name="stock_biz_key", schema="inventory")
    op.drop_table("stock_biz_key", schema="inventory")

    op.drop_index("idx_sku_stock_shop_wh", table_name="sku_stock", schema="inventory")
    op.drop_table("sku_stock", schema="inventory")

    op.drop_index(
        "uk_warehouse_default",
        table_name="warehouse",
        schema="inventory",
        postgresql_where=sa.text("is_default"),
    )
    op.drop_table("warehouse", schema="inventory")
