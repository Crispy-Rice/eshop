"""FastAPI 依赖别名与认证依赖。

路由签名里统一用这里的别名，读起来更短，也便于以后统一改造：

    @router.post("/api/orders")
    async def create_order(
        body: CreateOrderRequest,
        user: CurrentUserDep,
        session: DbSession,
        redis: RedisDep,
    ) -> ApiResponse[OrderCreated]:
        async with session.begin():
            ...
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import CurrentUser
from app.core.db import get_session
from app.core.errors import BizError, ErrorCode
from app.core.redis import get_redis
from app.core.security import decode_access_token

DbSession = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise BizError(ErrorCode.UNAUTHORIZED)

    payload = decode_access_token(authorization[7:].strip())
    return CurrentUser(
        id=int(payload["sub"]),
        role=payload.get("role", "buyer"),
        shop_id=payload.get("shop_id"),
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def get_current_merchant(user: CurrentUserDep) -> CurrentUser:
    """商家身份。运营/财务也能进来，具体权限由 require_role 再收。"""
    if not user.is_merchant():
        raise BizError(ErrorCode.FORBIDDEN)
    return user


CurrentMerchantDep = Annotated[CurrentUser, Depends(get_current_merchant)]


def require_role(*roles: str):
    """用法：``user: Annotated[CurrentUser, Depends(require_role("admin", "finance"))]``"""

    async def _dep(user: CurrentUserDep) -> CurrentUser:
        if user.role not in roles:
            raise BizError(ErrorCode.FORBIDDEN)
        return user

    return _dep


def client_ip(request: Request) -> str | None:
    """取客户端真实 IP。

    生产环境前面有 Nginx，`request.client` 拿到的是 Nginx 的地址，
    所以要读它设置的 X-Real-IP。只在开发环境（直连）才回落到 socket 地址。
    """
    forwarded = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For")
    if forwarded:
        # X-Forwarded-For 可能是 "客户端, 代理1, 代理2"，取第一个
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


# 幂等键长度的上限。调用方会再拼上前缀与 skuId 组成最终的业务幂等键，
# 而那个字段是 VARCHAR(64)，所以这里留足余量。
MAX_IDEMPOTENCY_KEY_LEN = 32


async def idempotency_key(
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """取 ``Idempotency-Key`` 请求头，缺失就报错。

    ★ 这一层只负责**校验与提取**，真正的幂等由底层保证：
    库存变更写 ``inventory.stock_biz_key``（主键冲突即已处理过），
    不依赖这个头本身。所以即使客户端换一个 key 重放，DB 也不会重复扣减——
    幂等键只是让**同一笔操作的重试**能被识别出来。

    docs/10 描述的通用幂等层（Redis ``SET NX`` + 缓存上次响应体，
    配 ``CachedResponse``）留到 trade/payment 需要"重放原响应"时再建。
    """
    if not key or not key.strip():
        raise BizError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    cleaned = key.strip()
    if len(cleaned) > MAX_IDEMPOTENCY_KEY_LEN:
        raise BizError(
            ErrorCode.VALIDATION_ERROR,
            f"Idempotency-Key 不能超过 {MAX_IDEMPOTENCY_KEY_LEN} 个字符",
        )
    return cleaned


IdempotencyKeyDep = Annotated[str, Depends(idempotency_key)]
