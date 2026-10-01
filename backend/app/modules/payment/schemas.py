"""payment 模块的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.core.schemas import CamelModel
from app.modules.payment.models import PAY_STATUS_TEXT, Payment


class PaymentCreateRequest(CamelModel):
    order_main_no: str = Field(min_length=20, max_length=32, description="母单号")


class MockCallbackRequest(CamelModel):
    """模拟渠道回调。

    ``tradeNo`` 由渠道生成，这里可省略 —— 不传就自己造一个，方便直接 curl 测试。
    真实渠道的回调原文比这复杂得多，本期不需要。
    """

    trade_no: str | None = Field(default=None, max_length=64)


class PaymentOut(CamelModel):
    pay_no: str
    order_main_no: str
    amount: int = Field(description="应付金额（分）")
    paid_amount: int = Field(description="实付金额（分）")
    channel: str
    status: int
    status_text: str
    pay_time: datetime | None = None
    created_at: datetime

    # 模拟支付入口。前端据此渲染"确认支付"按钮；真实渠道下这里是二维码/跳转链接
    mock_pay_url: str | None = Field(
        default=None, description="模拟支付的回调地址。仅 mock 模式返回"
    )


def to_payment_out(payment: Payment, *, with_mock_url: bool = False) -> PaymentOut:
    """把 ORM 对象转成响应。

    ``mock_pay_url`` 只在发起支付时给 —— 查单接口不需要暴露这个入口。
    """
    return PaymentOut(
        pay_no=payment.pay_no,
        order_main_no=payment.order_main_no,
        amount=payment.amount,
        paid_amount=payment.paid_amount,
        channel=payment.channel,
        status=payment.status,
        status_text=PAY_STATUS_TEXT.get(int(payment.status), "未知"),
        pay_time=payment.pay_time,
        created_at=payment.created_at,
        mock_pay_url=(
            f"/api/payments/{payment.pay_no}/mock-callback" if with_mock_url else None
        ),
    )
