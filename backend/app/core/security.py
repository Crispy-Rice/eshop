"""认证与密码：Argon2id 哈希 + JWT。

第一期用账号密码登录（不接短信，见 docs/16-deployment.md §6）。
access token 有效期 30 分钟，只放最小信息；refresh token 是随机串，
哈希后存 PG、可吊销。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from app.core.config import get_settings
from app.core.errors import BizError, ErrorCode

_hasher = PasswordHasher()  # 默认 Argon2id

JWT_ALGORITHM = "HS256"


# ------------------------------------------------------------
# 密码
# ------------------------------------------------------------
def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    """校验密码。任何异常都当作校验失败，不向外泄露细节。

    登录接口对"账号不存在"和"密码错误"必须返回同一个错误
    （docs/15-api-and-errors.md §3.3），所以调用方也不要用返回值区分。
    """
    try:
        return _hasher.verify(hashed, raw)
    except (Argon2Error, ValueError, TypeError):
        return False


def password_needs_rehash(hashed: str) -> bool:
    """参数升级后（如调高了内存成本），登录成功时顺手重算哈希。"""
    try:
        return _hasher.check_needs_rehash(hashed)
    except (Argon2Error, ValueError, TypeError):
        return False


def validate_password_strength(raw: str) -> str:
    """密码强度校验。返回原值，校验失败抛 ValueError（由 Pydantic 转成 400）。

    规则：长度达标 + 至少包含两类字符（小写/大写/数字/符号）。
    不强制"必须含特殊字符"这类容易逼用户写便签的规则。
    """
    min_length = get_settings().password_min_length
    if len(raw) < min_length:
        raise ValueError(f"密码至少 {min_length} 位")
    if len(raw) > 128:
        raise ValueError("密码过长")

    classes = sum(
        (
            any(c.islower() for c in raw),
            any(c.isupper() for c in raw),
            any(c.isdigit() for c in raw),
            any(not c.isalnum() for c in raw),
        )
    )
    if classes < 2:
        raise ValueError("密码需包含字母、数字、符号中的至少两类")

    if raw.lower() in {"12345678", "password", "qwertyui", "11111111"}:
        raise ValueError("密码过于简单")
    return raw


# ------------------------------------------------------------
# 刷新令牌
# ------------------------------------------------------------
def generate_refresh_token() -> tuple[str, str]:
    """生成刷新令牌，返回 (原文, SHA-256 哈希)。

    原文只在这一次响应里返回给客户端，库里只存哈希 ——
    即使数据库被拖走也无法直接冒用。
    """
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------
# JWT
# ------------------------------------------------------------
def create_access_token(user_id: int, role: str, shop_id: int | None = None) -> str:
    settings = get_settings()
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + settings.jwt_access_ttl_minutes * 60,
    }
    if shop_id is not None:
        payload["shop_id"] = shop_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """解析并校验 access token。失败一律抛 UNAUTHORIZED，不区分原因。"""
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise BizError(ErrorCode.UNAUTHORIZED, "登录已过期，请重新登录") from exc
    except jwt.PyJWTError as exc:
        raise BizError(ErrorCode.UNAUTHORIZED) from exc
