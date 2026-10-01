"""account 模块的请求/响应模型。"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, field_validator

from app.core.schemas import CamelModel, SnowflakeId
from app.core.security import validate_password_strength

# 中国大陆手机号
PHONE_PATTERN = r"^1[3-9]\d{9}$"


# ============================================================
# 认证
# ============================================================
class RegisterRequest(CamelModel):
    phone: str = Field(pattern=PHONE_PATTERN, description="手机号")
    password: str = Field(min_length=8, max_length=128)
    nickname: str | None = Field(default=None, max_length=64)

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return validate_password_strength(v)


class LoginRequest(CamelModel):
    phone: str = Field(pattern=PHONE_PATTERN)
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(CamelModel):
    refresh_token: str = Field(min_length=16, max_length=128)


class TokenResponse(CamelModel):
    access_token: str
    refresh_token: str

    token_type: str = "Bearer"  # noqa: S105
    expires_in: int = Field(description="access token 有效期（秒）")


# ============================================================
# 用户
# ============================================================
class UserOut(CamelModel):
    id: SnowflakeId
    phone: str = Field(description="脱敏后的手机号，如 138****8888")
    nickname: str
    avatar: str | None = None
    gender: int = 0
    role: str = "buyer"
    member_level: int = 1
    credit_score: int = 700
    # 有店铺的商家才返回
    shop_id: SnowflakeId | None = None
    register_time: datetime


class UpdateProfileRequest(CamelModel):
    nickname: str | None = Field(default=None, min_length=1, max_length=64)
    avatar: str | None = Field(default=None, max_length=255)
    gender: int | None = Field(default=None, ge=0, le=2)
    birthday: date | None = None


# ============================================================
# 收货地址
# ============================================================
class AddressIn(CamelModel):
    receiver_name: str = Field(min_length=1, max_length=64)
    phone: str = Field(pattern=PHONE_PATTERN)
    province: str = Field(min_length=1, max_length=32)
    city: str = Field(min_length=1, max_length=32)
    district: str = Field(min_length=1, max_length=32)
    detail: str = Field(min_length=1, max_length=255)
    region_code: str = Field(min_length=1, max_length=16, description="行政区划码")
    tag: str | None = Field(default=None, max_length=16)
    is_default: bool = False


class AddressUpdate(CamelModel):
    """全量更新（前端表单本来就是整体提交）。字段与 AddressIn 相同。"""

    receiver_name: str = Field(min_length=1, max_length=64)
    phone: str = Field(pattern=PHONE_PATTERN)
    province: str = Field(min_length=1, max_length=32)
    city: str = Field(min_length=1, max_length=32)
    district: str = Field(min_length=1, max_length=32)
    detail: str = Field(min_length=1, max_length=255)
    region_code: str = Field(min_length=1, max_length=16)
    tag: str | None = Field(default=None, max_length=16)
    is_default: bool = False


class AddressOut(CamelModel):
    id: SnowflakeId
    receiver_name: str
    phone: str = Field(description="脱敏后的手机号")
    province: str
    city: str
    district: str
    detail: str
    region_code: str
    tag: str | None = None
    is_default: bool = False


# ============================================================
# 店铺
# ============================================================
class ShopCreateRequest(CamelModel):
    name: str = Field(min_length=2, max_length=64)
    logo: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=255)


class ShopOut(CamelModel):
    id: SnowflakeId
    name: str
    logo: str | None = None
    description: str | None = None
    status: int = 1
