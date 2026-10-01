"""payment 模块的 HTTP 路由。

三个接口，够把链路走通（docs/09）：发起支付、模拟回调、查单。
事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUserDep, DbSession
from app.core.response import ApiResponse
from app.modules.payment import service
from app.modules.payment.schemas import (
    MockCallbackRequest,
    PaymentCreateRequest,
    PaymentOut,
    to_payment_out,
)

router = APIRouter()


@router.post("/api/payments", response_model=ApiResponse[PaymentOut], summary="发起支付")
async def create_payment(
    session: DbSession, body: PaymentCreateRequest, user: CurrentUserDep
) -> ApiResponse[PaymentOut]:
    """发起支付。**幂等**：同一母单重复调用返回同一张支付单。

    响应的 ``mockPayUrl`` 就是模拟渠道的"去支付"入口，前端把它渲染成按钮。
    """
    payment = await service.create_payment(
        session, user_id=user.id, order_main_no=body.order_main_no
    )
    return ApiResponse.ok(to_payment_out(payment, with_mock_url=True))


@router.post(
    "/api/payments/{pay_no}/mock-callback",
    response_model=ApiResponse[PaymentOut],
    summary="模拟渠道回调（仅 mock 模式）",
)
async def mock_callback(
    session: DbSession, pay_no: str, body: MockCallbackRequest
) -> ApiResponse[PaymentOut]:
    """模拟渠道回调。**不需要登录** —— 真实渠道回调也是不带用户态的服务端请求。

    ★ **幂等**：重复回调只推进一次订单。测试要专门覆盖这条
    （渠道重试是常态，不是异常）。
    """
    payment = await service.mock_callback(
        session, pay_no=pay_no, trade_no=body.trade_no
    )
    return ApiResponse.ok(to_payment_out(payment))


@router.get("/api/payments/{pay_no}", response_model=ApiResponse[PaymentOut], summary="查单")
async def get_payment(
    session: DbSession, pay_no: str, user: CurrentUserDep
) -> ApiResponse[PaymentOut]:
    """查单。带归属校验 —— 查不了别人的支付单。"""
    payment = await service.get_payment(session, user_id=user.id, pay_no=pay_no)
    return ApiResponse.ok(to_payment_out(payment))
