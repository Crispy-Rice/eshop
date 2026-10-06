"""freight 模块的 HTTP 路由。

商家端维护模板；买家端估运费。
事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUserDep, DbSession
from app.core.errors import BizError, ErrorCode
from app.core.response import ApiResponse
from app.modules.account import service as account_service
from app.modules.account.deps import CurrentShopIdDep
from app.modules.freight import repository as repo
from app.modules.freight import service
from app.modules.freight.models import FreightTemplate
from app.modules.freight.schemas import (
    ExcludeRegionOut,
    ExcludeRegionsReplaceRequest,
    FreightEstimateOut,
    FreightEstimateRequest,
    FreightPackageOut,
    FreightTemplateCreateRequest,
    FreightTemplateOut,
    FreightTemplateUpdateOut,
    FreightTemplateUpdateRequest,
    RegionRuleOut,
    RegionRulesReplaceRequest,
    SkuBindOut,
    SkuBindRequest,
)
from app.modules.inventory import service as inventory_service
from app.modules.product import service as product_service

router = APIRouter()


async def _to_template_out(
    session: AsyncSession,
    tpl: FreightTemplate,
    *,
    with_count: bool = True,
    deleted_sku_ids: list[int] | None = None,
) -> FreightTemplateOut:
    """``deleted_sku_ids`` 传已软删商品的 SKU，让"已绑定 N 个 SKU"跟抽屉里
    能看到的明细对得上。调用方**一次请求取一遍**再传进来，别在这里查 ——
    这个函数在列表里是按模板循环调用的。"""
    bound = (
        await repo.count_binds_of_template(session, tpl.id, exclude_sku_ids=deleted_sku_ids or ())
        if with_count
        else 0
    )
    return FreightTemplateOut(
        id=tpl.id,
        name=tpl.name,
        charge_type=tpl.charge_type,
        first_unit=tpl.first_unit,
        first_price=tpl.first_price,
        add_unit=tpl.add_unit,
        add_price=tpl.add_price,
        free_shipping=tpl.free_shipping,
        free_threshold=tpl.free_threshold,
        free_num=tpl.free_num,
        merge_type=tpl.merge_type,
        status=tpl.status,
        is_default=tpl.is_default,
        bound_sku_count=bound,
    )


# ============================================================
# 商家：模板
# ============================================================
@router.get(
    "/api/merchant/freight/templates",
    response_model=ApiResponse[list[FreightTemplateOut]],
    summary="运费模板列表",
)
async def list_templates(
    session: DbSession, shop_id: CurrentShopIdDep
) -> ApiResponse[list[FreightTemplateOut]]:
    rows = await service.list_templates(session, shop_id)
    # 已软删商品的 SKU 只取一次，循环里复用
    deleted = await product_service.list_deleted_sku_ids(session, shop_id)
    return ApiResponse.ok(
        [await _to_template_out(session, t, deleted_sku_ids=deleted) for t in rows]
    )


@router.post(
    "/api/merchant/freight/templates",
    response_model=ApiResponse[FreightTemplateOut],
    summary="新建运费模板",
)
async def create_template(
    session: DbSession, body: FreightTemplateCreateRequest, shop_id: CurrentShopIdDep
) -> ApiResponse[FreightTemplateOut]:
    tpl = await service.create_template(session, shop_id, body)
    return ApiResponse.ok(await _to_template_out(session, tpl, with_count=False))


@router.get(
    "/api/merchant/freight/templates/{template_id}",
    response_model=ApiResponse[FreightTemplateOut],
    summary="模板详情",
)
async def get_template(
    session: DbSession, template_id: int, shop_id: CurrentShopIdDep
) -> ApiResponse[FreightTemplateOut]:
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")
    deleted = await product_service.list_deleted_sku_ids(session, shop_id)
    return ApiResponse.ok(await _to_template_out(session, tpl, deleted_sku_ids=deleted))


@router.put(
    "/api/merchant/freight/templates/{template_id}",
    response_model=ApiResponse[FreightTemplateUpdateOut],
    summary="修改模板（返回影响面提示）",
)
async def update_template(
    session: DbSession,
    template_id: int,
    body: FreightTemplateUpdateRequest,
    shop_id: CurrentShopIdDep,
) -> ApiResponse[FreightTemplateUpdateOut]:
    """改模板。

    响应里带上**受影响的 SKU 数**和提示 —— 商家需要知道这一改会波及多少商品，
    以及"已下单的订单不受影响"（它们存的是运费快照）。
    """
    tpl, affected = await service.update_template(session, shop_id, template_id, body)
    deleted = await product_service.list_deleted_sku_ids(session, shop_id)
    return ApiResponse.ok(
        FreightTemplateUpdateOut(
            template=await _to_template_out(session, tpl, deleted_sku_ids=deleted),
            affected_sku_count=affected,
            notice=(
                f"已绑定 {affected} 个 SKU，修改后立即对**新建**订单生效；"
                "已下单订单的运费按下单时的快照，不受影响"
            ),
        )
    )


@router.put(
    "/api/merchant/freight/templates/{template_id}/default",
    response_model=ApiResponse[FreightTemplateOut],
    summary="设为店铺默认模板",
)
async def set_default_template(
    session: DbSession, template_id: int, shop_id: CurrentShopIdDep
) -> ApiResponse[FreightTemplateOut]:
    """把这条模板设为店铺默认。

    ★ 默认模板是"没绑定运费模板的 SKU 的兜底"：绑定是逐条 SKU 的（``sku_freight_bind``），
      新发布的商品天然在模板之外 —— 没有兜底的话，漏配这件事只有**买家在结算页**
      才会撞上「还没有绑定运费模板」，商家那边毫无感知。一个店只有一条，设新的顶掉旧的。
    """
    tpl = await service.set_default_template(session, shop_id, template_id)
    deleted = await product_service.list_deleted_sku_ids(session, shop_id)
    return ApiResponse.ok(await _to_template_out(session, tpl, deleted_sku_ids=deleted))


# ============================================================
# 商家：区域规则与不发货区域
# ============================================================
@router.get(
    "/api/merchant/freight/templates/{template_id}/regions",
    response_model=ApiResponse[list[RegionRuleOut]],
    summary="区域规则",
)
async def list_regions(
    session: DbSession, template_id: int, shop_id: CurrentShopIdDep
) -> ApiResponse[list[RegionRuleOut]]:
    rows = await service.list_region_rules(session, shop_id, template_id)
    return ApiResponse.ok(
        [
            RegionRuleOut(
                id=r.id,
                region_code=r.region_code,
                region_level=r.region_level,
                first_unit=r.first_unit,
                first_price=r.first_price,
                add_unit=r.add_unit,
                add_price=r.add_price,
                free_shipping=r.free_shipping,
                enabled=r.enabled,
                priority=r.priority,
            )
            for r in rows
        ]
    )


@router.put(
    "/api/merchant/freight/templates/{template_id}/regions",
    response_model=ApiResponse[list[RegionRuleOut]],
    summary="整体替换区域规则",
)
async def replace_regions(
    session: DbSession,
    template_id: int,
    body: RegionRulesReplaceRequest,
    shop_id: CurrentShopIdDep,
) -> ApiResponse[list[RegionRuleOut]]:
    await service.replace_region_rules(session, shop_id, template_id, body.rules)
    return await list_regions(session, template_id, shop_id)


@router.get(
    "/api/merchant/freight/templates/{template_id}/exclude",
    response_model=ApiResponse[list[ExcludeRegionOut]],
    summary="不发货区域",
)
async def list_excludes(
    session: DbSession, template_id: int, shop_id: CurrentShopIdDep
) -> ApiResponse[list[ExcludeRegionOut]]:
    rows = await service.list_exclude_regions(session, shop_id, template_id)
    return ApiResponse.ok(
        [ExcludeRegionOut(id=e.id, region_code=e.region_code, reason=e.reason) for e in rows]
    )


@router.put(
    "/api/merchant/freight/templates/{template_id}/exclude",
    response_model=ApiResponse[list[ExcludeRegionOut]],
    summary="整体替换不发货区域",
)
async def replace_excludes(
    session: DbSession,
    template_id: int,
    body: ExcludeRegionsReplaceRequest,
    shop_id: CurrentShopIdDep,
) -> ApiResponse[list[ExcludeRegionOut]]:
    await service.replace_exclude_regions(session, shop_id, template_id, body.excludes)
    return await list_excludes(session, template_id, shop_id)


@router.get(
    "/api/merchant/freight/templates/{template_id}/binds",
    response_model=ApiResponse[list[SkuBindOut]],
    summary="模板绑定了哪些 SKU",
)
async def list_template_binds(
    session: DbSession, template_id: int, shop_id: CurrentShopIdDep
) -> ApiResponse[list[SkuBindOut]]:
    binds = await service.list_binds(session, shop_id, template_id)
    if not binds:
        return ApiResponse.ok([])

    # 批量拼显示名，避免 N+1
    sku_ids = [int(b.sku_id) for b in binds]
    skus = {
        int(s.id): s
        for s in await product_service.batch_get_skus(session, sku_ids, only_on_shelf=False)
    }
    warehouses = {
        int(w.id): w.name for w in await inventory_service.list_warehouses(session, shop_id)
    }

    rows: list[SkuBindOut] = []
    for b in binds:
        sku = skus.get(int(b.sku_id))
        if sku is None:
            # 商品已软删。绑定行还在 ``freight.sku_freight_bind`` 里（软删不动
            # 别的 schema），但对商家已经没有任何意义 —— 那个商品连下单都下不了。
            # 排掉，让这个数字跟"已绑定 N 个 SKU"对得上。
            continue
        rows.append(
            SkuBindOut(
                sku_id=b.sku_id,
                sku_code=sku.sku_code or "",
                spu_title=sku.title,
                spec_text=sku.spec_text,
                warehouse_id=b.warehouse_id,
                warehouse_name=warehouses.get(int(b.warehouse_id), ""),
                priority=b.priority,
                enabled=b.enabled,
            )
        )
    return ApiResponse.ok(rows)


@router.post(
    "/api/merchant/freight/bind",
    response_model=ApiResponse[None],
    summary="把 SKU 绑到运费模板",
)
async def bind_sku(
    session: DbSession, body: SkuBindRequest, shop_id: CurrentShopIdDep
) -> ApiResponse[None]:
    await service.bind_sku(
        session,
        shop_id,
        sku_id=int(body.sku_id),
        template_id=int(body.template_id),
        warehouse_id=int(body.warehouse_id),
        priority=body.priority,
    )
    return ApiResponse.ok(None)


# ============================================================
# 买家：估运费
# ============================================================
@router.post(
    "/api/checkout/freight",
    response_model=ApiResponse[FreightEstimateOut],
    summary="估运费（购物车页展示预计运费用）",
)
async def estimate_freight(
    session: DbSession, body: FreightEstimateRequest, user: CurrentUserDep
) -> ApiResponse[FreightEstimateOut]:
    """单独估运费，不跑完整的算价。

    购物车页要展示"预计运费"（docs/06 §4.1 强调：续重的取整规则必须提前告知用户，
    否则到支付才发现运费比预期高），但那时用户还没进结算页、可能也没选券，
    跑完整算价是浪费。
    """
    address = await account_service.get_address_for_order(
        session, user.id, int(body.address_id)
    )
    sku_ids = [int(i.sku_id) for i in body.items]
    skus = {
        int(s.id): s
        for s in await product_service.batch_get_skus(session, sku_ids, only_on_shelf=False)
    }
    # ★ 与算价/下单**同一套择仓**（规则优先、缺货按候选链兜底，见 inventory.routing）
    #   —— 三处必须给出同一个仓，不然购物车预估的运费会和结算页/下单时的对不上。
    #   （运费本身不随仓变：模板按 SKU、包裹按仓、一店一仓。）
    choices = await inventory_service.route_warehouses(
        session,
        region_code=address.region_code,
        sku_to_shop={sid: int(sku.shop_id) for sid, sku in skus.items()},
        need={int(i.sku_id): int(i.num) for i in body.items},
    )

    def line_of(sku_id: str, num: int) -> service.FreightLine:
        sku = skus.get(int(sku_id))
        return service.FreightLine(
            sku_id=int(sku_id),
            num=num,
            weight_g=sku.weight_g if sku else 0,
            amount=sku.price * num if sku else 0,
            shop_id=int(sku.shop_id) if sku else 0,
            title=sku.title if sku else "",
        )

    result = await service.estimate(
        session,
        lines=[line_of(i.sku_id, i.num) for i in body.items],
        region_code=address.region_code,
        warehouses=inventory_service.warehouses_of(choices),
    )
    return ApiResponse.ok(
        FreightEstimateOut(
            total=result.total,
            packages=[
                FreightPackageOut(
                    warehouse_id=p.warehouse_id,
                    template_name=p.template_name,
                    freight=p.freight,
                    weight_g=p.weight_g,
                    qty=p.qty,
                    is_free=p.is_free,
                    free_reason=p.free_reason,
                )
                for p in result.packages
            ],
            notices=result.notices,
        )
    )
