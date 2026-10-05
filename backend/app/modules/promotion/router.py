"""promotion 模块的 HTTP 路由。

买家侧：券中心、领券、我的券、算价。
运营侧：建券模板、客服补发、建促销活动。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.context import CurrentUser
from app.core.deps import CurrentUserDep, DbSession, IdempotencyKeyDep, client_ip, require_role
from app.core.errors import BizError, ErrorCode
from app.core.response import ApiResponse
from app.core.snowflake import next_id
from app.modules.account import service as account_service
from app.modules.promotion import checkout, service
from app.modules.promotion import repository as repo
from app.modules.promotion.models import (
    ACTIVITY_STATUS_TEXT,
    BANNER_ENABLED,
    CALC_TYPE_TEXT,
    COUPON_TPL_ONGOING,
    COUPON_TPL_STATUS_TEXT,
    COUPON_TYPE_TEXT,
    DISCOUNT_TYPE_TEXT,
    LEVEL_TEXT,
    VALID_DAYS_AFTER,
    VALID_FIXED,
    Banner,
    CouponTemplate,
    PromoActivity,
)
from app.modules.promotion.schemas import (
    AdminCouponTemplateListOut,
    AdminCouponTemplateOut,
    AdminIssueRequest,
    AdminPromoActivityListOut,
    AdminPromoActivityOut,
    BannerCreateRequest,
    BannerOut,
    BannerUpdateRequest,
    CalcPriceOut,
    CalcPriceRequest,
    CouponReceiveOut,
    CouponTemplateCreateRequest,
    CouponTemplateOut,
    MyCouponOut,
    PromoActivityCreateRequest,
    ReceivableCouponOut,
    UserLookupOut,
)

router = APIRouter()

AdminDep = Annotated[CurrentUser, Depends(require_role("admin", "finance"))]


def _to_tpl_out(tpl: CouponTemplate) -> CouponTemplateOut:
    return CouponTemplateOut(
        id=tpl.id,
        shop_id=tpl.shop_id,
        name=tpl.name,
        type=tpl.type,
        discount_value=tpl.discount_value,
        max_discount=tpl.max_discount,
        threshold=tpl.threshold,
        per_user_limit=tpl.per_user_limit,
        valid_start=tpl.valid_start,
        valid_end=tpl.valid_end,
        valid_days=tpl.valid_days,
        scope_type=tpl.scope_type,
        status=tpl.status,
    )


def _scope_value_out(raw: list[Any] | None) -> list[str] | None:
    """scope_value 里是雪花 ID，出参必须转字符串。

    这些 ID 超过 2^53，序列化成 JSON number 会让前端（JS 只有 double）
    静默丢精度 —— 表现是"选中的商品 ID 变成另一个数"。
    """
    return [str(v) for v in raw] if raw else None


def _to_admin_tpl_out(tpl: CouponTemplate) -> AdminCouponTemplateOut:
    return AdminCouponTemplateOut(
        id=tpl.id,
        shop_id=tpl.shop_id,
        name=tpl.name,
        type=tpl.type,
        type_text=COUPON_TYPE_TEXT.get(tpl.type, "-"),
        get_type=tpl.get_type,
        discount_value=tpl.discount_value,
        max_discount=tpl.max_discount,
        threshold=tpl.threshold,
        total_count=tpl.total_count,
        issued_count=tpl.issued_count,
        used_count=tpl.used_count,
        per_user_limit=tpl.per_user_limit,
        valid_type=tpl.valid_type,
        valid_start=tpl.valid_start,
        valid_end=tpl.valid_end,
        valid_days=tpl.valid_days,
        scope_type=tpl.scope_type,
        scope_value=_scope_value_out(tpl.scope_value),
        status=tpl.status,
        status_text=COUPON_TPL_STATUS_TEXT.get(tpl.status, "-"),
        created_at=tpl.created_at,
    )


def _to_admin_activity_out(act: PromoActivity) -> AdminPromoActivityOut:
    return AdminPromoActivityOut(
        id=act.id,
        name=act.name,
        level=act.level,
        level_text=LEVEL_TEXT.get(act.level, "-"),
        type=act.type,
        # type 是叠加规则矩阵的键，正常都在映射表里；兜底成原值便于排查
        type_text=DISCOUNT_TYPE_TEXT.get(act.type, act.type),
        calc_type=act.calc_type,
        calc_type_text=CALC_TYPE_TEXT.get(act.calc_type, "-"),
        discount_value=act.discount_value,
        max_discount=act.max_discount,
        threshold=act.threshold,
        shop_id=act.shop_id,
        scope_type=act.scope_type,
        scope_value=_scope_value_out(act.scope_value),
        start_at=act.start_at,
        end_at=act.end_at,
        priority=act.priority,
        status=act.status,
        status_text=ACTIVITY_STATUS_TEXT.get(act.status, "-"),
        created_at=act.created_at,
    )


# ============================================================
# 买家：券
# ============================================================
@router.get(
    "/api/coupons/available",
    response_model=ApiResponse[list[ReceivableCouponOut]],
    summary="券中心（可领取的券）",
)
async def list_available_coupons(
    session: DbSession, user: CurrentUserDep
) -> ApiResponse[list[ReceivableCouponOut]]:
    rows = await service.list_receivable(session, user.id)
    return ApiResponse.ok(
        [
            ReceivableCouponOut(
                template=_to_tpl_out(r["template"]),
                received=r["received"],
                can_receive=r["can_receive"],
                remain=r["remain"],
            )
            for r in rows
        ]
    )


@router.post(
    "/api/coupons/{template_id}/receive",
    response_model=ApiResponse[CouponReceiveOut],
    summary="领券（需 Idempotency-Key）",
)
async def receive_coupon(
    session: DbSession,
    template_id: int,
    user: CurrentUserDep,
    idempotency_key: IdempotencyKeyDep,
    ip: Annotated[str | None, Depends(client_ip)] = None,
) -> ApiResponse[CouponReceiveOut]:
    """领取优惠券。

    ★ 必须带 ``Idempotency-Key``：它是"客户端超时重试"的最后一道防线。
    没有它的话，用户点一次、网络卡住、浏览器自动重试，就可能领到两张。
    """
    code = await service.receive(
        session,
        user_id=user.id,
        tpl_id=template_id,
        idem_key=idempotency_key,
        ip=ip,
    )
    tpl = await repo.get_template(session, template_id)
    return ApiResponse.ok(
        CouponReceiveOut(
            id=code.id,
            code=code.code,
            name=tpl.name if tpl else "优惠券",
            valid_start=code.valid_start,
            valid_end=code.valid_end,
        )
    )


@router.get("/api/my/coupons", response_model=ApiResponse[list[MyCouponOut]], summary="我的券")
async def list_my_coupons(
    session: DbSession,
    user: CurrentUserDep,
    # 1未使用 2已锁定 3已使用 4已过期。不传返回全部
    status: int | None = Query(default=None, ge=1, le=5),
) -> ApiResponse[list[MyCouponOut]]:
    rows = await service.list_my_coupons(session, user.id, status=status)
    out: list[MyCouponOut] = []
    for r in rows:
        c = r["code"]
        tpl = r["template"]
        out.append(
            MyCouponOut(
                id=c.id,
                code=c.code,
                status=c.status,
                status_text=r["status_text"],
                valid_start=c.valid_start,
                valid_end=c.valid_end,
                expired=r["expired"],
                name=tpl.name if tpl else "优惠券",
                threshold=tpl.threshold if tpl else 0,
                discount_value=tpl.discount_value if tpl else 0,
                max_discount=tpl.max_discount if tpl else 0,
                coupon_type=tpl.type if tpl else 1,
                shop_id=tpl.shop_id if tpl else 0,
            )
        )
    return ApiResponse.ok(out)


# ============================================================
# 买家：算价
# ============================================================
@router.post(
    "/api/checkout/calc",
    response_model=ApiResponse[CalcPriceOut],
    summary="算价（逐级优惠 + 分摊明细）",
)
async def calc_price(
    session: DbSession, body: CalcPriceRequest, user: CurrentUserDep
) -> ApiResponse[CalcPriceOut]:
    """结算页算价。

    返回每一行的原价、促销价、**分摊到该行的优惠**与实付 —— 分摊明细必须给，
    因为退款时按它算每行退多少（docs/05 §6.4）。不可用的券会带上原因。
    """
    return ApiResponse.ok(await checkout.calc_price(session, user.id, body))


# ============================================================
# 运营：券模板（列表 / 新建）、补发、活动（列表 / 新建）
# ============================================================
@router.get(
    "/api/admin/coupons/templates",
    response_model=ApiResponse[AdminCouponTemplateListOut],
    summary="券模板列表（运营）",
)
async def list_admin_templates(
    session: DbSession,
    _: AdminDep,
    # 查询参数必须显式写 alias，否则前端传 camelCase 会被静默忽略
    status: Annotated[int | None, Query(ge=1, le=4)] = None,
    cursor: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[AdminCouponTemplateListOut]:
    page = await service.list_admin_templates(
        session, status=status, cursor=cursor, limit=limit
    )
    return ApiResponse.ok(
        AdminCouponTemplateListOut(
            items=[_to_admin_tpl_out(t) for t in page["items"]],
            next_cursor=page["next_cursor"],
            has_more=page["has_more"],
        )
    )


@router.post(
    "/api/admin/coupons/templates",
    response_model=ApiResponse[CouponTemplateOut],
    summary="新建券模板",
)
async def create_template(
    session: DbSession, body: CouponTemplateCreateRequest, _: AdminDep
) -> ApiResponse[CouponTemplateOut]:
    if body.valid_type == VALID_FIXED and (body.valid_start is None or body.valid_end is None):
        raise BizError(ErrorCode.VALIDATION_ERROR, "固定有效期必须同时给 validStart 与 validEnd")
    if body.valid_type == VALID_DAYS_AFTER and body.valid_days is None:
        raise BizError(ErrorCode.VALIDATION_ERROR, "「领取后 N 天」必须给 validDays")
    if body.valid_end is not None and body.valid_start is not None and body.valid_end <= body.valid_start:
        raise BizError(ErrorCode.VALIDATION_ERROR, "validEnd 必须晚于 validStart")

    tpl = CouponTemplate(
        id=next_id(),
        shop_id=body.shop_id,
        name=body.name,
        type=body.type,
        # 运营建的券默认是「主动领取」，系统发放走补发接口
        get_type=1,
        discount_value=body.discount_value,
        max_discount=body.max_discount,
        threshold=body.threshold,
        total_count=body.total_count,
        per_user_limit=body.per_user_limit,
        valid_type=body.valid_type,
        valid_start=body.valid_start,
        valid_end=body.valid_end,
        valid_days=body.valid_days,
        scope_type=body.scope_type,
        scope_value=body.scope_value,
        # 建出来就是进行中；真要定时开始由 cron 改状态
        status=COUPON_TPL_ONGOING,
    )
    await repo.insert_template(session, tpl)
    return ApiResponse.ok(_to_tpl_out(tpl))


@router.get(
    "/api/admin/users/lookup",
    response_model=ApiResponse[UserLookupOut],
    summary="按手机号定位用户（定向发券用）",
)
async def lookup_user(
    admin: AdminDep,
    session: DbSession,
    phone: str = Query(min_length=11, max_length=11, description="11 位手机号"),
) -> ApiResponse[UserLookupOut]:
    """把运营手上的**手机号**换成一个 ``userId``，供定向发券使用。

    ★ 运营拿不到用户的雪花 ID —— 那个对话框原来要求手填 18 位数字，实际没人填得出来。
      "用户报手机号"才是真实场景，而 ``account`` 注册时就写好了可查的 ``phone_hash``。

    查不到就 404：宁可让运营回去核对号码，也不要猜一个 ID 发出去。
    """
    found = await account_service.find_user_by_phone(session, phone)
    if found is None:
        raise BizError(ErrorCode.NOT_FOUND, "该手机号没有对应的注册用户")
    user_id, nickname, phone_masked = found
    return ApiResponse.ok(
        UserLookupOut(user_id=user_id, nickname=nickname, phone_masked=phone_masked)
    )


@router.post(
    "/api/admin/coupons/issue",
    response_model=ApiResponse[list[CouponReceiveOut]],
    summary="客服补发（不占活动额度）",
)
async def admin_issue(
    session: DbSession, body: AdminIssueRequest, admin: AdminDep
) -> ApiResponse[list[CouponReceiveOut]]:
    """手工补发。**不消耗活动库存**（``issued_count`` 不变）——
    补发是平台欠用户的，不该挤占其他用户的名额（docs/04 §11）。

    ★ 先校验收件人存在。以前不校验 —— 运营手抄错一位数字，就会给一个不存在的
      用户静静地发券：券码进了库，但永远没人能领到，也没人会发现。
    """
    if not await account_service.user_exists(session, int(body.user_id)):
        raise BizError(ErrorCode.NOT_FOUND, "该用户不存在，请核对后重试")
    codes = await service.issue_by_admin(
        session,
        tpl_id=int(body.template_id),
        user_id=int(body.user_id),
        operator=f"admin:{admin.id}",
        count=body.count,
    )
    tpl = await repo.get_template(session, int(body.template_id))
    return ApiResponse.ok(
        [
            CouponReceiveOut(
                id=c.id,
                code=c.code,
                name=tpl.name if tpl else "优惠券",
                valid_start=c.valid_start,
                valid_end=c.valid_end,
            )
            for c in codes
        ]
    )


@router.get(
    "/api/admin/promotions",
    response_model=ApiResponse[AdminPromoActivityListOut],
    summary="促销活动列表（运营）",
)
async def list_admin_activities(
    session: DbSession,
    _: AdminDep,
    status: Annotated[int | None, Query(ge=1, le=4)] = None,
    cursor: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[AdminPromoActivityListOut]:
    page = await service.list_admin_activities(
        session, status=status, cursor=cursor, limit=limit
    )
    return ApiResponse.ok(
        AdminPromoActivityListOut(
            items=[_to_admin_activity_out(a) for a in page["items"]],
            next_cursor=page["next_cursor"],
            has_more=page["has_more"],
        )
    )


@router.post(
    "/api/admin/promotions",
    response_model=ApiResponse[dict],
    summary="新建促销活动",
)
async def create_activity(
    session: DbSession, body: PromoActivityCreateRequest, _: AdminDep
) -> ApiResponse[dict]:
    if body.end_at <= body.start_at:
        raise BizError(ErrorCode.VALIDATION_ERROR, "endAt 必须晚于 startAt")

    now = datetime.now(UTC)
    activity = PromoActivity(
        id=next_id(),
        name=body.name,
        level=body.level,
        type=body.type,
        calc_type=body.calc_type,
        discount_value=body.discount_value,
        max_discount=body.max_discount,
        threshold=body.threshold,
        shop_id=body.shop_id,
        scope_type=body.scope_type,
        scope_value=body.scope_value,
        start_at=body.start_at,
        end_at=body.end_at,
        status=2 if body.start_at <= now < body.end_at else 1,
        priority=body.priority,
    )
    await repo.insert_activity(session, activity)
    return ApiResponse.ok({"id": str(activity.id), "name": activity.name})


# ============================================================
# 首页 Banner
#
# 买家侧只读启用中的；运营侧（admin / finance，与营销同角色）可增删改。
# 写操作直接落在这一层 —— promotion 模块的运营 CRUD 一贯是"路由 + repository"，
# 中间没有 service 中转，这里保持一致。
# ============================================================
def _to_banner_out(banner: Banner) -> BannerOut:
    return BannerOut(
        id=banner.id,
        title=banner.title,
        image=banner.image,
        link_url=banner.link_url,
        sort=banner.sort,
        status=banner.status,
    )


@router.get("/api/banners", response_model=ApiResponse[list[BannerOut]], summary="首页轮播图")
async def list_banners(session: DbSession) -> ApiResponse[list[BannerOut]]:
    """**公开接口**：只返回启用中的，按 sort、id 升序；没配就是空数组。"""
    rows = await repo.list_banners(session, only_enabled=True)
    return ApiResponse.ok([_to_banner_out(b) for b in rows])


@router.get(
    "/api/admin/banners",
    response_model=ApiResponse[list[BannerOut]],
    summary="轮播图列表（含停用）",
)
async def list_admin_banners(session: DbSession, _: AdminDep) -> ApiResponse[list[BannerOut]]:
    """管理端要能看到停用过的 —— 否则停用一张图就再也找不回来了。"""
    rows = await repo.list_banners(session)
    return ApiResponse.ok([_to_banner_out(b) for b in rows])


@router.post("/api/admin/banners", response_model=ApiResponse[BannerOut], summary="新增轮播图")
async def create_banner(
    session: DbSession, body: BannerCreateRequest, _: AdminDep
) -> ApiResponse[BannerOut]:
    banner = Banner(
        id=next_id(),
        title=body.title,
        image=body.image,
        # 空串统一存成 NULL："没配链接"只有一种表示
        link_url=body.link_url or None,
        sort=body.sort,
        status=BANNER_ENABLED,
    )
    await repo.insert_banner(session, banner)
    return ApiResponse.ok(_to_banner_out(banner))


@router.put(
    "/api/admin/banners/{banner_id}",
    response_model=ApiResponse[BannerOut],
    summary="修改轮播图",
)
async def update_banner(
    session: DbSession, banner_id: int, body: BannerUpdateRequest, _: AdminDep
) -> ApiResponse[BannerOut]:
    banner = await repo.get_banner(session, banner_id)
    if banner is None:
        raise BizError(ErrorCode.NOT_FOUND, "轮播图不存在")

    values: dict[str, Any] = {}
    if body.title is not None:
        values["title"] = body.title
    if body.image is not None:
        values["image"] = body.image
    if body.link_url is not None:
        # 空串 = 清空链接。None 表示"不改"，两者语义不同，不能合
        values["link_url"] = body.link_url or None
    if body.sort is not None:
        values["sort"] = body.sort
    if body.status is not None:
        values["status"] = body.status

    await repo.update_banner_fields(session, banner_id, values)
    # 上面走的是批量 UPDATE，identity map 里的对象要显式刷一下才拿到新值
    await session.refresh(banner)
    return ApiResponse.ok(_to_banner_out(banner))


@router.delete(
    "/api/admin/banners/{banner_id}", response_model=ApiResponse[None], summary="删除轮播图"
)
async def delete_banner(session: DbSession, banner_id: int, _: AdminDep) -> ApiResponse[None]:
    if await repo.get_banner(session, banner_id) is None:
        raise BizError(ErrorCode.NOT_FOUND, "轮播图不存在")
    await repo.delete_banner(session, banner_id)
    return ApiResponse.ok(None)
