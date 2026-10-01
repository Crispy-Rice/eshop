"""aftersale 模块的 HTTP 路由。

买家端 ``/api/aftersales``、商家端 ``/api/merchant/aftersales``。
事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, DbSession, IdempotencyKeyDep
from app.core.response import ApiResponse
from app.modules.account.deps import CurrentShopIdDep
from app.modules.aftersale import service
from app.modules.aftersale.schemas import (
    RefundApplyRequest,
    RefundApproveRequest,
    RefundCheckOut,
    RefundCheckRequest,
    RefundListOut,
    RefundOut,
    RefundQualityRequest,
    RefundRejectRequest,
    RefundReturnRequest,
)

router = APIRouter()


# ============================================================
# 买家
# ============================================================
@router.post(
    "/api/aftersales/check",
    response_model=ApiResponse[RefundCheckOut],
    summary="售后资格预检（能退多少、退哪种类型）",
)
async def check_eligibility(
    session: DbSession, body: RefundCheckRequest, user: CurrentUserDep
) -> ApiResponse[RefundCheckOut]:
    """进入申请页前先问一次。

    让用户**在填表前**就知道能退多少钱、运费退不退、为什么不能退 ——
    而不是提交完才被拒。
    """
    return ApiResponse.ok(
        await service.check_eligibility(
            session, user_id=user.id, order_sub_no=body.order_sub_no
        )
    )


@router.post(
    "/api/aftersales",
    response_model=ApiResponse[RefundOut],
    summary="申请售后（需 Idempotency-Key）",
)
async def apply_refund(
    session: DbSession,
    body: RefundApplyRequest,
    request_id: IdempotencyKeyDep,
    user: CurrentUserDep,
) -> ApiResponse[RefundOut]:
    """申请售后。

    ★ **必须带 ``Idempotency-Key``**：用户在申请页连点两次很常见，
    没有幂等键会提交出两张售后单（虽然唯一索引会拦下第二张，
    但用户会看到一个莫名其妙的报错）。

    售后类型由后端按子单状态强制推导 —— 传上来的 ``refundType`` 只用于对日志取证。
    """
    order = await service.apply(
        session,
        user_id=user.id,
        req=service.ApplyRequest(
            order_sub_no=body.order_sub_no,
            items=[
                service.ApplyItem(order_item_id=int(i.order_item_id), num=i.num)
                for i in body.items
            ],
            reason_type=body.reason_type,
            refund_type=body.refund_type,
            reason_desc=body.reason_desc,
            images=body.images,
        ),
        request_id=request_id,
    )
    return ApiResponse.ok(
        await service.get_detail_for_user(session, user_id=user.id, refund_no=order.refund_no)
    )


@router.get("/api/aftersales", response_model=ApiResponse[RefundListOut], summary="我的售后")
async def list_my_refunds(
    session: DbSession,
    user: CurrentUserDep,
    status: int | None = Query(default=None, description="不传=全部"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=10, ge=1, le=50),
) -> ApiResponse[RefundListOut]:
    return ApiResponse.ok(
        await service.list_my_refunds(session, user.id, status=status, cursor=cursor, limit=limit)
    )


@router.get(
    "/api/aftersales/{refund_no}",
    response_model=ApiResponse[RefundOut],
    summary="售后详情",
)
async def get_refund(
    session: DbSession, refund_no: str, user: CurrentUserDep
) -> ApiResponse[RefundOut]:
    return ApiResponse.ok(
        await service.get_detail_for_user(session, user_id=user.id, refund_no=refund_no)
    )


@router.post(
    "/api/aftersales/{refund_no}/return",
    response_model=ApiResponse[None],
    summary="填写退货物流",
)
async def fill_return(
    session: DbSession, refund_no: str, body: RefundReturnRequest, user: CurrentUserDep
) -> ApiResponse[None]:
    """寄回商品后填快递单号。**这一步不动库存** —— 要等商家签收并质检合格。"""
    await service.fill_return_express(
        session,
        user_id=user.id,
        refund_no=refund_no,
        express_company=body.express_company,
        express_no=body.express_no,
    )
    return ApiResponse.ok(None)


@router.post(
    "/api/aftersales/{refund_no}/revoke",
    response_model=ApiResponse[None],
    summary="撤销申请",
)
async def revoke(
    session: DbSession, refund_no: str, user: CurrentUserDep
) -> ApiResponse[None]:
    """撤销售后。商品已寄出（待商家收货及之后）就不能撤了。

    撤销后子单回到申请前的状态，预占的退货数量一并释放。
    """
    await service.revoke(session, user_id=user.id, refund_no=refund_no)
    return ApiResponse.ok(None)


# ============================================================
# 商家
# ============================================================
@router.get(
    "/api/merchant/aftersales",
    response_model=ApiResponse[RefundListOut],
    summary="商家售后列表",
)
async def list_shop_refunds(
    session: DbSession,
    shop_id: CurrentShopIdDep,
    status: int | None = Query(default=None),
    pending_only: bool = Query(default=False, description="只看待我处理的"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> ApiResponse[RefundListOut]:
    return ApiResponse.ok(
        await service.list_shop_refunds(
            session,
            shop_id,
            status=status,
            pending_only=pending_only,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get(
    "/api/merchant/aftersales/{refund_no}",
    response_model=ApiResponse[RefundOut],
    summary="商家售后详情",
)
async def get_shop_refund(
    session: DbSession, refund_no: str, shop_id: CurrentShopIdDep
) -> ApiResponse[RefundOut]:
    return ApiResponse.ok(
        await service.get_detail_for_shop(session, shop_id=shop_id, refund_no=refund_no)
    )


@router.post(
    "/api/merchant/aftersales/{refund_no}/approve",
    response_model=ApiResponse[None],
    summary="同意售后",
)
async def approve(
    session: DbSession, refund_no: str, body: RefundApproveRequest, shop_id: CurrentShopIdDep
) -> ApiResponse[None]:
    """同意售后。

    - 退货退款：等买家寄回，**不动库存**
    - 仅退款：货没出库，立即回补库存并开始退款
    """
    await service.approve(session, shop_id=shop_id, refund_no=refund_no, remark=body.remark)
    return ApiResponse.ok(None)


@router.post(
    "/api/merchant/aftersales/{refund_no}/reject",
    response_model=ApiResponse[None],
    summary="拒绝售后",
)
async def reject(
    session: DbSession, refund_no: str, body: RefundRejectRequest, shop_id: CurrentShopIdDep
) -> ApiResponse[None]:
    await service.reject(session, shop_id=shop_id, refund_no=refund_no, reason=body.reason)
    return ApiResponse.ok(None)


@router.post(
    "/api/merchant/aftersales/{refund_no}/receive",
    response_model=ApiResponse[None],
    summary="确认收到退货",
)
async def receive(
    session: DbSession, refund_no: str, shop_id: CurrentShopIdDep
) -> ApiResponse[None]:
    """签收买家寄回的商品。**还不回补库存** —— 要等质检合格。"""
    await service.merchant_receive(session, shop_id=shop_id, refund_no=refund_no)
    return ApiResponse.ok(None)


@router.post(
    "/api/merchant/aftersales/{refund_no}/quality",
    response_model=ApiResponse[None],
    summary="提交质检结果",
)
async def quality(
    session: DbSession, refund_no: str, body: RefundQualityRequest, shop_id: CurrentShopIdDep
) -> ApiResponse[None]:
    """提交质检结果。

    ★ **合格才回补库存并发起退款**；不合格商品进残次品池、不回补可售库存，
    售后单直接终结（本期不做"商家寄回商品"的回程）。
    """
    await service.quality(
        session,
        shop_id=shop_id,
        refund_no=refund_no,
        passed=body.passed,
        remark=body.remark,
        images=body.images,
    )
    return ApiResponse.ok(None)
