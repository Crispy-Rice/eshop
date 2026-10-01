"""inventory 模块的 ORM 模型。

对应 docs/03-inventory.md 与 docs/13-schema.md §1。四张表，各有一条关键约束：

1. ``sku_stock`` 的 **CHECK 恒等式** ``total = available + locked + frozen``
   与 **非负约束**——任何代码 Bug 导致的负库存或等式破坏，都会让事务直接失败，
   而不是悄悄写进库里（docs/03 §5）。
2. ``sku_stock`` 上**不给** available/locked/frozen 建索引：被索引的列一旦更新
   就不能走 HOT，热点 SKU 的扣减会产生大量索引写入（docs/13 §9.2）。
3. ``stock_biz_key`` 与 ``stock_flow`` **必须分开**：PG 分区表的唯一约束必须包含
   分区键，把 biz_key 放在流水表上就失去了全局唯一性，防重复回补就失效了
   （docs/03 §8）。所以幂等键独立成一张小表。
4. ``warehouse`` 上用一个 partial unique index 保证**每店铺最多一个默认仓**。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# 仓库状态
WAREHOUSE_ENABLED = 1
WAREHOUSE_DISABLED = 2

# 流水变更类型（docs/03 §8）
CHANGE_LOCK = 1  # 预占（下单）
CHANGE_CONFIRM = 2  # 实扣（支付成功）
CHANGE_RELEASE = 3  # 回补（取消/超时/支付失败）
CHANGE_DELIVER = 4  # 发货扣减
CHANGE_RETURN_IN = 5  # 退货入库
CHANGE_ADJUST = 6  # 手工调整
CHANGE_INIT = 7  # 初始化
# 未发货退款：frozen → available。docs/08 §3.4 把它归在"回补"里，
# 但它与取消订单的回补（locked → available）动的是不同的格子，
# 流水里分开记才能一眼看出这批货当初有没有付过款
CHANGE_REFUND_BACK = 8  # 未发货退款回补
CHANGE_DEFECTIVE = 9  # 质检不合格入残次品池

CHANGE_TYPE_TEXT: dict[int, str] = {
    CHANGE_LOCK: "预占",
    CHANGE_CONFIRM: "实扣",
    CHANGE_RELEASE: "回补",
    CHANGE_DELIVER: "发货扣减",
    CHANGE_RETURN_IN: "退货入库",
    CHANGE_ADJUST: "手工调整",
    CHANGE_INIT: "初始化",
    CHANGE_REFUND_BACK: "退款回补",
    CHANGE_DEFECTIVE: "残次品入库",
}


class Warehouse(Base):
    """仓库。第一期每个店铺一个默认仓，由 ``ensure_default_warehouse`` 懒创建。"""

    __tablename__ = "warehouse"
    __table_args__ = (
        UniqueConstraint("shop_id", "name", name="uk_warehouse_shop_name"),
        # 每店铺最多一个默认仓。"最多一个"用 partial unique index 表达，
        # 非默认仓的 is_default 全是 false，不参与唯一性判断。
        Index("uk_warehouse_default", "shop_id", unique=True, postgresql_where=text("is_default")),
        {"schema": "inventory"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="跨 schema 不建外键")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    region_code: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("''"), comment="区划码，运费计算用"
    )
    is_default: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), comment="每店铺最多一个"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1启用 2停用"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class SkuStock(Base):
    """分仓库存。全站更新最频繁的表。

    四个数量的含义与流转见 docs/03 §3.1::

        available ──下单预占──> locked ──支付成功──> frozen ──发货──> 扣减 total
             ↑                      │
             └──── 超时取消/主动取消 ─┘
    """

    __tablename__ = "sku_stock"
    __table_args__ = (
        UniqueConstraint("sku_id", "warehouse_id", name="uk_sku_stock_sku_wh"),
        # 列表页按店铺 + 仓筛选
        Index("idx_sku_stock_shop_wh", "shop_id", "warehouse_id"),
        CheckConstraint(
            "available >= 0 AND locked >= 0 AND frozen >= 0 AND total >= 0",
            name="non_negative",
        ),
        # 残次品池：退货质检不合格的商品进这里。**不参与下面的恒等式** ——
        # 它既不是可售库存，也不该让"账面总量"看起来还在。运营后续决定翻新
        # （defective → available/total）还是销毁（defective 直接减）
        CheckConstraint("defective >= 0", name="defective_non_negative"),
        # 恒等式。发货时 total -= n 且 frozen -= n，等式保持
        CheckConstraint("total = available + locked + frozen", name="identity"),
        # 热点行：预留页内空间，让 UPDATE 尽量走 HOT
        {"schema": "inventory", "postgresql_with": {"fillfactor": 80}},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 冗余 shop_id：列表查询与鉴权都要它，不必每次回 product.sku
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    available: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="可售，能被新订单预占"
    )
    locked: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="已下单未支付"
    )
    frozen: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="已支付待发货"
    )
    defective: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="残次品，不计入 total 恒等式也不可售（退货质检不合格时增加）",
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="每次变更 +1"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class StockBizKey(Base):
    """库存变更的幂等键。

    ★ 这是防重复回补的**终极武器**：每次库存变更先在这个表里
    ``INSERT ... ON CONFLICT DO NOTHING``，``rowcount == 0`` 说明已处理过，
    直接返回成功、不再碰库存。即使上游所有幂等判断都失效，
    这里的主键也会拦住第二次写入。

    与 ``stock_flow`` 分开的理由见模块 docstring。
    """

    __tablename__ = "stock_biz_key"
    __table_args__ = (
        # 按 created_at 清理 90 天前的记录
        Index("idx_stock_biz_key_created", "created_at"),
        {"schema": "inventory"},
    )

    biz_key: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="形如 LOCK:{orderSubNo}:{skuId}"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class StockFlow(Base):
    """库存流水。只追加，按月分区。

    这是事后排查超卖的**唯一可靠依据**（docs/03 §4），所以每笔变更都要落一条。

    分区表的两条约束：
    - 主键必须包含分区键 ``created_at``；
    - 未来月份的分区由 cron 提前创建，见 tasks.py。
    """

    __tablename__ = "stock_flow"
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at", name="pk_stock_flow"),
        Index("idx_stock_flow_sku_time", "sku_id", "created_at"),
        Index("idx_stock_flow_order", "order_no"),
        {"schema": "inventory", "postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_no: Mapped[str | None] = mapped_column(String(32), comment="关联单据号，手工调整时为空")
    change_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="见 CHANGE_TYPE_TEXT"
    )
    num: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="正数增加、负数减少；0 无意义"
    )
    before_qty: Mapped[int] = mapped_column(Integer, nullable=False, comment="变更前可售量")
    after_qty: Mapped[int] = mapped_column(Integer, nullable=False, comment="变更后可售量")
    biz_key: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str | None] = mapped_column(String(64), comment="操作人/系统")
    remark: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
