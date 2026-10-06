"""multi warehouse routing

给"商家自建多仓 + 按收货区划路由发货仓"补上数据落点。

1. ``inventory.warehouse`` 加**地址与联系人** —— 商家要能自建多个仓，仓得有地址。
   ``region_code`` 本来就存在（列注释写着"运费计算用"），但**从来没有任何代码读它**；
   这一轮它才真正开始起作用（路由匹配它的前缀）。

2. 新表 ``inventory.warehouse_region_rule`` —— "这个仓发往哪些区划"。
   照 ``freight_region_rule`` 的范式（前缀匹配、层级由码长推导）。

   ★ 唯一键是 ``(shop_id, region_code)`` 而不是 ``(warehouse_id, region_code)``：
     一个区划码只能指向一个仓，于是**整张表不需要优先级**。具体性由**码长**决定 ——
     "4403 深圳 → 深圳仓" 比 "44 广东 → 广州仓" 更具体，地址码 ``440305`` 两条规则
     都命中，取码更长的那个。商家的心智也因此简单：**一个地方只由一个仓发货**。

3. ``trade.order_sub`` 加 ``warehouse_id`` —— **把"这批货从哪发"记下来**。

   在此之前订单上没有任何仓信息：``delivery_order.warehouse_id`` 有列但硬编码 0，
   而售后回补是**重新路由一遍**（``aftersale`` 调 ``inventory.batch_sku_warehouses``）。
   一期单仓时这没暴露，多仓下"退货入哪个仓"会随规则变化而变，必然错账。
   历史行回填成该店默认仓 —— 这一轮之前每店只有默认仓，那正是当时用的仓。

★ 整套设计的地基：**路由不看库存，只看（店铺, 收货区划）**。算价时的运费是**按仓
  计算**的（``promotion.checkout`` 把 ``warehouses`` 交给 freight 拆包裹），路由若随
  库存波动，算价选 A、下单选 B，运费就变了 —— 会**误触发**价格一致性校验。代价是
  "路由到的仓里某商品没货就整单失败"（用可操作的报错补偿，见 docs/03）。

Revision ID: c1e8f4a70b93
Revises: b8e1c4d7a902
Create Date: 2026-10-06 21:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1e8f4a70b93"
down_revision: Union[str, Sequence[str], None] = "b8e1c4d7a902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ------------------------------------------------------------------
    # 1. 仓库的地址与联系人。字段命名与 account.user_address 对齐
    # ------------------------------------------------------------------
    op.add_column(
        "warehouse",
        sa.Column(
            "province", sa.String(length=32), server_default=sa.text("''"), nullable=False,
            comment="省/直辖市，展示用（路由只认 region_code）",
        ),
        schema="inventory",
    )
    op.add_column(
        "warehouse",
        sa.Column(
            "city", sa.String(length=32), server_default=sa.text("''"), nullable=False,
            comment="市",
        ),
        schema="inventory",
    )
    op.add_column(
        "warehouse",
        sa.Column(
            "district", sa.String(length=32), server_default=sa.text("''"), nullable=False,
            comment="区/县",
        ),
        schema="inventory",
    )
    op.add_column(
        "warehouse",
        sa.Column(
            "detail", sa.String(length=255), server_default=sa.text("''"), nullable=False,
            comment="详细地址（街道门牌）",
        ),
        schema="inventory",
    )
    op.add_column(
        "warehouse",
        sa.Column(
            "contact_name", sa.String(length=64), server_default=sa.text("''"), nullable=False,
            comment="联系人",
        ),
        schema="inventory",
    )
    op.add_column(
        "warehouse",
        sa.Column(
            "contact_phone", sa.String(length=20), server_default=sa.text("''"), nullable=False,
            comment="联系电话",
        ),
        schema="inventory",
    )

    # ------------------------------------------------------------------
    # 2. 仓 → 覆盖区划
    # ------------------------------------------------------------------
    op.create_table(
        "warehouse_region_rule",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "shop_id", sa.BigInteger(), nullable=False, comment="冗余：按店批量查规则必需"
        ),
        sa.Column("warehouse_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "region_code",
            sa.String(length=16),
            nullable=False,
            comment='前缀匹配；"0" = 该仓兜底覆盖全国',
        ),
        sa.Column(
            "region_level",
            sa.SmallInteger(),
            nullable=False,
            comment="1省 2市 3区。由码长推导，**匹配时不参与**（只作展示）",
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["inventory.warehouse.id"],
            name=op.f("fk_warehouse_region_rule_warehouse_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_warehouse_region_rule")),
        # ★ 一个区划码只落一个仓 —— 于是不需要优先级列
        sa.UniqueConstraint("shop_id", "region_code", name="uk_warehouse_rule_shop_region"),
        schema="inventory",
        comment="仓库的发货覆盖区划（一个区划只由一个仓发货）",
    )
    op.create_index(
        "idx_warehouse_rule_shop", "warehouse_region_rule", ["shop_id"], schema="inventory"
    )

    # ------------------------------------------------------------------
    # 3. 订单子单记发货仓
    # ------------------------------------------------------------------
    op.add_column(
        "order_sub",
        sa.Column(
            "warehouse_id",
            sa.BigInteger(),
            nullable=True,
            comment="下单时路由到的发货仓。**发货与售后都读它**，不再重新路由",
        ),
        schema="trade",
    )
    # 回填：这一轮之前每店只有默认仓，那正是当时用的仓。
    # 保持可空（而非 SET NOT NULL）：万一有回填不到的孤儿行，代码里回退到默认仓即可，
    # 而 SET NOT NULL 会让迁移在真实数据上直接卡住。
    op.execute(
        """
        UPDATE trade.order_sub AS s
        SET warehouse_id = w.id
        FROM inventory.warehouse AS w
        WHERE w.shop_id = s.shop_id
          AND w.is_default
          AND s.warehouse_id IS NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("order_sub", "warehouse_id", schema="trade")

    op.drop_index("idx_warehouse_rule_shop", table_name="warehouse_region_rule", schema="inventory")
    op.drop_table("warehouse_region_rule", schema="inventory")

    for column in ("contact_phone", "contact_name", "detail", "district", "city", "province"):
        op.drop_column("warehouse", column, schema="inventory")
