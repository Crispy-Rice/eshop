"""trade 模块的请求/响应模型。

金额一律是**整数分**；雪花 ID 在 JSON 里是字符串（见 ``app.core.schemas``）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.core.schemas import CamelModel, Quantity, SnowflakeId
from app.modules.trade.models import (
    AFTERSALE_WINDOW_DAYS,
    STATUS_TEXT,
    DeliveryOrder,
    OrderItem,
    OrderMain,
    OrderSub,
)

PAY_STATUS_TEXT: dict[int, str] = {
    0: "未支付",
    1: "已支付",
    2: "部分退款",
    3: "全额退款",
}

DELIVERY_STATUS_TEXT: dict[int, str] = {
    0: "未发货",
    1: "部分发货",
    2: "已发货",
    3: "已签收",
}


# ============================================================
# 请求
# ============================================================
class OrderCreateItem(CamelModel):
    sku_id: SnowflakeId
    num: Quantity


class OrderCreateRequest(CamelModel):
    """下单。

    幂等键走 ``Idempotency-Key`` 请求头（docs/15 §1.3），不放在 body 里 ——
    这样前端重试时**照原样重发同一个请求**即可，不用改 body。
    """

    items: list[OrderCreateItem] = Field(min_length=1, max_length=100)
    address_id: SnowflakeId
    coupon_code_ids: list[SnowflakeId] = Field(default_factory=list, max_length=20)
    buyer_remark: str | None = Field(default=None, max_length=255)

    @field_validator("items")
    @classmethod
    def _no_duplicate_sku(cls, v: list[OrderCreateItem]) -> list[OrderCreateItem]:
        """同一个 SKU 不能出现两次。

        重复传的话会被当成两行，库存按两行分别预占、退款按两行分别退 ——
        是个很难查的坑，入口就挡掉。
        """
        seen = {int(i.sku_id) for i in v}
        if len(seen) != len(v):
            raise ValueError("同一商品不能重复提交，请合并数量")
        return v


class OrderShipRequest(CamelModel):
    express_company: str = Field(min_length=2, max_length=32, description="快递公司")
    express_no: str = Field(min_length=4, max_length=64, description="快递单号")


# ============================================================
# 响应
# ============================================================
class DeliveryOut(CamelModel):
    delivery_no: str
    express_company: str
    express_no: str
    status: int
    status_text: str
    deliver_time: datetime | None = None


class OrderItemOut(CamelModel):
    """订单项。字段全部来自**下单时的快照**，不反映商品当前的样子。"""

    sku_id: SnowflakeId
    spu_id: SnowflakeId
    title: str = Field(description="下单时的商品标题")
    spec_text: str
    cover_image: str
    unit_price: int = Field(description="下单时的单价（分）")
    num: int
    item_amount: int = Field(description="行小计（分）")
    discount_amount: int
    payable_amount: int


class OrderSubOut(CamelModel):
    """子单：商家履约视角。一个母单下按店铺拆成 N 个。"""

    order_sub_no: str
    shop_id: SnowflakeId
    shop_name: str
    status: int
    status_text: str
    delivery_status: int
    delivery_status_text: str

    total_amount: int
    discount_amount: int = Field(description="子单承担的总优惠")
    freight_amount: int
    payable_amount: int

    deliver_time: datetime | None = None
    receive_time: datetime | None = None
    create_time: datetime

    can_aftersale: bool
    items: list[OrderItemOut] = Field(default_factory=list)
    deliveries: list[DeliveryOut] = Field(default_factory=list)


class OrderMainOut(CamelModel):
    """母单：支付单位 + 用户视角。"""

    order_main_no: str
    status: int
    status_text: str
    pay_status: int
    pay_status_text: str

    shop_count: int
    total_amount: int
    discount_amount: int
    freight_amount: int
    payable_amount: int = Field(description="应付（分）")
    paid_amount: int = Field(description="实付（分）")
    coupon_amount: int
    point_deduction: int

    receiver_name: str
    receiver_phone: str
    receiver_province: str
    receiver_city: str
    receiver_district: str
    receiver_detail: str

    buyer_remark: str | None = None
    freight_detail: dict[str, Any] = Field(default_factory=dict)

    create_time: datetime
    pay_deadline: datetime
    pay_time: datetime | None = None
    finish_time: datetime | None = None

    # 还剩多少秒可支付。前端直接拿它倒计时，不用自己算时区
    pay_remain_seconds: int = Field(default=0, description="剩余支付秒数，<=0 表示已超时")
    can_cancel: bool = Field(default=False)
    can_pay: bool = Field(default=False)
    can_aftersale: bool = Field(default=False)

    subs: list[OrderSubOut] = Field(default_factory=list)


class OrderListItemOut(CamelModel):
    """我的订单列表项。

    只带**前 2 个商品**的缩略信息和一个总件数 —— 列表页不需要每个子单的明细，
    拉全量会让"我的订单"这类高频页面变重（docs/07 §10）。
    """

    order_main_no: str
    status: int
    status_text: str
    pay_status: int
    pay_status_text: str

    payable_amount: int
    total_amount: int

    shop_count: int
    item_kind_count: int = Field(description="有几件商品（行数）")
    total_num: int = Field(description="共几件（数量之和）")
    preview_titles: list[str] = Field(default_factory=list)
    preview_images: list[str] = Field(default_factory=list)

    create_time: datetime
    pay_deadline: datetime
    pay_remain_seconds: int = 0
    can_cancel: bool = False
    can_pay: bool = False


class OrderListOut(CamelModel):
    items: list[OrderListItemOut]
    next_cursor: str | None = None
    has_more: bool = False


class MerchantOrderOut(CamelModel):
    """商家订单列表项。**子单视角** —— 商家只关心自己店里那部分。"""

    order_sub_no: str
    order_main_no: str
    status: int
    status_text: str
    delivery_status: int
    delivery_status_text: str

    shop_id: SnowflakeId
    # 下单时**路由**到的发货仓：决定"这单从哪打包"。历史老单可能为空
    warehouse_id: SnowflakeId | None = None
    warehouse_name: str = ""
    buyer_name: str = Field(description="收货人。发货要用的")
    buyer_phone: str

    total_amount: int
    discount_amount: int
    freight_amount: int
    payable_amount: int = Field(description="本期应结算给商家的金额（分）")

    item_kind_count: int
    total_num: int
    preview_titles: list[str] = Field(default_factory=list)
    preview_images: list[str] = Field(default_factory=list)

    create_time: datetime
    pay_time: datetime | None = None
    deliver_time: datetime | None = None
    receiver_full: str = Field(description="完整收货地址，发货用")

    can_ship: bool = False
    deliveries: list[DeliveryOut] = Field(default_factory=list)


class MerchantOrderListOut(CamelModel):
    items: list[MerchantOrderOut]
    next_cursor: str | None = None
    has_more: bool = False


# ============================================================
# ORM → 响应
# ============================================================
def _items_of_sub(items: list[OrderItem], order_sub_no: str) -> list[OrderItemOut]:
    return [
        OrderItemOut(
            sku_id=it.sku_id,
            spu_id=it.spu_id,
            title=it.spu_title_snap,
            spec_text=it.sku_spec_snap,
            cover_image=it.cover_image_snap,
            unit_price=it.unit_price_snap,
            num=it.num,
            item_amount=it.item_amount,
            discount_amount=it.discount_amount,
            payable_amount=it.payable_amount,
        )
        for it in items
        if it.order_sub_no == order_sub_no
    ]


def _delivery_out(rows: list[DeliveryOrder]) -> list[DeliveryOut]:
    return [
        DeliveryOut(
            delivery_no=d.delivery_no,
            express_company=d.express_company,
            express_no=d.express_no,
            status=d.status,
            status_text={1: "待发货", 2: "已发货", 3: "已签收"}.get(int(d.status), "未知"),
            deliver_time=d.deliver_time,
        )
        for d in rows
    ]


def _remain_seconds(deadline: datetime | None, *, now: datetime) -> int:
    if deadline is None:
        return 0
    return max(0, int((deadline - now).total_seconds()))


def _main_flags(main: OrderMain, *, now: datetime) -> tuple[int, bool, bool, bool]:
    """算出 ``payRemainSeconds`` 与三个「能不能操作」的标识。

    把判断放服务端：前端各页面自己算会算歪（时区、时钟漂移），
    而且规则一变就要改多处。
    """
    status = int(main.status)
    remain = _remain_seconds(main.pay_deadline, now=now)
    can_pay = status == 10 and remain > 0
    # 取消：只有待付款且未支付时能取消
    can_cancel = can_pay
    return remain, can_pay, can_cancel, status == 40


def to_order_main_out(
    main: OrderMain,
    subs: list[OrderSub],
    items: list[OrderItem],
    deliveries: list[DeliveryOrder],
    *,
    now: datetime,
) -> OrderMainOut:
    """母单详情。子单里各自带自己那部分订单项与发货单。"""
    remain, can_pay, can_cancel, can_aftersale = _main_flags(main, now=now)
    return OrderMainOut(
        order_main_no=main.order_main_no,
        status=main.status,
        status_text=STATUS_TEXT.get(int(main.status), "未知"),
        pay_status=main.pay_status,
        pay_status_text=PAY_STATUS_TEXT.get(int(main.pay_status), "未知"),
        shop_count=main.shop_count,
        total_amount=main.total_amount,
        discount_amount=main.discount_amount,
        freight_amount=main.freight_amount,
        payable_amount=main.payable_amount,
        paid_amount=main.paid_amount,
        coupon_amount=main.coupon_amount,
        point_deduction=main.point_deduction,
        receiver_name=main.receiver_name,
        receiver_phone=main.receiver_phone,
        receiver_province=main.receiver_province,
        receiver_city=main.receiver_city,
        receiver_district=main.receiver_district,
        receiver_detail=main.receiver_detail,
        buyer_remark=main.buyer_remark,
        freight_detail=main.freight_detail or {},
        create_time=main.create_time,
        pay_deadline=main.pay_deadline,
        pay_time=main.pay_time,
        finish_time=main.finish_time,
        pay_remain_seconds=remain,
        can_cancel=can_cancel,
        can_pay=can_pay,
        can_aftersale=can_aftersale,
        subs=[
            OrderSubOut(
                order_sub_no=s.order_sub_no,
                shop_id=s.shop_id,
                shop_name=s.shop_name_snap,
                status=s.status,
                status_text=STATUS_TEXT.get(int(s.status), "未知"),
                delivery_status=s.delivery_status,
                delivery_status_text=DELIVERY_STATUS_TEXT.get(int(s.delivery_status), "未知"),
                total_amount=s.total_amount,
                discount_amount=(
                    s.item_discount + s.shop_discount + s.platform_discount + s.point_deduction
                ),
                freight_amount=s.freight_amount,
                payable_amount=s.payable_amount,
                deliver_time=s.deliver_time,
                receive_time=s.receive_time,
                create_time=s.create_time,
                can_aftersale=bool(s.can_aftersale),
                items=_items_of_sub(items, s.order_sub_no),
                deliveries=_delivery_out(
                    [d for d in deliveries if d.order_sub_no == s.order_sub_no]
                ),
            )
            for s in subs
        ],
    )


def to_order_list_item(
    main: OrderMain, items: list[OrderItem], *, now: datetime
) -> OrderListItemOut:
    remain, can_pay, can_cancel, _ = _main_flags(main, now=now)
    return OrderListItemOut(
        order_main_no=main.order_main_no,
        status=main.status,
        status_text=STATUS_TEXT.get(int(main.status), "未知"),
        pay_status=main.pay_status,
        pay_status_text=PAY_STATUS_TEXT.get(int(main.pay_status), "未知"),
        payable_amount=main.payable_amount,
        total_amount=main.total_amount,
        shop_count=main.shop_count,
        item_kind_count=len(items),
        total_num=sum(it.num for it in items),
        preview_titles=[it.spu_title_snap for it in items[:2]],
        preview_images=[it.cover_image_snap for it in items[:2]],
        create_time=main.create_time,
        pay_deadline=main.pay_deadline,
        pay_remain_seconds=remain,
        can_cancel=can_cancel,
        can_pay=can_pay,
    )


def to_merchant_order_out(
    sub: OrderSub,
    main: OrderMain,
    items: list[OrderItem],
    deliveries: list[DeliveryOrder],
    warehouse_name: str = "",
) -> MerchantOrderOut:
    return MerchantOrderOut(
        order_sub_no=sub.order_sub_no,
        order_main_no=sub.order_main_no,
        status=sub.status,
        status_text=STATUS_TEXT.get(int(sub.status), "未知"),
        delivery_status=sub.delivery_status,
        delivery_status_text=DELIVERY_STATUS_TEXT.get(int(sub.delivery_status), "未知"),
        shop_id=sub.shop_id,
        warehouse_id=sub.warehouse_id,
        warehouse_name=warehouse_name,
        buyer_name=main.receiver_name,
        buyer_phone=main.receiver_phone,
        total_amount=sub.total_amount,
        discount_amount=(
            sub.item_discount + sub.shop_discount + sub.platform_discount + sub.point_deduction
        ),
        freight_amount=sub.freight_amount,
        payable_amount=sub.payable_amount,
        item_kind_count=len(items),
        total_num=sum(it.num for it in items),
        preview_titles=[it.spu_title_snap for it in items[:2]],
        preview_images=[it.cover_image_snap for it in items[:2]],
        create_time=sub.create_time,
        pay_time=main.pay_time,
        deliver_time=sub.deliver_time,
        receiver_full=(
            f"{main.receiver_province}{main.receiver_city}{main.receiver_district}"
            f"{main.receiver_detail}"
        ),
        # 只有「待发货」能发货；状态机会再拦一次，这里只是让前端少显示一个无效按钮
        can_ship=int(sub.status) == 20,
        deliveries=_delivery_out(deliveries),
    )


__all__ = [
    "AFTERSALE_WINDOW_DAYS",
    "MerchantOrderListOut",
    "MerchantOrderOut",
    "OrderCreateItem",
    "OrderCreateRequest",
    "OrderListItemOut",
    "OrderListOut",
    "OrderMainOut",
    "OrderShipRequest",
    "OrderSubOut",
    "to_merchant_order_out",
    "to_order_list_item",
    "to_order_main_out",
]
