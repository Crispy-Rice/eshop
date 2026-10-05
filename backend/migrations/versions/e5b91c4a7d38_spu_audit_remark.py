"""spu audit remark

给 product.spu 加审核意见列，并补一个平台审核队列要用的索引。

背景：``POST /api/admin/spus/{id}/audit`` 一直收 ``remark`` 字段，但**没有任何地方存它** ——
驳回时写的理由被直接丢掉，商家只能看到商品回到"草稿"，不知道为什么。
这次把它落库，商家在自己的商品详情里就能看到。

Revision ID: e5b91c4a7d38
Revises: d4a8f13b7c52
Create Date: 2026-10-04 20:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b91c4a7d38"
down_revision: Union[str, Sequence[str], None] = "d4a8f13b7c52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "spu",
        sa.Column(
            "audit_remark",
            sa.String(length=255),
            nullable=True,
            comment="最近一次审核意见（驳回理由）；通过时清空",
        ),
        schema="product",
    )
    # 平台审核队列查的是「跨店铺、按状态、按时间倒序」：
    #   WHERE status = 5 ORDER BY created_at DESC
    # 现有的 idx_spu_shop_status 前导列是 shop_id，帮不上这个查询。
    op.create_index(
        "idx_spu_status_created",
        "spu",
        ["status", "created_at"],
        schema="product",
        postgresql_where=sa.text("NOT deleted"),
    )


def downgrade() -> None:
    op.drop_index("idx_spu_status_created", table_name="spu", schema="product")
    op.drop_column("spu", "audit_remark", schema="product")
