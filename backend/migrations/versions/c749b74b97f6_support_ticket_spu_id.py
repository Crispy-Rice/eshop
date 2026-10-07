"""support ticket spu_id

给 ``support.ticket`` 加**商品上下文**：商品页点「联系客服」时带上 spu id。

★ 已有的三个上下文（``order_main_no`` / ``order_sub_no`` / ``refund_no``）都是**单号**，
  而商品是第四个、也是唯一一个用 **id** 的：店小蜜的 ``ticket_product`` 工具要拿它
  去查**当前**的价格/在售状态（标题是快照会过期，id 永远指向现在那份数据）。

★ 与那三个一样：**只用于展示与工具取数，不参与鉴权** —— 归属永远来自会话自己的
  ``user_id`` / ``shop_id``。

Revision ID: c749b74b97f6
Revises: 7486e2ecb95f
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c749b74b97f6"
down_revision: str | Sequence[str] | None = "7486e2ecb95f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ticket",
        sa.Column(
            "spu_id",
            sa.BigInteger(),
            nullable=True,
            comment="会话的商品上下文（商品页点进来时带上）",
        ),
        schema="support",
    )


def downgrade() -> None:
    op.drop_column("ticket", "spu_id", schema="support")
