"""aftersale 模块的请求/响应模型。

金额一律是**整数分**；雪花 ID 在 JSON 里是字符串（见 ``app.core.schemas``）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from app.core.schemas import CamelModel, SnowflakeId
from app.modules.aftersale.models import (
    BEARER_TEXT,
    QUALITY_RESULT_TEXT,
    REASON_TEXT,
    REFUND_STATUS_TEXT,
    REFUND_TYPE_TEXT,
    RefundItem,
    RefundOrder,
)

MAX_REFUND_ITEMS = 50


# ============================================================
# 请求
# ============================================================
class RefundCheckRequest(CamelModel):
    """售后资格预检。不传 items 就是"整个子单能退多少"。"""

    order_sub_no: str = Field(min_length=1, max_length=32)


class RefundApplyLine(CamelModel):
    order_item_id: SnowflakeId
    num: int = Field(ge=1, le=200, strict=True)


class RefundApplyRequest(CamelModel):
    """申请售后。幂等键走 ``Idempotency-Key`` 请求头。

    ``refund_type`` 由后端按子单状态强制推导 —— 这里传的值只用于"前端展示的
    类型与实际不符"时记一条日志，不影响落库。
    """

    order_sub_no: str = Field(min_length=1, max_length=32)
    items: list[RefundApplyLine] = Field(min_length=1, max_length=MAX_REFUND_ITEMS)
    reason_type: int = Field(ge=1, le=6, description="1质量问题 2不想要 3发错货 4少发 5假货 6其他")
    refund_type: int = Field(default=0, ge=0, le=2, description="0=由后端推导")
    reason_desc: str | None = Field(default=None, max_length=255)
    images: list[str] = Field(default_factory=list, max_length=9)

    @model_validator(mode="after")
    def _no_duplicate_item(self) -> RefundApplyRequest:
        """同一个订单项不能出现两次 —— 否则数量会被算两次。"""
        ids = [int(i.order_item_id) for i in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("同一商品不能重复提交，请合并数量")
        return self


class RefundReturnRequest(CamelModel):
    """用户填退货物流。"""

    express_company: str = Field(min_length=2, max_length=32)
    express_no: str = Field(min_length=4, max_length=64)


class RefundApproveRequest(CamelModel):
    remark: str | None = Field(default=None, max_length=255)


class RefundRejectRequest(CamelModel):
    reason: str = Field(min_length=2, max_length=255, description="拒绝理由，买家能看到")


class RefundQualityRequest(CamelModel):
    passed: bool = Field(description="true=质检合格（触发库存回补与退款）")
    remark: str | None = Field(default=None, max_length=255)
    images: list[str] = Field(
        default_factory=list, max_length=9, description="开箱/质检留证照片"
    )


# ============================================================
# 响应
# ============================================================
class RefundItemOut(CamelModel):
    order_item_id: SnowflakeId
    sku_id: SnowflakeId
    title: str
    spec_text: str
    cover_image: str
    refund_num: int
    refund_amount: int = Field(description="该行商品退款金额（分）")
    stock_restored: bool


class RefundOut(CamelModel):
    """售后单详情。买家与商家共用，靠 ``can*`` 标识区分各自能做什么。"""

    refund_no: str
    order_sub_no: str
    order_main_no: str
    shop_id: SnowflakeId
    shop_name: str

    refund_type: int
    refund_type_text: str
    reason_type: int
    reason_type_text: str
    reason_desc: str | None = None
    images: list[str] = Field(default_factory=list)

    refund_amount: int = Field(description="商品退款金额（分），不含运费")
    refund_freight: int = Field(description="退还的运费（分）")
    total_refund: int = Field(description="退款合计 = 商品款 + 运费")
    freight_bearer: int
    freight_bearer_text: str

    status: int
    status_text: str
    source_status: int

    merchant_remark: str | None = None
    reject_reason: str | None = None

    quality_result: int | None = None
    quality_result_text: str | None = None
    quality_remark: str | None = None
    quality_images: list[str] = Field(default_factory=list)

    return_express: str | None = None
    return_express_no: str | None = None

    apply_time: datetime
    merchant_handle_time: datetime | None = None
    return_time: datetime | None = None
    receive_time: datetime | None = None
    quality_time: datetime | None = None
    refund_time: datetime | None = None
    close_time: datetime | None = None
    deadline: datetime | None = Field(default=None, description="当前环节的截止时间")

    # 还能做什么。由服务端算 —— 前端各自判断会判断歪
    can_revoke: bool = False
    can_fill_return: bool = False
    can_approve: bool = False
    can_receive: bool = False
    can_quality: bool = False

    items: list[RefundItemOut] = Field(default_factory=list)


class RefundListItemOut(CamelModel):
    """列表项。只带第一个商品的缩略信息 —— 列表页不需要全量明细。"""

    refund_no: str
    order_sub_no: str
    order_main_no: str
    shop_id: SnowflakeId
    shop_name: str

    refund_type: int
    refund_type_text: str
    status: int
    status_text: str
    total_refund: int

    item_count: int = Field(description="涉及几件商品")
    preview_title: str
    preview_image: str

    apply_time: datetime
    deadline: datetime | None = None

    can_revoke: bool = False
    can_fill_return: bool = False
    can_approve: bool = False
    can_receive: bool = False
    can_quality: bool = False


class RefundListOut(CamelModel):
    items: list[RefundListItemOut]
    next_cursor: str | None = None
    has_more: bool = False


class RefundableItemOut(CamelModel):
    """可退商品。前端据此渲染"选哪件、退几件"。"""

    order_item_id: SnowflakeId
    sku_id: SnowflakeId
    title: str
    spec_text: str
    cover_image: str
    num: int = Field(description="购买数量")
    refunded_num: int = Field(description="已退数量")
    refunding_num: int = Field(description="在退数量")
    max_num: int = Field(description="本次最多可退")
    unit_payable: int = Field(description="单件实付分摊额（分），用于展示最多可退多少钱")


class RefundCheckOut(CamelModel):
    refundable: bool
    reason: str | None = Field(default=None, description="不可退的原因，给用户看")
    refund_type: int
    refund_type_text: str
    max_item_amount: int = Field(description="商品款最多可退（分）")
    max_freight: int = Field(description="运费可退（分）。部分退时为 0")
    deadline: datetime | None = Field(default=None, description="售后窗口截止时间")
    items: list[RefundableItemOut] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)


# ============================================================
# ORM → 响应
# ============================================================
def to_item_out(item: RefundItem) -> RefundItemOut:
    return RefundItemOut(
        order_item_id=item.order_item_id,
        sku_id=item.sku_id,
        title=item.spu_title_snap,
        spec_text=item.sku_spec_snap,
        cover_image=item.cover_image_snap,
        refund_num=item.refund_num,
        refund_amount=item.refund_amount,
        stock_restored=bool(item.stock_restored),
    )


def _actions(order: RefundOrder, items: list[RefundItem] | None) -> dict[str, bool]:
    """按状态算出"谁还能做什么"。买家与商家共用同一份规则。"""
    status = int(order.status)
    is_return = int(order.refund_type) == 2
    return {
        "can_revoke": status in (10, 20, 30),
        "can_fill_return": status == 30,
        "can_approve": status == 10,
        # 只有退货退款才有"收货"与"质检"两个环节
        "can_receive": status == 40 and is_return,
        "can_quality": status == 50 and is_return,
    }


def to_refund_out(
    order: RefundOrder, items: list[RefundItem], *, shop_name: str
) -> RefundOut:
    return RefundOut(
        refund_no=order.refund_no,
        order_sub_no=order.order_sub_no,
        order_main_no=order.order_main_no,
        shop_id=order.shop_id,
        shop_name=shop_name,
        refund_type=order.refund_type,
        refund_type_text=REFUND_TYPE_TEXT.get(int(order.refund_type), "未知"),
        reason_type=order.reason_type,
        reason_type_text=REASON_TEXT.get(int(order.reason_type), "其他"),
        reason_desc=order.reason_desc,
        images=list(order.images or []),
        refund_amount=order.refund_amount,
        refund_freight=order.refund_freight,
        total_refund=int(order.refund_amount) + int(order.refund_freight),
        freight_bearer=order.freight_bearer,
        freight_bearer_text=BEARER_TEXT.get(int(order.freight_bearer), "未知"),
        status=order.status,
        status_text=REFUND_STATUS_TEXT.get(int(order.status), "未知"),
        source_status=order.source_status,
        merchant_remark=order.merchant_remark,
        reject_reason=order.reject_reason,
        quality_result=order.quality_result,
        quality_result_text=(
            QUALITY_RESULT_TEXT.get(int(order.quality_result))
            if order.quality_result is not None
            else None
        ),
        quality_remark=order.quality_remark,
        quality_images=list(order.quality_images or []),
        return_express=order.return_express,
        return_express_no=order.return_express_no,
        apply_time=order.apply_time,
        merchant_handle_time=order.merchant_handle_time,
        return_time=order.return_time,
        receive_time=order.receive_time,
        quality_time=order.quality_time,
        refund_time=order.refund_time,
        close_time=order.close_time,
        deadline=order.deadline,
        items=[to_item_out(i) for i in items],
        **_actions(order, items),
    )


def to_list_item_out(
    order: RefundOrder, items: list[RefundItem], *, shop_name: str
) -> RefundListItemOut:
    first = items[0] if items else None
    return RefundListItemOut(
        refund_no=order.refund_no,
        order_sub_no=order.order_sub_no,
        order_main_no=order.order_main_no,
        shop_id=order.shop_id,
        shop_name=shop_name,
        refund_type=order.refund_type,
        refund_type_text=REFUND_TYPE_TEXT.get(int(order.refund_type), "未知"),
        status=order.status,
        status_text=REFUND_STATUS_TEXT.get(int(order.status), "未知"),
        total_refund=int(order.refund_amount) + int(order.refund_freight),
        item_count=sum(i.refund_num for i in items),
        preview_title=first.spu_title_snap if first else "",
        preview_image=first.cover_image_snap if first else "",
        apply_time=order.apply_time,
        deadline=order.deadline,
        **_actions(order, items),
    )


__all__ = [
    "RefundApplyRequest",
    "RefundCheckOut",
    "RefundCheckRequest",
    "RefundListOut",
    "RefundOut",
    "to_list_item_out",
    "to_refund_out",
]
