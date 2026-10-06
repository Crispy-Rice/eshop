"""freight default template

给 ``freight.freight_template`` 加 ``is_default``，并保证**同一店铺最多一条默认**。

背景：SKU 与运费模板是逐条绑定的（``freight.sku_freight_bind``），所以新发布的
商品、别的店铺的商品天然都在模板之外。没绑定的 SKU 在算运费时是**直接报错**，
而且报错发生在**买家点结算**那一刻 —— 商家那边毫无感知：

    「联想拯救者 Y9000P 2023」还没有绑定运费模板，无法计算运费

设计文档其实写过怎么收场（docs/06 §404）："绑定关系失效时回退到'店铺默认模板'；
没有默认模板则拒绝"。实现只做了后半句。这次把前半句补上。

★ 唯一性用 **partial unique index** 表达，而不是给 ``(shop_id, is_default)`` 加普通
  唯一约束 —— 后者会把"每条模板的 is_default=false"也算成重复，一个店只能有一条
  非默认模板。先例：``inventory/models.py`` 的 ``uk_warehouse_default``、
  ``product.sku`` 的 ``uk_sku_spu_code``。

回填：给**每个已经有模板的店铺**挑一条设为默认（优先启用中的，其次 id 最小），
这样存量店铺立刻能受益，而不是要等有人手动去点一下。

Revision ID: a7c4e81b3f92
Revises: c4d9e13f7b58
Create Date: 2026-10-05 20:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c4e81b3f92"
down_revision: Union[str, Sequence[str], None] = "c4d9e13f7b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "freight_template",
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="店铺默认模板：未绑定模板的 SKU 回落到它",
        ),
        schema="freight",
    )

    # 回填：每店一条。status <> 1 排在后面（优先启用中的），同状态取 id 最小
    op.execute(
        """
        UPDATE freight.freight_template t
           SET is_default = true
         WHERE t.id = (
               SELECT x.id
                 FROM freight.freight_template x
                WHERE x.shop_id = t.shop_id
                ORDER BY (x.status <> 1), x.id
                LIMIT 1
         )
        """
    )

    # 索引建在回填**之后**：万一回填写出多条，这里会直接失败而不是留下脏数据
    op.create_index(
        "uk_freight_tpl_default",
        "freight_template",
        ["shop_id"],
        unique=True,
        schema="freight",
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index("uk_freight_tpl_default", table_name="freight_template", schema="freight")
    op.drop_column("freight_template", "is_default", schema="freight")
