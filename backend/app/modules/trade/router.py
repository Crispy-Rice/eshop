"""trade 模块的 HTTP 路由。

买家端 ``/api/orders``、商家端 ``/api/merchant/orders``。
事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, DbSession, IdempotencyKeyDep
from app.core.response import ApiResponse
from app.modules.account.deps import CurrentShopIdDep
from app.modules.trade import service
from app.modules.trade.schemas import (
    MerchantOrderListOut,
    MerchantOrderOut,
    OrderCreateRequest,
    OrderListOut,
    OrderMainOut,
    OrderShipRequest,
)

router = APIRouter()


# ============================================================
# 买家
# ============================================================
@router.post(
    "/api/orders",
    response_model=ApiResponse[OrderMainOut],
    summary="下单（需 Idempotency-Key）",
)
async def create_order(
    session: DbSession,
    body: OrderCreateRequest,
    request_id: IdempotencyKeyDep,
    user: CurrentUserDep,
) -> ApiResponse[OrderMainOut]:
    """下单。

    ★ **必须带 ``Idempotency-Key``**：用户在结算页点两次"提交订单"是很常见的
    （手抖、网络慢、页面没反应），没有幂等键就会下出两个订单、扣两次库存。

    幂等键落到 ``order_main (user_id, request_id)`` 的唯一索引上 ——
    即使 Redis 幂等键过期，数据库仍然挡得住。
    """
    main = await service.create_order(
        session,
        user_id=user.id,
        req=service.CreateOrderRequest(
            items=[{"skuId": int(i.sku_id), "num": i.num} for i in body.items],
            address_id=int(body.address_id),
            coupon_code_ids=[int(c) for c in body.coupon_code_ids],
            buyer_remark=body.buyer_remark,
        ),
        request_id=request_id,
    )
    return ApiResponse.ok(
        await service.get_my_order_detail(session, user_id=user.id, order_main_no=main.order_main_no)
    )


@router.get("/api/orders", response_model=ApiResponse[OrderListOut], summary="我的订单")
async def list_orders(
    session: DbSession,
    user: CurrentUserDep,
    status: int | None = Query(default=None, description="不传=全部。10待付款 20待发货 …"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=10, ge=1, le=50),
) -> ApiResponse[OrderListOut]:
    """我的订单。键集游标分页 —— 翻到第 100 页也不会变慢（docs/07 §10）。"""
    return ApiResponse.ok(
        await service.list_my_orders(
            session, user.id, status=status, cursor=cursor, limit=limit
        )
    )


@router.get(
    "/api/orders/{order_main_no}",
    response_model=ApiResponse[OrderMainOut],
    summary="订单详情",
)
async def get_order(
    session: DbSession, order_main_no: str, user: CurrentUserDep
) -> ApiResponse[OrderMainOut]:
    return ApiResponse.ok(
        await service.get_my_order_detail(session, user_id=user.id, order_main_no=order_main_no)
    )


@router.post(
    "/api/orders/{order_main_no}/cancel",
    response_model=ApiResponse[None],
    summary="取消订单",
)
async def cancel_order(
    session: DbSession, order_main_no: str, user: CurrentUserDep
) -> ApiResponse[None]:
    """取消订单。**幂等**：重复取消不是错误。

    取消会释放库存预占、解锁优惠券、关闭支付单 —— 由 outbox 保证这些副作用
    与事务同生共死（docs/07 §7.3）。
    """
    await service.cancel_order(session, user_id=user.id, order_main_no=order_main_no)
    return ApiResponse.ok(None)


@router.post(
    "/api/order-subs/{order_sub_no}/receive",
    response_model=ApiResponse[None],
    summary="确认收货",
)
async def receive(
    session: DbSession, order_sub_no: str, user: CurrentUserDep
) -> ApiResponse[None]:
    """确认收货。**按子单确认** —— 跨店订单可能有店铺还没发货。

    路径用 ``order-subs`` 而不是 ``orders/{subNo}``：确认收货的对象是子单，
    和「用母单号操作」的接口分开，前端一眼能看出操作的是哪一层（docs/15 §2.2）。
    """
    await service.receive(session, user_id=user.id, order_sub_no=order_sub_no)
    return ApiResponse.ok(None)


# ============================================================
# 商家
# ============================================================
@router.get(
    "/api/merchant/orders",
    response_model=ApiResponse[MerchantOrderListOut],
    summary="商家订单列表",
)
async def list_merchant_orders(
    session: DbSession,
    shop_id: CurrentShopIdDep,
    status: int | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> ApiResponse[MerchantOrderListOut]:
    return ApiResponse.ok(
        await service.list_shop_orders(
            session, shop_id, status=status, cursor=cursor, limit=limit
        )
    )


@router.get(
    "/api/merchant/orders/{order_sub_no}",
    response_model=ApiResponse[MerchantOrderOut],
    summary="商家订单详情",
)
async def get_merchant_order(
    session: DbSession, order_sub_no: str, shop_id: CurrentShopIdDep
) -> ApiResponse[MerchantOrderOut]:
    return ApiResponse.ok(
        await service.get_shop_order(session, shop_id=shop_id, order_sub_no=order_sub_no)
    )


@router.post(
    "/api/merchant/order-subs/{order_sub_no}/ship",
    response_model=ApiResponse[None],
    summary="发货",
)
async def ship(
    session: DbSession,
    order_sub_no: str,
    body: OrderShipRequest,
    shop_id: CurrentShopIdDep,
) -> ApiResponse[None]:
    """发货。

    只能对**待发货**的子单操作 —— 已发货的再发一次会被状态机拒绝
    （不是"静默成功"，商家需要知道单号没被改掉）。
    """
    await service.ship(
        session,
        shop_id=shop_id,
        order_sub_no=order_sub_no,
        express_company=body.express_company,
        express_no=body.express_no,
    )
    return ApiResponse.ok(None)
