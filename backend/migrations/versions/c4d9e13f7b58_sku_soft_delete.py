"""sku soft delete

给 ``product.sku`` 加软删标记，并把商家编码的唯一约束改成部分唯一索引。

背景：要支持"商品没有订单时整体替换规格与 SKU"。替换掉的旧 SKU **不物理删除** ——
``inventory.sku_stock`` 的行还挂着它们，而 ``inventory/repository.py`` 的 ``list_flows``
用 ``INNER JOIN sku_stock`` 取 shop_id，删掉库存行会让那段库存流水从商家列表里
消失（数据还在库里，但人看不见）。软删既保住流水，又能复用各模块已有的
"排掉已删 SKU" 读侧过滤（``product.repository.list_deleted_sku_ids``）。

★ 唯一约束改成 **partial unique index**（``WHERE NOT deleted``）：软删掉的 SKU 不该
  再占用商家编码，否则换了规格之后想复用同一个编码会被一条已退役的行挡住。
  先例：``inventory/models.py`` 的 ``uk_warehouse_default``。

★ 注意：软删必须**同时**把 ``status`` 置为 2（下架）。购物车与下单路径是按 status
  过滤的，不看 ``deleted`` —— 只加这一列的话，被替换掉的 SKU 仍能加购、仍能下单。

Revision ID: c4d9e13f7b58
Revises: b8e1d47f3a26
Create Date: 2026-10-05 17:20:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d9e13f7b58"
down_revision: Union[str, Sequence[str], None] = "b8e1d47f3a26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sku",
        sa.Column(
            "deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="软删标记",
        ),
        schema="product",
    )
    op.drop_constraint("uk_sku_spu_code", "sku", schema="product", type_="unique")
    op.create_index(
        "uk_sku_spu_code",
        "sku",
        ["spu_id", "sku_code"],
        unique=True,
        schema="product",
        postgresql_where=sa.text("NOT deleted"),
    )


def downgrade() -> None:
    """回退时把软删的 SKU 真正删掉。

    ★ 旧版本没有"软删"这个概念，留着这些行只会让它们**重新变成可售的 SKU**
      （它们的 status 是 2，但旧代码随后可能被上架操作翻回 1）。所以回退时
      物理删除才是诚实的选择 —— 先清掉拦路的外键 ``sku_spec``。
    """
    op.execute(
        "DELETE FROM product.sku_spec WHERE sku_id IN (SELECT id FROM product.sku WHERE deleted)"
    )
    op.execute("DELETE FROM product.sku WHERE deleted")
    op.drop_index("uk_sku_spu_code", table_name="sku", schema="product")
    op.create_unique_constraint("uk_sku_spu_code", "sku", ["spu_id", "sku_code"], schema="product")
    op.drop_column("sku", "deleted", schema="product")
