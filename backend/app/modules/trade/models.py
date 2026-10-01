"""trade 模块的 ORM 模型。

对应 docs/07-order-and-split.md §3。

**母子单模型**（本模块的根本设计）：

- **母单**是**支付单位**，用户只看到它、只对它付一次钱
- **子单**是**履约单位**，按店铺拆，各自有独立的状态机、发货单、物流单号
- 母子单之间**金额必须守恒**，由数据库的 CHECK 约束兜底

为什么必须拆（docs/07 §1）：跨店购买时，商家 A 无法操作包含商家 B 商品的订单；
不同店铺运费规则不同；退款要能定位到具体店铺；平台要按店铺分账；
而"A 已发货 B 未发货"这种状态，用一个字段根本表达不了。

**订单一旦创建，金额与 `_snap` 字段永不可变**（docs/07 §9）——
变更即对账不平，而快照是买卖双方的契约。需要改价时只能取消原单重新下单。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# ---------------- 母单 / 子单状态（同一套编码）----------------
ORDER_WAIT_PAY = 10
ORDER_WAIT_DELIVER = 20
ORDER_WAIT_RECEIVE = 30
ORDER_FINISHED = 40
ORDER_CLOSED = 50
ORDER_REFUNDING = 60
ORDER_REFUNDED = 70

STATUS_TEXT: dict[int, str] = {
    ORDER_WAIT_PAY: "待付款",
    ORDER_WAIT_DELIVER: "待发货",
    ORDER_WAIT_RECEIVE: "待收货",
    ORDER_FINISHED: "已完成",
    ORDER_CLOSED: "已关闭",
    ORDER_REFUNDING: "退款中",
    ORDER_REFUNDED: "已退款",
}

# 母单支付状态（与履约状态正交，单独维护）
PAY_UNPAID = 0
PAY_PAID = 1
PAY_PARTIAL_REFUND = 2
PAY_FULL_REFUND = 3

# 履约状态
DELIVERY_NONE = 0
DELIVERY_PARTIAL = 1
DELIVERY_ALL = 2
DELIVERY_RECEIVED = 3

# 发货单状态
DELIVERY_ORDER_WAIT = 1
DELIVERY_ORDER_SENT = 2
DELIVERY_ORDER_RECEIVED = 3

# 状态流水的主体类型
ORDER_TYPE_MAIN = 1
ORDER_TYPE_SUB = 2
ORDER_TYPE_REFUND = 3

# 操作人类型
OPERATOR_USER = 1
OPERATOR_MERCHANT = 2
OPERATOR_SYSTEM = 3
OPERATOR_PLATFORM = 4

# 订单来源（第一期只有 PC）
ORDER_SOURCE_PC = 4

# 支付截止时长
PAY_DEADLINE_MINUTES = 30
# 发货后自动确认收货的天数
AUTO_RECEIVE_DAYS = 15
# 完成后可申请售后的天数
AFTERSALE_WINDOW_DAYS = 7


class OrderMain(Base):
    """母单：支付单位 + 用户视角。

    ``discount_amount`` 是**生成列**（``GENERATED ALWAYS AS ... STORED``）：
    金额守恒由数据库保证，应用算错了也写不进去。
    """

    __tablename__ = "order_main"
    __table_args__ = (
        UniqueConstraint("order_main_no", name="uk_order_main_no"),
        Index("idx_order_main_user", "user_id", "status", text("create_time DESC")),
        # ★ 下单幂等的持久化兜底：Redis 幂等键丢失时仍能挡住重复下单
        Index("uk_order_main_request", "user_id", "request_id", unique=True),
        # 超时关单扫描：部分索引，只含待付款订单，体积极小
        Index(
            "idx_order_main_pay_deadline",
            "pay_deadline",
            postgresql_where=text("status = 10"),
        ),
        CheckConstraint(
            "payable_amount = total_amount - item_discount - shop_discount "
            "- platform_discount - point_deduction + freight_amount AND payable_amount >= 0",
            name="payable_conserved",
        ),
        CheckConstraint("refunded_amount <= paid_amount", name="refund_not_exceed_paid"),
        {"schema": "trade", "comment": "母单（支付与用户视角）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_main_no: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_id: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="下单请求的幂等键"
    )
    shop_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    # 金额（含从子单汇总来的分摊）
    total_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="商品原价总额")
    item_discount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    shop_discount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    platform_discount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    coupon_amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="券优惠总额（含店铺券+平台券），是上面各项的子集",
    )
    point_deduction: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    point_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    freight_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    payable_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 生成列：优惠合计永远等于各项之和，算错了也写不进去
    discount_amount: Mapped[int] = mapped_column(
        BigInteger,
        Computed(
            "item_discount + shop_discount + platform_discount + point_deduction",
            persisted=True,
        ),
        nullable=False,
        comment="优惠合计（生成列）",
    )
    paid_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="实付（支付回调写入）"
    )
    refunded_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="累计已退"
    )

    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("10"), comment="见 STATUS_TEXT"
    )
    pay_status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="0未付 1已付 2部分退 3全退"
    )

    # 收货信息快照（下单时冻结，之后改地址不影响历史订单）
    receiver_name: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    receiver_province: Mapped[str] = mapped_column(String(32), nullable=False)
    receiver_city: Mapped[str] = mapped_column(String(32), nullable=False)
    receiver_district: Mapped[str] = mapped_column(String(32), nullable=False)
    receiver_detail: Mapped[str] = mapped_column(String(255), nullable=False)
    region_code: Mapped[str] = mapped_column(String(16), nullable=False, comment="运费计算与统计用")
    freight_detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="运费计算明细快照（模板改了也能追溯）"
    )

    order_source: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("4"), comment="4=PC"
    )
    buyer_remark: Mapped[str | None] = mapped_column(String(255))

    create_time: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    pay_deadline: Mapped[datetime] = mapped_column(TS, nullable=False, comment="创建 + 30min")
    pay_time: Mapped[datetime | None] = mapped_column(TS)
    finish_time: Mapped[datetime | None] = mapped_column(TS)
    close_time: Mapped[datetime | None] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class OrderSub(Base):
    """子单：商家履约视角。状态独立流转，母单状态由它们聚合得出。"""

    __tablename__ = "order_sub"
    __table_args__ = (
        UniqueConstraint("order_sub_no", name="uk_order_sub_no"),
        Index("idx_order_sub_main", "order_main_no"),
        Index("idx_order_sub_user", "user_id", "status", text("create_time DESC")),
        # 商家后台的主查询路径（第一期单库直接走索引，不需要 ES）
        Index("idx_order_sub_shop", "shop_id", "status", text("create_time DESC")),
        Index(
            "idx_order_sub_auto_finish",
            "auto_finish_time",
            postgresql_where=text("status = 30"),
        ),
        CheckConstraint(
            "payable_amount = total_amount - item_discount - shop_discount "
            "- platform_discount - point_deduction + freight_amount AND payable_amount >= 0",
            name="payable_conserved",
        ),
        CheckConstraint("refunded_amount <= payable_amount", name="refund_not_exceed_payable"),
        {"schema": "trade", "comment": "子单（商家履约视角）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_sub_no: Mapped[str] = mapped_column(String(32), nullable=False, comment="母单号 + '-' + 序号")
    order_main_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("trade.order_main.order_main_no"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="冗余，便于按用户查")
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    shop_name_snap: Mapped[str] = mapped_column(String(64), nullable=False, comment="店铺名快照")

    total_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_discount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    shop_discount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    platform_discount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    coupon_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    point_deduction: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    freight_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    payable_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    refunded_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("10"), comment="见 STATUS_TEXT"
    )

    delivery_status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="0未发 1部分 2全部 3已签收"
    )
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    has_aftersale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否有进行中的售后"
    )
    can_aftersale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    is_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    create_time: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    deliver_time: Mapped[datetime | None] = mapped_column(TS)
    receive_time: Mapped[datetime | None] = mapped_column(TS)
    finish_time: Mapped[datetime | None] = mapped_column(TS)
    close_time: Mapped[datetime | None] = mapped_column(TS)
    auto_finish_time: Mapped[datetime | None] = mapped_column(TS, comment="自动确认收货时间")
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class OrderItem(Base):
    """订单项。**所有 ``_snap`` 字段写入后永不变更**。

    商家三天后改了标题/图/价格，历史订单不能跟着变 —— 否则退款金额算不清，
    用户看到的和买到的不一样（docs/02 §5）。
    """

    __tablename__ = "order_item"
    __table_args__ = (
        Index("idx_order_item_sub", "order_sub_no"),
        Index("idx_order_item_main", "order_main_no"),
        Index("idx_order_item_sku", "sku_id"),
        CheckConstraint("num > 0", name="num_positive"),
        CheckConstraint("discount_amount >= 0", name="discount_non_negative"),
        # 数量守恒：已退 + 在退 不能超过购买数量。售后申请时预占 refunding_num，
        # 退款成功时才转成 refunded_num（docs/08 §9）
        CheckConstraint("refunded_num + refunding_num <= num", name="refund_num_conserved"),
        CheckConstraint(
            "refunded_amount >= 0 AND refunded_amount <= payable_amount",
            name="refund_amount_conserved",
        ),
        {"schema": "trade", "comment": "订单项（商品快照）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_main_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_sub_no: Mapped[str] = mapped_column(String(32), nullable=False)
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spu_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ---- ★ 快照字段：写入后永不变更 ----
    spu_title_snap: Mapped[str] = mapped_column(String(120), nullable=False)
    sku_spec_snap: Mapped[str] = mapped_column(String(255), nullable=False, comment="如 暗夜黑;256G")
    cover_image_snap: Mapped[str] = mapped_column(String(255), nullable=False)
    unit_price_snap: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="下单时单价")
    weight_g_snap: Mapped[int] = mapped_column(Integer, nullable=False, comment="退款算运费要用")

    num: Mapped[int] = mapped_column(Integer, nullable=False)
    item_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="= unit_price_snap * num，行小计"
    )

    # 优惠分摊。**退款时直接读这几个字段，不重算** —— 重算会因为基数变化而和用户付的对不上
    discount_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="本行承担的总优惠"
    )
    coupon_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    promo_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    point_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    payable_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="本行实付 = item_amount - discount_amount"
    )

    # ---- 退款（docs/08 §9）。商品款记在这三个字段上，运费记在售后单上 ----
    refunding_num: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="申请中、尚未退款完成的件数。申请时预占，成功时转为 refunded_num",
    )
    refunded_num: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="已退款完成的件数"
    )
    refunded_amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="已退的商品款。差额法要看它",
    )

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class OrderDiscountSnapshot(Base):
    """订单优惠快照。审计与退款依据（docs/05 §9）。

    ``UNIQUE (order_main_no, level, source_type, source_id)`` 防同一活动的优惠被重复记账。
    ``source_id`` 用 ``NOT NULL DEFAULT 0`` 而不是可空 —— PG 的唯一约束里 NULL 互不相等，
    可空列会让积分类快照绕过唯一性。
    """

    __tablename__ = "order_discount_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "order_main_no", "level", "source_type", "source_id", name="uk_discount_snap"
        ),
        Index("idx_discount_snap_source", "source_type", "source_id"),
        {"schema": "trade", "comment": "订单优惠快照（审计与退款依据）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_main_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_sub_no: Mapped[str | None] = mapped_column(String(32), comment="空表示平台级")
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="0单品 1店铺 2平台 3积分")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    source_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="快照名称")
    rule_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="规则快照：threshold/discountValue/rate/scope"
    )
    discount_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class DeliveryOrder(Base):
    """发货单。子单内**按仓库拆** —— 一个子单可能从多个仓发货，各有一个物流单号。"""

    __tablename__ = "delivery_order"
    __table_args__ = (
        UniqueConstraint("delivery_no", name="uk_delivery_no"),
        # 同一张快递单号不能被两个发货单用（顺手挡住商家填错单号）
        UniqueConstraint("express_company", "express_no", name="uk_delivery_express"),
        Index("idx_delivery_sub", "order_sub_no"),
        {"schema": "trade", "comment": "发货单"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_sub_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("trade.order_sub.order_sub_no"), nullable=False
    )
    order_main_no: Mapped[str] = mapped_column(String(32), nullable=False)
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    express_company: Mapped[str] = mapped_column(String(32), nullable=False)
    express_no: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1待发 2已发 3已签收"
    )
    deliver_time: Mapped[datetime | None] = mapped_column(TS)
    receive_time: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class DeliveryItem(Base):
    """发货单与订单项的多对多 —— 一个订单项可能分多次发货。"""

    __tablename__ = "delivery_item"
    __table_args__ = (
        UniqueConstraint("delivery_no", "order_item_id", name="uk_delivery_item"),
        CheckConstraint("num > 0", name="num_positive"),
        {"schema": "trade"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    delivery_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("trade.delivery_order.delivery_no"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("trade.order_item.id"), nullable=False
    )
    num: Mapped[int] = mapped_column(Integer, nullable=False)


class OrderStateFlow(Base):
    """订单状态流转流水。**不可变的审计日志**。

    docs/16 §4 说应用账号对这张表只授予 INSERT/SELECT 权限 ——
    从**权限层面**保证不可改。纠纷时靠它还原订单的完整生命历史。
    """

    __tablename__ = "order_state_flow"
    __table_args__ = (Index("idx_state_flow_order", "order_no", "created_at"), {"schema": "trade"})

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_type: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="1母单 2子单 3售后单")
    order_no: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    to_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    operator_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1用户 2商家 3系统 4平台客服"
    )
    operator_id: Mapped[str | None] = mapped_column(String(64))
    remark: Mapped[str | None] = mapped_column(String(255))
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB, comment="上下文快照")
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
