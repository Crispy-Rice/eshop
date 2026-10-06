"""backfill total_sold

给 ``product.spu.total_sold``（列表与详情上的「已售」）补上历史值。

背景：这个字段**一直只有读、没有写** —— 文档说要"支付成功后累加"（docs/02 §336），
但那条链路指的是 outbox → Redis Stream → 消费者，而投递循环与消费者都没实现，
于是它永远是建表时的默认值 0。买家付完款，商品卡片的「已售」纹丝不动。

代码已改成在支付成功的**同一个事务**里累加（``trade.mark_paid`` →
``product.apply_sold_delta``）。这个迁移把存量订单按**同一条规则**算出来：

    total_sold = Σ 已付款订单（含之后各状态）里该商品的件数

★ 口径是「**累计**销量」——退款**不扣减**（``models`` 里那列的注释、docs/02 §47）。
  所以已退款（``status = 70``）的单子照样算进去。要"净销量"得另立字段。

★ 重算而不是累加，所以**可以重复执行**；且只改值真的变了的行。

★ 只按订单算，不看 Redis/outbox —— 这里是历史数据的一次性对齐，
  之后的正字由业务代码保证，跑偏了对不上就是 bug，不该靠这个校正。

Revision ID: c5e1a7d3b920
Revises: b3f7c2d9a1e4
Create Date: 2026-10-05 22:10:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e1a7d3b920"
down_revision: Union[str, Sequence[str], None] = "b3f7c2d9a1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 状态码：20 待发货 / 30 待收货 / 40 已完成 / 60 退款中 / 70 已退款 ——
    # 已付款及之后都算成交；10 待付款、50 已关闭不算。
    op.execute(
        """
        UPDATE product.spu p
           SET total_sold = x.sold, updated_at = now()
          FROM (
            SELECT i.spu_id, SUM(i.num) AS sold
              FROM trade.order_item i
              JOIN trade.order_sub s ON s.order_sub_no = i.order_sub_no
             WHERE s.status IN (20, 30, 40, 60, 70)
             GROUP BY i.spu_id
          ) x
         WHERE p.id = x.spu_id
           AND p.total_sold <> x.sold
        """
    )


def downgrade() -> None:
    """不可逆。原始值全是建表默认的 0，没有信息可恢复。"""
