"""product 模块的 HTTP 路由。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.context import CurrentUser
from app.core.deps import DbSession, require_role
from app.core.errors import BizError, ErrorCode
from app.core.response import ApiResponse
from app.modules.account.deps import CurrentShopIdDep
from app.modules.freight import service as freight_service
from app.modules.product import service
from app.modules.product.models import SPU_ON_SHELF, SPU_PENDING_AUDIT, SPU_REJECTED
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
    SpuSpecsReplaceRequest,
    SpuUpdateRequest,
)
from app.modules.trade import service as trade_service

router = APIRouter()

AdminDep = Annotated[CurrentUser, Depends(require_role("admin"))]


async def _assert_freight_ready(session: DbSession, *, shop_id: int, sku_ids: list[int]) -> None:
    """商品"能不能上架"的最后一道检查：不能有算不出运费的规格。

    ★ 没绑运费模板的 SKU，原先只在**买家结算**时才报错（``freight.estimate``），
      那时商品已经挂在架上了 —— 上架是最后一道能拦住"卖不出去的商品"的关口。
    ★ 跨模块只读放路由层：product 不能反向 import freight（与「改规格先查订单」
      同一套路，见 ``replace_spu_specs``）。判据由 freight 给，这里只负责把它
      变成一句能照着做的提示。
    ★ **两条**上架路径都要过这道检查：商家的「上架」与平台的「审核通过」——
      后者直接置为在售，只挡商家那条等于给平台留了个后门。
    """
    missing = await freight_service.skus_without_freight(
        session, shop_id=shop_id, sku_ids=sku_ids
    )
    if missing:
        raise BizError(
            ErrorCode.VALIDATION_ERROR,
            f"这个商品有 {len(missing)} 个规格还算不出运费（既没绑定运费模板，"
            "店铺也没有设默认模板），无法上架。先到「运费模板」把它们绑定，"
            "或给店铺设一个默认模板",
        )


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
    # 店铺页用：只看这家店的商品。
    # ★ 走的是与搜索完全相同的 service/repository 路径（`_spu_filters` 里本来就有
    #   `shop_id` 这一条），**不要为店铺页另写查询** —— 另写一条必然漏掉
    #   `deleted = false` 与 `status = 2`（已上架）两个过滤，于是店铺页会陈列已下架商品。
    # ★ 这里**不校验店铺是否存在**：查不到就是空列表。店铺页自己会去取店铺信息、
    #   在那儿报"店铺不存在" —— 搜索接口保持"只负责筛，不做实体校验"这一条。
    shop_id: int | None = Query(default=None, alias="shopId"),
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
        shop_id=shop_id,
        price_from=price_from,
        price_to=price_to,
        sort=sort,
        cursor=cursor,
        limit=limit,
        # 只有这一条要给"共 N 件商品"。商家/运营列表页没有这个文案，别替它们跑 COUNT
        with_total=True,
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
    # ★ 上界跟着 SPU_REJECTED 走，别写死数字 —— 加了状态却忘了改这里，
    #   商家那个筛选 tab 会静默 422（前端拿到错误什么都不显示，最难查的一种坏法）。
    status: int | None = Query(default=None, ge=1, le=SPU_REJECTED),
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
    detail = await service.get_spu_detail(session, spu_id, owner_shop_id=shop_id)
    # 「能不能改规格」只有这一层算得出来：它同时看得见 product 与 trade，
    # 而 product 不能反过来 import trade（会成环，见 docs/01 §2）。
    detail.spec_editable = detail.status not in (
        SPU_ON_SHELF,
        SPU_PENDING_AUDIT,
    ) and not await trade_service.spu_has_order_items(session, spu_id)
    return ApiResponse.ok(detail)


@router.put(
    "/api/merchant/spus/{spu_id}/specs",
    response_model=ApiResponse[SpuDetailOut],
    summary="替换规格与 SKU",
)
async def replace_spu_specs(
    spu_id: int, body: SpuSpecsReplaceRequest, shop_id: CurrentShopIdDep, session: DbSession
) -> ApiResponse[SpuDetailOut]:
    """整体替换规格组与 SKU 集合（``spu_id`` 不变，购物车与链接都不受影响）。

    ★ **只允许没有订单的商品**。判据在 trade 域，所以在这里先查再交给 product ——
      跨模块调用只能放在路由层（product 不能反向 import trade）。
      在售 / 审核中的商品由 service 再挡一道（与 ``delete_spu`` 同口径）。
    """
    if await trade_service.spu_has_order_items(session, spu_id):
        raise BizError(ErrorCode.VALIDATION_ERROR, "该商品已有订单，规格不能再改")
    return ApiResponse.ok(await service.replace_specs(session, shop_id, spu_id, body))


@router.put("/api/merchant/spus/{spu_id}", response_model=ApiResponse[SpuDetailOut], summary="编辑商品")
async def update_spu(
    spu_id: int, body: SpuUpdateRequest, shop_id: CurrentShopIdDep, session: DbSession
) -> ApiResponse[SpuDetailOut]:
    """只能改标题、副标题、主图、类目、排序权重。

    规格与 SKU 不在这里改 —— 它们走 ``PUT /merchant/spus/{id}/specs`` 整体替换，
    且只在该商品**没有订单**时允许（见 ``replace_spu_specs``）。
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
    # 归属校验就落在 sku_ids_of_owned_spu 里（拿别人店铺的 spu_id 会直接 404）——
    # 不先过它的话，运费检查会抢在 404 前面报出"这个商品有几个规格"
    sku_ids = await service.sku_ids_of_owned_spu(session, shop_id, spu_id)
    await _assert_freight_ready(session, shop_id=shop_id, sku_ids=sku_ids)
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
    # ★ 上界跟着 SPU_REJECTED 走，别写死数字 —— 加了状态却忘了改这里，
    #   商家那个筛选 tab 会静默 422（前端拿到错误什么都不显示，最难查的一种坏法）。
    status: int | None = Query(default=None, ge=1, le=SPU_REJECTED),
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


