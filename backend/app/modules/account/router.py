"""account 模块的 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request

from app.core.deps import CurrentUserDep, DbSession, client_ip
from app.core.response import ApiResponse
from app.modules.account import service
from app.modules.account.schemas import (
    AddressIn,
    AddressOut,
    AddressUpdate,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ShopCreateRequest,
    ShopOut,
    TokenResponse,
    UpdateProfileRequest,
    UserOut,
)

router = APIRouter()


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
    async with session.begin():
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
    async with session.begin():
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
    async with session.begin():
        tokens = await service.refresh(
            session,
            refresh_token=body.refresh_token,
            ip=client_ip(request),
            user_agent=user_agent,
        )
    return ApiResponse.ok(tokens)


@router.post("/api/auth/logout", response_model=ApiResponse[None], summary="登出")
async def logout(body: RefreshRequest, session: DbSession) -> ApiResponse[None]:
    async with session.begin():
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
    async with session.begin():
        result = await service.update_me(session, user.id, body)
    return ApiResponse.ok(result)


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
    async with session.begin():
        result = await service.create_address(session, user.id, body)
    return ApiResponse.ok(result)


@router.put("/api/me/addresses/{address_id}", response_model=ApiResponse[AddressOut], summary="修改地址")
async def update_address(
    address_id: int, body: AddressUpdate, user: CurrentUserDep, session: DbSession
) -> ApiResponse[AddressOut]:
    async with session.begin():
        result = await service.update_address(session, user.id, address_id, body)
    return ApiResponse.ok(result)


@router.delete("/api/me/addresses/{address_id}", response_model=ApiResponse[None], summary="删除地址")
async def delete_address(address_id: int, user: CurrentUserDep, session: DbSession) -> ApiResponse[None]:
    async with session.begin():
        await service.delete_address(session, user.id, address_id)
    return ApiResponse.ok(None)


# ============================================================
# 店铺（商家端）
# ============================================================
@router.post("/api/merchant/shop", response_model=ApiResponse[ShopOut], summary="开店")
async def create_shop(
    body: ShopCreateRequest, user: CurrentUserDep, session: DbSession
) -> ApiResponse[ShopOut]:
    async with session.begin():
        result = await service.create_shop(session, user.id, body)
    return ApiResponse.ok(result)


@router.get("/api/merchant/shop", response_model=ApiResponse[ShopOut], summary="我的店铺")
async def get_my_shop(user: CurrentUserDep, session: DbSession) -> ApiResponse[ShopOut]:
    return ApiResponse.ok(await service.get_my_shop(session, user.id))
