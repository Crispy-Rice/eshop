"""cart module

Revision ID: e7c4a81f2b93
Revises: d5b9e2f4c810
Create Date: 2026-10-01 16:40:00.000000

购物车表，见 docs/02-domain-model.md §4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e7c4a81f2b93'
down_revision: Union[str, Sequence[str], None] = 'd5b9e2f4c810'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "cart_item",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "shop_id", sa.BigInteger(), nullable=False, comment="冗余，便于按店铺分组"
        ),
        sa.Column("sku_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "spu_id", sa.BigInteger(), nullable=False, comment="冗余，便于批量查 SPU"
        ),
        sa.Column(
            "price_snapshot",
            sa.BigInteger(),
            nullable=False,
            comment="加购时价格（分）。只用于降价提醒，**不参与结算**",
        ),
        sa.Column("num", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "selected",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="是否勾选，结算只算勾选的",
        ),
        sa.Column(
            "source",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1详情页 2列表页 3活动页",
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
        # 同一用户 + 同一 SKU 只能有一行；加购是累加 num
        sa.CheckConstraint("num BETWEEN 1 AND 200", name=op.f("ck_cart_item_num_range")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cart_item")),
        sa.UniqueConstraint("user_id", "sku_id", name="uk_cart_user_sku"),
        schema="cart",
    )
    op.create_index(
        "idx_cart_user_shop", "cart_item", ["user_id", "shop_id"], schema="cart"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_cart_user_shop", table_name="cart_item", schema="cart")
    op.drop_table("cart_item", schema="cart")
