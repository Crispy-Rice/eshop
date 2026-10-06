"""inventory 模块的 ORM 模型。

对应 docs/03-inventory.md 与 docs/13-schema.md §1。五张表，各有一条关键约束：

1. ``sku_stock`` 的 **CHECK 恒等式** ``total = available + locked + frozen``
   与 **非负约束**——任何代码 Bug 导致的负库存或等式破坏，都会让事务直接失败，
   而不是悄悄写进库里（docs/03 §5）。
2. ``sku_stock`` 上**不给** available/locked/frozen 建索引：被索引的列一旦更新
   就不能走 HOT，热点 SKU 的扣减会产生大量索引写入（docs/13 §9.2）。
3. ``stock_biz_key`` 与 ``stock_flow`` **必须分开**：PG 分区表的唯一约束必须包含
   分区键，把 biz_key 放在流水表上就失去了全局唯一性，防重复回补就失效了
   （docs/03 §8）。所以幂等键独立成一张小表。
4. ``warehouse`` 上用一个 partial unique index 保证**每店铺最多一个默认仓**。
5. ``warehouse_region_rule`` 的唯一键是 ``(shop_id, region_code)`` —— 一个区划码
   只能由一个仓发货，于是**不需要优先级**，具体性由区划码长度决定（见 routing.py）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
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
    """仓库。**商家可自建多个**，每店铺最多一个默认仓（partial unique index 保证）。

    ★ 每个仓有地址（省市区 + 详细地址 + 联系人/电话）。``region_code`` 是**最细一级
      的区划码**，与 ``account.user_address.region_code`` 同一口径 —— **路由匹配它的
      前缀**（见 ``routing.py``）。之前这一列没有任何代码读它，这一轮才真正开始用。

    ★ ``is_default`` 是**路由的兜底**：地址没命中任何区域规则时发这个仓。所以
      "每店必有且仅有一个默认仓"是一条不变量，**默认仓不允许停用**
      （要换先设另一个为默认）—— 否则路由会没有兜底。
    """

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
        String(16),
        nullable=False,
        server_default=text("''"),
        comment="最细一级的行政区划码（6 位），路由按它的前缀匹配",
    )
    # 地址与联系人。命名与 account.user_address 对齐，商城的地址表单可以照着抄
    province: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("''"), comment="省/直辖市，展示用"
    )
    city: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("''"), comment="市"
    )
    district: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("''"), comment="区/县"
    )
    detail: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''"), comment="详细地址（街道门牌）"
    )
    contact_name: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("''"), comment="联系人"
    )
    contact_phone: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("''"), comment="联系电话"
    )
    is_default: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), comment="每店铺最多一个。路由的兜底仓"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1启用 2停用"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class WarehouseRegionRule(Base):
    """仓的发货覆盖区划 —— "这个仓发往哪些地方"。

    ★ 唯一键是 ``(shop_id, region_code)``：**一个区划码只能由一个仓发货**。
      于是这张表**不需要优先级**，具体性完全由**区划码长度**决定 ——
      "4403 深圳 → 深圳仓" 比 "44 广东 → 广州仓" 更具体，收货地址 ``440305``
      两条都命中，取码更长的那个（见 ``routing.warehouse_candidates``）。
      商家的心智也因此简单：一个地方只由一个仓发货。

    ★ 这是**首选**意义上的：规则仓没货时会按候选链兜到别的仓
      （规则仓 → 默认仓 → 其余启用仓）。唯一键保证的只是"不会有两个仓抢同一个区划"。

    ★ ``shop_id`` 是冗余列：路由要按店**批量**取规则（一个订单可能跨店），
      带上它就不必 join ``warehouse``。

    ``region_level`` 由码长推导、**匹配时不参与**，只为后台展示（照 freight 的取舍）。
    """

    __tablename__ = "warehouse_region_rule"
    __table_args__ = (
        UniqueConstraint("shop_id", "region_code", name="uk_warehouse_rule_shop_region"),
        Index("idx_warehouse_rule_shop", "shop_id"),
        {"schema": "inventory", "comment": "仓库的发货覆盖区划"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="冗余：按店批量查规则必需"
    )
    warehouse_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("inventory.warehouse.id"), nullable=False
    )
    region_code: Mapped[str] = mapped_column(
        String(16), nullable=False, comment='前缀匹配；"0" = 该仓兜底覆盖全国'
    )
    region_level: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1省 2市 3区，由码长推导，匹配时不参与"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


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
