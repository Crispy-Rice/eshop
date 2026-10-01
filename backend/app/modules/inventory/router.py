"""inventory 模块的 HTTP 路由。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``（见 core/db.py）。

接口分两拨：
- ``/api/merchant/...`` 商家后台，鉴权走 account 的 ``CurrentShopIdDep``
- ``/api/skus/{id}/stock`` 买家侧，只回**档位**不回真实库存
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, DbSession, IdempotencyKeyDep
from app.core.response import ApiResponse
from app.modules.account.deps import CurrentShopIdDep
from app.modules.inventory import service
from app.modules.inventory.schemas import (
    SkuStockDisplayOut,
    StockAdjustOut,
    StockAdjustRequest,
    StockFlowListOut,
    StockListOut,
    WarehouseCreateRequest,
    WarehouseOut,
)

router = APIRouter()


# ============================================================
# 买家：库存档位
# ============================================================
@router.get(
    "/api/skus/{sku_id}/stock",
    response_model=ApiResponse[SkuStockDisplayOut],
    summary="SKU 库存档位（买家侧，不回传真实库存）",
)
async def sku_stock(session: DbSession, sku_id: int) -> ApiResponse[SkuStockDisplayOut]:
    text_value, sold_out = await service.sku_display(session, sku_id)
    return ApiResponse.ok(
        SkuStockDisplayOut(sku_id=sku_id, text=text_value, sold_out=sold_out)
    )


# ============================================================
# 商家：仓库
# ============================================================
@router.get(
    "/api/merchant/warehouses",
    response_model=ApiResponse[list[WarehouseOut]],
    summary="仓库列表",
)
async def list_warehouses(
    session: DbSession, shop_id: CurrentShopIdDep
) -> ApiResponse[list[WarehouseOut]]:
    return ApiResponse.ok(await service.list_warehouses(session, shop_id))


@router.post(
    "/api/merchant/warehouses",
    response_model=ApiResponse[WarehouseOut],
    summary="新建仓库（首个自动设为默认仓）",
)
async def create_warehouse(
    session: DbSession,
    body: WarehouseCreateRequest,
    shop_id: CurrentShopIdDep,
) -> ApiResponse[WarehouseOut]:
    return ApiResponse.ok(await service.create_warehouse(session, shop_id, body))


# ============================================================
# 商家：库存
# ============================================================
@router.get(
    "/api/merchant/inventory",
    response_model=ApiResponse[StockListOut],
    summary="库存列表",
)
async def list_stock(
    session: DbSession,
    shop_id: CurrentShopIdDep,
    # ★ 查询参数必须显式写 alias：FastAPI 默认用函数参数名（snake_case），
    #   而对外约定是 camelCase，不写 alias 的话前端传 warehouseId 会被静默忽略。
    warehouse_id: Annotated[int | None, Query(alias="warehouseId")] = None,
    sku_id: Annotated[int | None, Query(alias="skuId")] = None,
    cursor: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[StockListOut]:
    return ApiResponse.ok(
        await service.list_stock_out(
            session,
            shop_id,
            warehouse_id=warehouse_id,
            sku_id=sku_id,
            cursor=cursor,
            limit=limit,
        )
    )


@router.post(
    "/api/merchant/inventory/adjust",
    response_model=ApiResponse[StockAdjustOut],
    summary="手工调整库存（需 Idempotency-Key）",
)
async def adjust_stock(
    session: DbSession,
    body: StockAdjustRequest,
    shop_id: CurrentShopIdDep,
    # ★ 必须用 CurrentUserDep（Annotated[CurrentUser, Depends(...)]）。
    #   写成裸的 `user: CurrentUser`，FastAPI 会把它当成**请求体模型**，
    #   连带把 body 也变成嵌套字段，客户端传 {"skuId": ...} 就会报
    #   "body/user: Field required"。
    user: CurrentUserDep,
    idempotency_key: IdempotencyKeyDep,
) -> ApiResponse[StockAdjustOut]:
    """手工增减库存。

    本期**不走审批**（docs/03 §11 原文写的是"需审批"，那是后续补审批模块的事）。
    但一定会写 ``stock_flow`` 流水并记录操作人——流水是唯一能回答
    "这批库存是谁改的"的地方。
    """
    return ApiResponse.ok(
        await service.adjust_for_merchant(
            session,
            shop_id,
            body,
            operator=f"user:{user.id}",
            idempotency_key=idempotency_key,
        )
    )


@router.get(
    "/api/merchant/inventory/flows",
    response_model=ApiResponse[StockFlowListOut],
    summary="库存流水",
)
async def list_flows(
    session: DbSession,
    shop_id: CurrentShopIdDep,
    sku_id: Annotated[int | None, Query(alias="skuId")] = None,
    warehouse_id: Annotated[int | None, Query(alias="warehouseId")] = None,
    cursor: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[StockFlowListOut]:
    return ApiResponse.ok(
        await service.list_flows_out(
            session,
            shop_id,
            sku_id=sku_id,
            warehouse_id=warehouse_id,
            cursor=cursor,
            limit=limit,
        )
    )
