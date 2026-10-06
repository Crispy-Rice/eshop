"""fix sub platform allocation

修 ``trade.order_sub`` 的**平台级优惠重分摊**，把历史订单的子单金额算正。

背景：拆单时 ``item_discount`` / ``shop_discount`` / ``coupon_amount`` 三个字段都是
"该店铺各行分摊额之和"，唯独 ``platform_discount`` 又按「子单原价占比」把同一个
平台优惠**重新摊了一遍**。引擎是按「参与金额」摊到行上的，两个基数不同，结果会差
几分：

    子单1  4500 - 500 - 27 + 7 = 3980    ← platform_discount=27（重分摊）
    该子单的行  3976（= 4500 - 500 - 24）  ← 行上摊到的是 24

于是 ``Σ行实付 + 子单运费``（3976 + 7 = 3983）**大于** ``子单应付``（3980），
而售后正是拿子单应付当退款上限（docs/08 §10）—— 这笔单**连整单退都会被拦住**，
报「退款金额超限：已退 0 分，本次 3983 分，子单应付 3980 分」。

代码已改成四个字段一律聚合行分摊额（``trade.service._build_subs``）。这个迁移
把存量订单按**同一条不变式**重算：子单应付 = 该子单各行实付之和 + 子单运费。

★ 它是**守恒的**：Σ行实付 = 母单应付 - 母单运费，所以按子单重算后
  ``Σ子单应付`` 仍然精确等于 ``order_main.payable_amount``，用户付过的钱不变。

★ 只改真的算不平的行：单店铺订单（``platform_discount`` 原本就等于各行之和）
  算出来的值与原值相同，过滤掉不写，避免把没坏的数据也动一遍。

Revision ID: b3f7c2d9a1e4
Revises: a7c4e81b3f92
Create Date: 2026-10-05 21:40:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f7c2d9a1e4"
down_revision: Union[str, Sequence[str], None] = "a7c4e81b3f92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        WITH fixed AS (
          SELECT s.order_sub_no,
                 COALESCE(i.item_payable, 0) + s.freight_amount AS new_payable,
                 s.total_amount - s.item_discount - s.shop_discount
                   - s.point_deduction + s.freight_amount
                   - (COALESCE(i.item_payable, 0) + s.freight_amount) AS new_platform
            FROM trade.order_sub s
            LEFT JOIN (
              SELECT order_sub_no, SUM(payable_amount) AS item_payable
                FROM trade.order_item
               GROUP BY order_sub_no
            ) i ON i.order_sub_no = s.order_sub_no
        )
        UPDATE trade.order_sub s
           SET payable_amount = f.new_payable,
               platform_discount = f.new_platform
          FROM fixed f
         WHERE s.order_sub_no = f.order_sub_no
           AND (s.payable_amount <> f.new_payable
                OR s.platform_discount <> f.new_platform)
        """
    )


def downgrade() -> None:
    """不可逆。

    旧值是一份"算错的分摊"，没有信息量可恢复（重分摊的输入只有子单原价，
    它还在，但把错值写回去没有任何意义）。这里显式留空而不是假装能回滚。
    """
