"""cart 模块的 HTTP 路由。

契约见 docs/15-api-and-errors.md §2.2，**用 ``skuId`` 而不是 cart_item.id 定位**
——加购时前端还不知道 id。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from fastapi import APIRouter, Body

from app.core.deps import CurrentUserDep, DbSession
from app.core.response import ApiResponse
from app.modules.cart import service
from app.modules.cart.schemas import (
    CartAddRequest,
    CartCountOut,
    CartNumRequest,
    CartOut,
    CartSelectRequest,
    CartSkuIdsRequest,
)

router = APIRouter()


@router.get("/api/cart", response_model=ApiResponse[CartOut], summary="购物车（按店铺分组）")
async def get_cart(session: DbSession, user: CurrentUserDep) -> ApiResponse[CartOut]:
    return ApiResponse.ok(await service.list_cart(session, user.id))


@router.get("/api/cart/count", response_model=ApiResponse[CartCountOut], summary="购物车角标")
async def get_cart_count(session: DbSession, user: CurrentUserDep) -> ApiResponse[CartCountOut]:
    return ApiResponse.ok(CartCountOut(count=await service.count_skus(session, user.id)))


@router.post("/api/cart/items", response_model=ApiResponse[None], summary="加购（累加）")
async def add_item(
    session: DbSession, body: CartAddRequest, user: CurrentUserDep
) -> ApiResponse[None]:
    """加购。

    ★ **刻意不幂等**（docs/15 §2.2）：加购的语义就是累加，
    重复提交应该变成 2 件，而不是被幂等键拦掉。
    """
    await service.add_item(session, user.id, body)
    return ApiResponse.ok(None)


@router.put(
    "/api/cart/items/{sku_id}", response_model=ApiResponse[None], summary="修改数量（SET）"
)
async def update_num(
    session: DbSession, sku_id: int, body: CartNumRequest, user: CurrentUserDep
) -> ApiResponse[None]:
    """把数量**设为**指定值，不是累加。重复调用结果相同，天然幂等。"""
    await service.update_num(session, user.id, sku_id, body.num)
    return ApiResponse.ok(None)


@router.delete("/api/cart/items", response_model=ApiResponse[None], summary="批量删除")
async def delete_items(
    session: DbSession, user: CurrentUserDep, body: CartSkuIdsRequest = Body(...)
) -> ApiResponse[None]:
    """批量删除。已不存在的条目不算错——删除天然幂等。"""
    await service.delete_items(session, user.id, body.sku_ids)
    return ApiResponse.ok(None)


@router.delete("/api/cart/invalid", response_model=ApiResponse[None], summary="清除失效商品")
async def clear_invalid(session: DbSession, user: CurrentUserDep) -> ApiResponse[None]:
    """清除失效商品。

    这是 docs/15 契约之外的补充接口：本次确认"失效项不自动删、由用户主动清"，
    所以需要一个由用户触发的清理入口。判定用**实时状态**而不只看
    "商品查不到"——已下架的商品同样是买了也没用的。
    """
    await service.clear_invalid(session, user.id)
    return ApiResponse.ok(None)


@router.put("/api/cart/select", response_model=ApiResponse[None], summary="勾选/取消勾选")
async def select_items(
    session: DbSession, body: CartSelectRequest, user: CurrentUserDep
) -> ApiResponse[None]:
    """批量勾选。``skuIds`` 为空表示全选/全不选。"""
    await service.set_selected(session, user.id, body.sku_ids, body.selected)
    return ApiResponse.ok(None)
