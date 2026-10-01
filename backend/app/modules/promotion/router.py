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
from app.modules.promotion import checkout, service
from app.modules.promotion import repository as repo
from app.modules.promotion.models import (
    ACTIVITY_STATUS_TEXT,
    CALC_TYPE_TEXT,
    COUPON_TPL_ONGOING,
    COUPON_TPL_STATUS_TEXT,
    COUPON_TYPE_TEXT,
    DISCOUNT_TYPE_TEXT,
    LEVEL_TEXT,
    VALID_DAYS_AFTER,
    VALID_FIXED,
    CouponTemplate,
    PromoActivity,
)
from app.modules.promotion.schemas import (
    AdminCouponTemplateListOut,
    AdminCouponTemplateOut,
    AdminIssueRequest,
    AdminPromoActivityListOut,
    AdminPromoActivityOut,
    CalcPriceOut,
    CalcPriceRequest,
    CouponReceiveOut,
    CouponTemplateCreateRequest,
    CouponTemplateOut,
    MyCouponOut,
    PromoActivityCreateRequest,
    ReceivableCouponOut,
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


@router.post(
    "/api/admin/coupons/issue",
    response_model=ApiResponse[list[CouponReceiveOut]],
    summary="客服补发（不占活动额度）",
)
async def admin_issue(
    session: DbSession, body: AdminIssueRequest, admin: AdminDep
) -> ApiResponse[list[CouponReceiveOut]]:
    """手工补发。**不消耗活动库存**（``issued_count`` 不变）——
    补发是平台欠用户的，不该挤占其他用户的名额（docs/04 §11）。"""
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
