"""sku code optional

商家编码（``product.sku.sku_code``）改成**选填**。

背景：这个字段是给商家对接自己的 ERP / 打发货单用的钥匙，平台内部一律走雪花 ID ——
全仓库没有任何查询按它过滤（repository 层 grep 零命中）。既然如此，就不该逼着
没有自建系统的商家在发布时给每一行 SKU 编一个号。

★ 空值一律存 **NULL** 而不是空串：唯一约束 ``uk_sku_spu_code (spu_id, sku_code)``
  在 PostgreSQL 里把每个 NULL 视为互不相同，所以一个商品下可以有很多个没编码的
  SKU；换成空串，第二个就会撞约束。写入侧（``SkuIn`` 的 field_validator）已经把
  空串归一成 None，这里只负责放开 NOT NULL。

存量数据不用动：迁移前扫过，编码列没有空串（0 行）。

Revision ID: b8e1d47f3a26
Revises: a2f7c40de915
Create Date: 2026-10-05 16:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e1d47f3a26"
down_revision: Union[str, Sequence[str], None] = "a2f7c40de915"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "sku",
        "sku_code",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        nullable=True,
        comment="商家编码（选填），同一 SPU 内唯一",
        schema="product",
    )


def downgrade() -> None:
    """回退时把没编码的行补成 ``SKU-<id>``。

    ★ 旧版本要求 NOT NULL，而"没有编码"这件事在旧模型里根本无法表达 ——
      要么回退失败，要么给这些行造个值。这里选后者，用 ``SKU-<sku_id>``：
      它显然是人造的、且天然唯一（id 是主键），不会被误当成商家真实填的货号。
    """
    op.execute("UPDATE product.sku SET sku_code = 'SKU-' || id WHERE sku_code IS NULL")
    op.alter_column(
        "sku",
        "sku_code",
        existing_type=sa.String(length=64),
        existing_nullable=True,
        nullable=False,
        comment="商家编码",
        schema="product",
    )
