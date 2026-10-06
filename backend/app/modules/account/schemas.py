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


class ShopUpdateRequest(CamelModel):
    """店铺设置。部分更新 —— 只改传了的字段，和 ``UpdateProfileRequest`` 一致。

    ``logo`` / ``description`` 是可空的**选填**字段：服务端靠 ``model_fields_set``
    区分"没传"和"显式传 null"（后者表示清空），所以这里不能把 None 当成"不改"。
    ``name`` 相反 —— 店铺不能没有名字，传 None 直接当作不改。
    """

    name: str | None = Field(default=None, min_length=2, max_length=64)
    logo: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=255)


class ShopOut(CamelModel):
    id: SnowflakeId
    name: str
    logo: str | None = None
    description: str | None = None
    status: int = 1


# ============================================================
# 账号安全（自助）
# ============================================================
class ChangePasswordRequest(CamelModel):
    """改密码。新密码**复用注册那套强度校验** —— 口径必须与注册一致，
    否则会出现"注册时不让用、改密码时反而设得上"的漂移。"""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return validate_password_strength(v)


class CloseAccountRequest(CamelModel):
    """注销。要求输入当前密码 —— 没有短信验证码时，这是唯一能证明"是本人"
    的二次确认手段。密码不对就拒绝，免得别人拿着你的登录态把你的号注销掉。"""

    password: str = Field(min_length=1, max_length=128)


class DeactivationCheckOut(CamelModel):
    """注销前置检查的结果。

    ★ 只报**事实**（能不能注销 + 各挡了几项），**不在这里拼文案** —— 前端要按
      被挡住的那几项各给一个跳转按钮（去订单 / 去售后 / 去店铺设置），
      拼好的一句话反而拆不出这些动作。真正注销时那条错误消息仍是完整句子，
      因为它可能被别的调用方直接用。
    """

    can_deactivate: bool
    has_shop: bool
    order_count: int
    refund_count: int


# ============================================================
# 运营：用户管理
# ============================================================
# 与 core.enums.UserStatus 对齐
USER_STATUS_TEXT: dict[int, str] = {1: "正常", 2: "已封禁", 3: "已注销"}


class BanUserRequest(CamelModel):
    """封禁。**理由必填**，而且它不只是一条内部备注 —— 被封的人登录时会
    原样看到（docs/18-account.md），所以文案要能见人。"""

    reason: str = Field(min_length=2, max_length=255)


class AdminUserOut(CamelModel):
    id: SnowflakeId
    nickname: str
    phone: str = Field(description="脱敏手机号；后台刻意不提供解密查看")
    avatar: str | None = None
    gender: int = 0
    role: str = "buyer"
    status: int = 1
    status_text: str = ""
    member_level: int = 1
    credit_score: int = 700
    is_shop_owner: bool = Field(default=False, description="名下有店铺；店主不能自助注销")
    register_time: datetime
    closed_at: datetime | None = None


class AdminUserDetailOut(AdminUserOut):
    ban_reason: str | None = Field(default=None, description="最近一次封禁的理由")


class AdminUserListOut(CamelModel):
    items: list[AdminUserOut]
    next_cursor: str | None = None
    has_more: bool = False


class TempPasswordOut(CamelModel):
    """运营重置密码的返回。**只在这里出现这一次** —— 库里只有哈希，关掉就再也看不到。"""

    temp_password: str
