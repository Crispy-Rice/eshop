"""product 模块的 HTTP 路由。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.context import CurrentUser
from app.core.deps import DbSession, require_role
from app.core.response import ApiResponse
from app.modules.account.deps import CurrentShopIdDep
from app.modules.product import service
from app.modules.product.schemas import (
    AdminCategoryTreeOut,
    CategoryCreateRequest,
    CategoryMoveRequest,
    CategoryOut,
    CategoryTreeOut,
    CategoryUpdateRequest,
    SkuBatchRequest,
    SkuBriefOut,
    SkuUpdateRequest,
    SpuAuditRequest,
    SpuCreateRequest,
    SpuDetailOut,
    SpuListOut,
    SpuUpdateRequest,
)

router = APIRouter()

AdminDep = Annotated[CurrentUser, Depends(require_role("admin"))]


# ============================================================
# 买家：浏览与搜索
# ============================================================
@router.get("/api/categories", response_model=ApiResponse[list[CategoryTreeOut]], summary="类目树")
async def list_categories(session: DbSession) -> ApiResponse[list[CategoryTreeOut]]:
    return ApiResponse.ok(await service.list_category_tree(session))


@router.get("/api/search", response_model=ApiResponse[SpuListOut], summary="商品搜索")
async def search(
    session: DbSession,
    keyword: str | None = Query(default=None, max_length=60, description="空格分隔多个关键词"),
    # ★ 查询参数名必须显式写 alias：FastAPI 默认用函数参数名（snake_case），
    #   而外部约定是 camelCase，不写 alias 的话前端传 categoryId 会被静默忽略。
    category_id: int | None = Query(default=None, alias="categoryId"),
    price_from: int | None = Query(default=None, alias="priceFrom", ge=0, description="价格下限（分）"),
    price_to: int | None = Query(default=None, alias="priceTo", ge=0, description="价格上限（分）"),
    sort: str = Query(default="relevance", pattern="^(relevance|sales|newest|price_asc|price_desc)$"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=60),
) -> ApiResponse[SpuListOut]:
    result = await service.search_products(
        session,
        keyword=keyword,
        category_id=category_id,
        price_from=price_from,
        price_to=price_to,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )
    return ApiResponse.ok(result)


@router.get("/api/spus/{spu_id}", response_model=ApiResponse[SpuDetailOut], summary="商品详情")
async def get_spu(spu_id: int, session: DbSession) -> ApiResponse[SpuDetailOut]:
    """返回规格组与 SKU 列表，前端据此渲染规格选择器。"""
    return ApiResponse.ok(await service.get_spu_detail(session, spu_id))


@router.post("/api/skus/batch", response_model=ApiResponse[list[SkuBriefOut]], summary="批量查询 SKU")
async def batch_skus(body: SkuBatchRequest, session: DbSession) -> ApiResponse[list[SkuBriefOut]]:
    """购物车 / 结算页用。已下架或已删除的商品不会出现在结果里。"""
    return ApiResponse.ok(await service.batch_get_skus(session, body.sku_ids))


# ============================================================
# 商家端
# ============================================================
@router.post("/api/merchant/spus", response_model=ApiResponse[SpuDetailOut], summary="发布商品")
async def create_spu(
    body: SpuCreateRequest, shop_id: CurrentShopIdDep, session: DbSession
) -> ApiResponse[SpuDetailOut]:
    return ApiResponse.ok(await service.create_spu(session, shop_id, body))


@router.get("/api/merchant/spus", response_model=ApiResponse[SpuListOut], summary="我的商品")
async def list_my_spus(
    shop_id: CurrentShopIdDep,
    session: DbSession,
    status: int | None = Query(default=None, ge=1, le=5),
    keyword: str | None = Query(default=None, max_length=60),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=60),
) -> ApiResponse[SpuListOut]:
    result = await service.search_products(
        session,
        keyword=keyword,
        sort="newest",
        cursor=cursor,
        limit=limit,
        shop_id=shop_id,
        status=status,
        on_shelf_only=False,
    )
    return ApiResponse.ok(result)


@router.get(
    "/api/merchant/spus/{spu_id}", response_model=ApiResponse[SpuDetailOut], summary="商品详情（商家）"
)
async def get_my_spu(spu_id: int, shop_id: CurrentShopIdDep, session: DbSession) -> ApiResponse[SpuDetailOut]:
    return ApiResponse.ok(await service.get_spu_detail(session, spu_id, owner_shop_id=shop_id))


@router.put("/api/merchant/spus/{spu_id}", response_model=ApiResponse[SpuDetailOut], summary="编辑商品")
async def update_spu(
    spu_id: int, body: SpuUpdateRequest, shop_id: CurrentShopIdDep, session: DbSession
) -> ApiResponse[SpuDetailOut]:
    """只能改标题、副标题、主图、类目、排序权重。

    规格组合与 SKU 集合创建后不可变 —— 订单里存的是 SKU 快照，
    增减 SKU 会让历史订单指向不存在的商品。
    """
    return ApiResponse.ok(await service.update_spu(session, shop_id, spu_id, body))


@router.put("/api/merchant/skus/{sku_id}", response_model=ApiResponse[None], summary="编辑 SKU")
async def update_sku(
    sku_id: int, body: SkuUpdateRequest, shop_id: CurrentShopIdDep, session: DbSession
) -> ApiResponse[None]:
    await service.update_sku(session, shop_id, sku_id, body)
    return ApiResponse.ok(None)


@router.post("/api/merchant/spus/{spu_id}/submit", response_model=ApiResponse[None], summary="提交审核")
async def submit_for_audit(spu_id: int, shop_id: CurrentShopIdDep, session: DbSession) -> ApiResponse[None]:
    await service.submit_for_audit(session, shop_id, spu_id)
    return ApiResponse.ok(None)


@router.post("/api/merchant/spus/{spu_id}/on-shelf", response_model=ApiResponse[None], summary="上架")
async def on_shelf(spu_id: int, shop_id: CurrentShopIdDep, session: DbSession) -> ApiResponse[None]:
    await service.on_shelf(session, shop_id, spu_id)
    return ApiResponse.ok(None)


@router.post("/api/merchant/spus/{spu_id}/off-shelf", response_model=ApiResponse[None], summary="下架")
async def off_shelf(spu_id: int, shop_id: CurrentShopIdDep, session: DbSession) -> ApiResponse[None]:
    await service.off_shelf(session, shop_id, spu_id)
    return ApiResponse.ok(None)


@router.delete("/api/merchant/spus/{spu_id}", response_model=ApiResponse[None], summary="删除商品")
async def delete_spu(spu_id: int, shop_id: CurrentShopIdDep, session: DbSession) -> ApiResponse[None]:
    """软删商品。

    删完它会从商城、商家自己的列表、平台审核列表里一起消失，但：
    - **历史订单不受影响** —— 订单读的是商品快照
    - 库存页、运费模板的绑定列表里也不再出现它（残留行还在库里，只是不展示）

    在售的和审核中的不让删（见 ``service.delete_spu``）。
    """
    await service.delete_spu(session, shop_id, spu_id)
    return ApiResponse.ok(None)


# ============================================================
# 平台运营端
# ============================================================
@router.get(
    "/api/admin/categories",
    response_model=ApiResponse[list[AdminCategoryTreeOut]],
    summary="类目树（含停用节点）",
)
async def list_admin_categories(
    admin: AdminDep, session: DbSession
) -> ApiResponse[list[AdminCategoryTreeOut]]:
    return ApiResponse.ok(await service.list_admin_category_tree(session))


@router.post("/api/admin/categories", response_model=ApiResponse[CategoryOut], summary="新建类目")
async def create_category(
    body: CategoryCreateRequest, admin: AdminDep, session: DbSession
) -> ApiResponse[CategoryOut]:
    return ApiResponse.ok(await service.create_category(session, body, is_admin=True))


@router.put(
    "/api/admin/categories/{category_id}",
    response_model=ApiResponse[CategoryOut],
    summary="改类目（名称/排序/启停）",
)
async def update_category(
    category_id: int, body: CategoryUpdateRequest, admin: AdminDep, session: DbSession
) -> ApiResponse[CategoryOut]:
    return ApiResponse.ok(await service.update_category(session, category_id, body))


@router.post(
    "/api/admin/categories/{category_id}/move",
    response_model=ApiResponse[CategoryOut],
    summary="移动类目（连同子树）",
)
async def move_category(
    category_id: int, body: CategoryMoveRequest, admin: AdminDep, session: DbSession
) -> ApiResponse[CategoryOut]:
    """单独一个动作端点，不并进 PUT：移动会重写整棵子树的 path/level，
    和"改个名字"不是一回事（与既有的 /api/admin/spus/{id}/audit 同一种风格）。"""
    return ApiResponse.ok(await service.move_category(session, category_id, body.parent_id))


@router.delete(
    "/api/admin/categories/{category_id}",
    response_model=ApiResponse[None],
    summary="删除类目（仅叶子且无商品）",
)
async def delete_category(category_id: int, admin: AdminDep, session: DbSession) -> ApiResponse[None]:
    await service.delete_category(session, category_id)
    return ApiResponse.ok(None)


@router.get("/api/admin/spus", response_model=ApiResponse[SpuListOut], summary="商品列表（平台）")
async def list_admin_spus(
    admin: AdminDep,
    session: DbSession,
    status: int | None = Query(default=None, ge=1, le=5),
    keyword: str | None = Query(default=None, max_length=60),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=60),
) -> ApiResponse[SpuListOut]:
    """跨店铺的商品列表，平台运营的审核队列用它。

    ★ 之前只有 ``/api/merchant/spus``（限本店），平台侧**一个能列出商品的接口都没有** ——
      ``/api/admin/spus/{id}/audit`` 只能靠调用方自己知道 spu_id，
      所以审核一直是有接口、没页面。
    """
    return ApiResponse.ok(
        await service.list_admin_spus(
            session, status=status, keyword=keyword, cursor=cursor, limit=limit
        )
    )


@router.post("/api/admin/spus/{spu_id}/audit", response_model=ApiResponse[None], summary="商品审核")
async def audit_spu(
    spu_id: int, body: SpuAuditRequest, admin: AdminDep, session: DbSession
) -> ApiResponse[None]:
    """通过 → 上架；驳回 → 退回草稿。``remark`` 会存进 ``audit_remark`` 给商家看。"""
    await service.audit_spu(session, spu_id, body)
    return ApiResponse.ok(None)