@router.get(
    "/api/admin/spus/{spu_id}",
    response_model=ApiResponse[SpuDetailOut],
    summary="商品详情（平台）",
)
async def get_admin_spu(admin: AdminDep, session: DbSession, spu_id: int) -> ApiResponse[SpuDetailOut]:
    """平台视角的商品详情：**任何店铺、任何状态**都看得到。

    ★ 补这个接口是因为审核页原来只有一个缩略图和标题，**看不见商品本身就要点"通过"**。
      既有的两条路径都不通：``/api/merchant/spus/{id}`` 按店铺归属判权（运营没有店铺，必 403），
      公开的 ``/api/spus/{id}`` 只出已上架的 —— 而审核队列里全是**待审核**的。
    """
    return ApiResponse.ok(await service.get_spu_detail(session, spu_id, as_platform=True))


@router.post("/api/admin/spus/{spu_id}/audit", response_model=ApiResponse[None], summary="商品审核")
async def audit_spu(
    spu_id: int, body: SpuAuditRequest, admin: AdminDep, session: DbSession
) -> ApiResponse[None]:
    """通过 → 上架；驳回 → 退回草稿。``remark`` 会存进 ``audit_remark`` 给商家看。"""
    if body.approved:
        # ★ 通过就是**直接上架**，所以要和商家的「上架」过同一道运费检查 ——
        #   只在商家那条路上挡，平台审核就成了绕过它的后门（商品照样卖不出去，
        #   只是没人知道，直到买家点结算）。
        brief = await service.get_spu_brief(session, spu_id)
        if brief is not None:
            target_shop, sku_ids = brief
            await _assert_freight_ready(session, shop_id=target_shop, sku_ids=sku_ids)
    await service.audit_spu(session, spu_id, body)
    return ApiResponse.ok(None)
