"""account 模块的 HTTP 路由。

事务由 ``get_session`` 依赖统一管理（一个请求一个事务），路由里不再写
``async with session.begin()`` —— 详见 app/core/db.py 的说明。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import CurrentUser
from app.core.deps import CurrentUserDep, DbSession, client_ip, require_role
from app.core.errors import BizError, ErrorCode
from app.core.response import ApiResponse
from app.modules.account import service
from app.modules.account.schemas import (
    AddressIn,
    AddressOut,
    AddressUpdate,
    AdminUserDetailOut,
    AdminUserListOut,
    BanUserRequest,
    ChangePasswordRequest,
    CloseAccountRequest,
    DeactivationCheckOut,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ShopCreateRequest,
    ShopOut,
    ShopUpdateRequest,
    TempPasswordOut,
    TokenResponse,
    UpdateProfileRequest,
    UserOut,
)
from app.modules.aftersale import service as aftersale_service
from app.modules.freight import service as freight_service
from app.modules.trade import service as trade_service

router = APIRouter()

# 平台运营。**不含 finance** —— 财务能看营销与轮播图，但不该能封人或重置密码
# （与前端 App.vue 的 isPlatformAdmin 判据一致）。
# 注意：/api/admin/users/lookup 在 promotion 那边对 admin+finance 开放，
# 那条是"按手机号查人发券"，与这里的账号处置是两回事。
AdminDep = Annotated[CurrentUser, Depends(require_role("admin"))]


# ============================================================
# 认证（公开接口）
# ============================================================
@router.post("/api/auth/register", response_model=ApiResponse[TokenResponse], summary="注册")
async def register(
    body: RegisterRequest,
    request: Request,
    session: DbSession,
    user_agent: str | None = Header(default=None),
) -> ApiResponse[TokenResponse]:
    tokens = await service.register(
        session,
        phone=body.phone,
        password=body.password,
        nickname=body.nickname,
        ip=client_ip(request),
        user_agent=user_agent,
    )
    return ApiResponse.ok(tokens)


@router.post("/api/auth/login", response_model=ApiResponse[TokenResponse], summary="登录")
async def login(
    body: LoginRequest,
    request: Request,
    session: DbSession,
    user_agent: str | None = Header(default=None),
) -> ApiResponse[TokenResponse]:
    tokens = await service.login(
        session,
        phone=body.phone,
        password=body.password,
        ip=client_ip(request),
        user_agent=user_agent,
    )
    return ApiResponse.ok(tokens)


@router.post("/api/auth/refresh", response_model=ApiResponse[TokenResponse], summary="刷新令牌")
async def refresh(
    body: RefreshRequest,
    request: Request,
    session: DbSession,
    user_agent: str | None = Header(default=None),
) -> ApiResponse[TokenResponse]:
    tokens = await service.refresh(
        session,
        refresh_token=body.refresh_token,
        ip=client_ip(request),
        user_agent=user_agent,
    )
    return ApiResponse.ok(tokens)


@router.post("/api/auth/logout", response_model=ApiResponse[None], summary="登出")
async def logout(body: RefreshRequest, session: DbSession) -> ApiResponse[None]:
    await service.logout(session, refresh_token=body.refresh_token)
    return ApiResponse.ok(None)


# ============================================================
# 当前用户
# ============================================================
@router.get("/api/me", response_model=ApiResponse[UserOut], summary="当前用户信息")
async def get_me(user: CurrentUserDep, session: DbSession) -> ApiResponse[UserOut]:
    return ApiResponse.ok(await service.get_me(session, user.id))


@router.put("/api/me", response_model=ApiResponse[UserOut], summary="修改个人资料")
async def update_me(
    body: UpdateProfileRequest, user: CurrentUserDep, session: DbSession
) -> ApiResponse[UserOut]:
    return ApiResponse.ok(await service.update_me(session, user.id, body))


# ============================================================
# 收货地址
# ============================================================
@router.get("/api/me/addresses", response_model=ApiResponse[list[AddressOut]], summary="地址列表")
async def list_addresses(user: CurrentUserDep, session: DbSession) -> ApiResponse[list[AddressOut]]:
    return ApiResponse.ok(await service.list_addresses(session, user.id))


@router.post("/api/me/addresses", response_model=ApiResponse[AddressOut], summary="新增地址")
async def create_address(
    body: AddressIn, user: CurrentUserDep, session: DbSession
) -> ApiResponse[AddressOut]:
    return ApiResponse.ok(await service.create_address(session, user.id, body))


@router.put("/api/me/addresses/{address_id}", response_model=ApiResponse[AddressOut], summary="修改地址")
async def update_address(
    address_id: int, body: AddressUpdate, user: CurrentUserDep, session: DbSession
) -> ApiResponse[AddressOut]:
    return ApiResponse.ok(await service.update_address(session, user.id, address_id, body))


@router.delete("/api/me/addresses/{address_id}", response_model=ApiResponse[None], summary="删除地址")
async def delete_address(address_id: int, user: CurrentUserDep, session: DbSession) -> ApiResponse[None]:
    await service.delete_address(session, user.id, address_id)
    return ApiResponse.ok(None)


# ============================================================
# 店铺（公开只读）
# ============================================================
@router.get("/api/shops", response_model=ApiResponse[list[ShopOut]], summary="批量取店铺信息")
async def list_shops(
    session: DbSession,
    ids: list[int] = Query(default=[], max_length=100, description="要查询的店铺 ID"),
) -> ApiResponse[list[ShopOut]]:
    """商品列表页一次显示 N 个商品，逐个请求就是 N+1，所以给一个批量的。

    参数字形是**重复参数**（``?ids=1&ids=2``）。axios 默认会序列化成带方括号的
    ``ids[]=1``，FastAPI 不认 —— 前端那边配 ``paramsSerializer: { indexes: null }``
    即可，比两边各自解析逗号分隔的字符串干净。
    """
    return ApiResponse.ok(await service.list_public_shops(session, ids))


@router.get("/api/shops/{shop_id}", response_model=ApiResponse[ShopOut], summary="店铺公开信息")
async def get_shop(shop_id: int, session: DbSession) -> ApiResponse[ShopOut]:
    """不需要登录：商品详情页要显示"这件商品是哪家店的"。

    和下面的 /api/merchant/shop 分开 —— 那条是"我的店铺"，要登录且只返回自己的。
    """
    return ApiResponse.ok(await service.get_public_shop(session, shop_id))


# ============================================================
# 店铺（商家端）
# ============================================================
@router.post("/api/merchant/shop", response_model=ApiResponse[ShopOut], summary="开店")
async def create_shop(
    body: ShopCreateRequest, user: CurrentUserDep, session: DbSession
) -> ApiResponse[ShopOut]:
    """开店，并**顺手送一条默认运费模板**。

    ★ 没有那条模板，新店的商品一件都上不了架（上架要求每个规格都算得出运费），
      而商家要绕到"发布 → 提交审核 → 平台点通过被判 400"才会发现 —— 卡在运营
      那一步，运营只能打回去，白跑一趟。
    ★ 模板参数只是**起点**（首重 ¥10 / 续重 ¥3 / 满 ¥99 包邮），商家该按真实
      运费改；它名字叫「默认快递模板」、列表里也标着「默认」，一眼看得出是系统给的。
    ★ 跨模块调用放路由层：freight 那边调 account 也是同一个套路
      （见 ``freight/router.py``）。
    """
    shop = await service.create_shop(session, user.id, body)
    await freight_service.ensure_default_template(session, int(shop.id))
    return ApiResponse.ok(shop)


@router.get("/api/merchant/shop", response_model=ApiResponse[ShopOut], summary="我的店铺")
async def get_my_shop(user: CurrentUserDep, session: DbSession) -> ApiResponse[ShopOut]:
    return ApiResponse.ok(await service.get_my_shop(session, user.id))


@router.put("/api/merchant/shop", response_model=ApiResponse[ShopOut], summary="修改店铺设置")
async def update_my_shop(
    body: ShopUpdateRequest, user: CurrentUserDep, session: DbSession
) -> ApiResponse[ShopOut]:
    """改名 / 换 LOGO / 改简介，部分更新。

    用 ``CurrentUserDep`` 而不是 ``CurrentShopIdDep``：归属是「我的店」这一层语义，
    由 service 按 owner_user_id 查库确定，取不到就是没开店（404）。
    """
    return ApiResponse.ok(await service.update_my_shop(session, user.id, body))


# ============================================================
# 账号安全（自助）
# ============================================================
@router.put("/api/me/password", response_model=ApiResponse[TokenResponse], summary="修改密码")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: CurrentUserDep,
    session: DbSession,
    user_agent: str | None = Header(default=None),
) -> ApiResponse[TokenResponse]:
    """改密码。返回**新的一对令牌**。

    ★ 其它设备的 refresh token 会被全部吊销（改密的动机常常就是"怀疑别人在用
      我的号"，留一条旧会话等于没改），当前这台补发一对，用户不用重新登录。
    """
    tokens = await service.change_password(
        session,
        user.id,
        old_password=body.old_password,
        new_password=body.new_password,
        ip=client_ip(request),
        user_agent=user_agent,
    )
    return ApiResponse.ok(tokens)


async def _deactivation_blockers(session: AsyncSession, user_id: int) -> tuple[bool, int, int]:
    """注销的三条前置守卫，返回 ``(名下有店铺, 未完成订单数, 进行中售后数)``。

    ★ **预检与真正注销共用这一份** —— 两处各写一遍必然漂，而漂的后果是
      "预检说能注销、提交时却被拒"，对用户来说比没有预检更糟。

    ★ 为什么守卫在**路由层**：要查 trade / aftersale，而 account **不能** import
      它们的 service（trade → … → product、aftersale → trade，反向即环，
      docs/01 §2）。所以由下游暴露只读查询、这里来编排 —— 与 create_shop 调
      freight 是同一个套路。
    """
    has_shop = await service.get_shop_id(session, user_id) is not None
    orders = await trade_service.count_blocking_orders(session, user_id)
    refunds = await aftersale_service.count_open_refunds(session, user_id)
    return has_shop, orders, refunds


@router.get(
    "/api/me/deactivation",
    response_model=ApiResponse[DeactivationCheckOut],
    summary="注销前置检查",
)
async def deactivation_check(
    user: CurrentUserDep, session: DbSession
) -> ApiResponse[DeactivationCheckOut]:
    """现在能不能注销、以及**被什么挡着**。

    ★ 它存在的唯一理由是**别让用户白输一次密码**：原先只有提交之后才知道
      "你有未完成订单"，而那时密码已经输过了，提示还是个转瞬即逝的 toast、
      也不说去哪里处理。前端现在先问这里，被挡就弹一个能点去处理的对话框。
    """
    has_shop, orders, refunds = await _deactivation_blockers(session, user.id)
    return ApiResponse.ok(
        DeactivationCheckOut(
            can_deactivate=not (has_shop or orders or refunds),
            has_shop=has_shop,
            order_count=orders,
            refund_count=refunds,
        )
    )


@router.post("/api/me/deactivate", response_model=ApiResponse[None], summary="注销账号")
async def close_account(
    body: CloseAccountRequest, user: CurrentUserDep, session: DbSession
) -> ApiResponse[None]:
    """注销账号。**不可恢复**：手机号、昵称、地址都会匿名化。

    ★ 用 POST + 动作名而不是 ``DELETE /api/me``：注销是**状态变更**（账号行还在，
      只是被匿名化），不是删资源；而且它要带密码，**DELETE 带 body 的语义在
      RFC 里没有定义**，httpx 的 ``client.delete()`` 干脆就不收 ``json=``，
      中间层也可能把 body 丢掉 —— 那会让服务端收到一个"空密码"的请求。

    ★ 三条前置守卫写在**路由层**，因为它们要查别的模块：

    - 名下有店铺 → 拒绝（没有"停用店铺"机制，得先自行处理）
    - 有没走完的订单（待付款 / 待发货 / 待收货 / 退款中）→ 拒绝
    - 有进行中的售后 → 拒绝

    ★ 为什么守卫在这儿：account **不能** import trade / aftersale 的 service
      （trade → … → product、aftersale → trade，反向即环，docs/01 §2）。
      所以由下游暴露只读查询、这里来编排 —— 与上面 create_shop 调 freight
      是同一个套路。

    ★ 要求输密码：没有短信验证码，这是唯一能证明"是本人"的二次确认。
    """
    has_shop, orders, refunds = await _deactivation_blockers(session, user.id)
    if has_shop:
        raise BizError(
            ErrorCode.SHOP_OWNER_CANNOT_DEACTIVATE,
            "你名下有店铺，请先处理店铺（下架商品、结清订单）后再注销账号",
        )
    if orders or refunds:
        parts = []
        if orders:
            parts.append(f"{orders} 笔未完成订单")
        if refunds:
            parts.append(f"{refunds} 笔进行中的售后")
        raise BizError(
            ErrorCode.ACCOUNT_HAS_UNFINISHED, f"你还有{'、'.join(parts)}，处理完才能注销"
        )

    await service.close_account(session, user.id, password=body.password)
    return ApiResponse.ok(None)


# ============================================================
# 平台运营：用户管理
# ============================================================
@router.get("/api/admin/users", response_model=ApiResponse[AdminUserListOut], summary="用户列表")
async def admin_list_users(
    session: DbSession,
    _admin: AdminDep,
    status: int | None = Query(default=None, description="1正常 2已封禁 3已注销"),
    keyword: str | None = Query(
        default=None, max_length=64, description="手机号（全匹配）或昵称（模糊）"
    ),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> ApiResponse[AdminUserListOut]:
    """运营的用户列表。关键词两种走法见 ``service.admin_list_users``。"""
    return ApiResponse.ok(
        await service.admin_list_users(
            session, status=status, keyword=keyword, cursor=cursor, limit=limit
        )
    )


@router.get(
    # ★ 路径里的 `:int` 转换器**不能省**：`{user_id}` 会匹配任意单段路径，
    #   把 promotion 那条 GET /api/admin/users/lookup 抢过来 —— "lookup"
    #   被当成 user_id 解析成整数，运营按手机号查人的接口会直接 400。
    #   加了转换器之后这条只匹配数字，`/users/lookup` 才能落到它自己的路由上。
    #   将来再加 `/api/admin/users/<非数字段>` 时，同样要注意这个陷阱。
    "/api/admin/users/{user_id:int}",
    response_model=ApiResponse[AdminUserDetailOut],
    summary="用户详情",
)
async def admin_get_user(
    user_id: int, session: DbSession, _admin: AdminDep
) -> ApiResponse[AdminUserDetailOut]:
    """用户详情。手机号**只回掩码**，后台刻意不提供解密查看（与其他运营接口同口径）。"""
    return ApiResponse.ok(await service.admin_get_user(session, user_id))


@router.post("/api/admin/users/{user_id}/ban", response_model=ApiResponse[None], summary="封禁用户")
async def ban_user(
    user_id: int, body: BanUserRequest, admin: AdminDep, session: DbSession
) -> ApiResponse[None]:
    """封禁（第一版**只禁登录**，不拦下单/评价）。

    ★ 生效方式是登录与刷新被拒 + 吊销该用户全部 refresh token；**已签发的
      access token 最长还能用 30 分钟**（认证依赖不查库，见 docs/18-account.md）。
      前端文案别写"立即生效"，否则第一次用就会被当成 bug。
    """
    await service.ban_user(session, admin_id=admin.id, user_id=user_id, reason=body.reason)
    return ApiResponse.ok(None)


@router.post(
    "/api/admin/users/{user_id}/unban", response_model=ApiResponse[None], summary="解封用户"
)
async def unban_user(user_id: int, admin: AdminDep, session: DbSession) -> ApiResponse[None]:
    """解封。**不恢复令牌** —— 用户用密码重新登录即可。已注销的账号解不了。"""
    await service.unban_user(session, admin_id=admin.id, user_id=user_id)
    return ApiResponse.ok(None)


@router.post(
    "/api/admin/users/{user_id}/reset-password",
    response_model=ApiResponse[TempPasswordOut],
    summary="重置用户密码",
)
async def reset_password(
    user_id: int, admin: AdminDep, session: DbSession
) -> ApiResponse[TempPasswordOut]:
    """把某用户重置为一个随机临时口令，**只在这里返回一次**。

    ★ 运营因此会短暂地知道这个口令。所以前端必须提示"只显示一次、请让用户
      登录后立即自行修改"。本期不做"强制下次登录改密"（docs/18-account.md）。
    """
    temp = await service.reset_password_by_admin(session, admin_id=admin.id, user_id=user_id)
    return ApiResponse.ok(TempPasswordOut(temp_password=temp))
