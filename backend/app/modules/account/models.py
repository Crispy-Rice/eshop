"""account 模块的 ORM 模型。

刻意**不使用 relationship()**：异步 SQLAlchemy 下懒加载会抛
``MissingGreenlet``，必须显式 ``selectinload``。与其到处小心，不如统一
在 repository 里写显式查询，行为更好预测。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base


class User(Base):
    """用户账号。

    手机号存三份（见 app/core/crypto.py 的说明）：
    哈希用于查询、密文用于客服查看、掩码用于列表展示。
    """

    __tablename__ = "user"
    __table_args__ = (
        # 手机号唯一性**只对未注销的行生效**（部分唯一索引）。
        # 注销是终态、不可恢复，若仍是普通唯一约束，那个手机号就被永久占死 ——
        # 用户注销后就再也回不来，是客服投诉点。流程见 service.close_account：
        # 注销时手机号仍占位（防"注销→立刻重注册"刷新人券），满冷静期后
        # 由 register 惰性把旧行的 phone_hash 改写成占位值，索引随之腾出。
        # 先例：product.sku 的 uk_sku_spu_code 也是 drop_constraint + 部分唯一索引。
        Index(
            "uk_user_phone_hash",
            "phone_hash",
            unique=True,
            postgresql_where=text("status <> 3"),  # 3 = UserStatus.CLOSED
        ),
        Index("idx_user_status_time", "status", "register_time"),
        Index("idx_user_role", "role"),
        {"schema": "account"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phone_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="HMAC-SHA256，查询与唯一约束用"
    )
    phone_cipher: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, comment="AES-256-GCM，客服查看用"
    )
    phone_masked: Mapped[str] = mapped_column(String(20), nullable=False, comment="138****8888，列表展示用")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="Argon2id")
    nickname: Mapped[str] = mapped_column(String(64), nullable=False)
    avatar: Mapped[str | None] = mapped_column(String(255))
    gender: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="0未知 1男 2女"
    )
    birthday: Mapped[date | None] = mapped_column(Date)
    member_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    member_expire: Mapped[datetime | None] = mapped_column(TS)
    credit_score: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("700"), comment="信用分，售后风控用"
    )
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'buyer'"),
        comment="buyer/merchant/admin/finance",
    )
    status: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("1"),
        comment="UserStatus：1正常 2封禁 3注销。2/3 的写入口见 service.ban_user / close_account",
    )
    ban_reason: Mapped[str | None] = mapped_column(
        String(255), comment="封禁理由。**会展示给用户**（登录失败的文案），解封时清空"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        TS, comment="注销时间。手机号 30 天冷静期以此为基准，见 service.register"
    )
    failed_logins: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(TS, comment="锁定到期时间，见登录防爆破")
    register_time: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class RefreshToken(Base):
    """刷新令牌。**只存哈希**，即使库被拖走也无法直接冒用。

    可吊销：登出时把对应行标记 revoked，续期时校验。
    """

    __tablename__ = "refresh_token"
    __table_args__ = (
        Index("idx_refresh_token_user", "user_id"),
        Index("idx_refresh_token_expire", "expires_at", postgresql_where=text("revoked_at IS NULL")),
        {"schema": "account"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.user.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="SHA-256(令牌原文)")
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class Shop(Base):
    """店铺。一个用户可以拥有多个店铺（一期先支持一个）。"""

    __tablename__ = "shop"
    __table_args__ = (
        Index("idx_shop_owner", "owner_user_id"),
        {"schema": "account"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    logo: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(255))
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.user.id"), nullable=False)
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1正常 2关闭 3审核中"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class ShopMember(Base):
    """店铺成员。把"谁能操作哪个店铺"独立成表，方便以后加店员角色。"""

    __tablename__ = "shop_member"
    __table_args__ = (
        Index("idx_shop_member_user", "user_id"),
        {"schema": "account"},
    )

    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.shop.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.user.id"), primary_key=True)
    member_role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'owner'"), comment="owner/staff"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class UserAddress(Base):
    """收货地址。下单时会把地址字段快照进订单，之后改地址不影响历史订单。"""

    __tablename__ = "user_address"
    __table_args__ = (
        Index("idx_user_address_user", "user_id", "status"),
        # 每个用户最多一个默认地址
        Index(
            "uk_user_address_default",
            "user_id",
            unique=True,
            postgresql_where=text("is_default AND status = 1"),
        ),
        CheckConstraint("status IN (1, 2)", name="status_valid"),
        {"schema": "account"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.user.id"), nullable=False)
    receiver_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, comment="收货电话，展示时脱敏")
    province: Mapped[str] = mapped_column(String(32), nullable=False)
    city: Mapped[str] = mapped_column(String(32), nullable=False)
    district: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str] = mapped_column(String(255), nullable=False)
    region_code: Mapped[str] = mapped_column(String(16), nullable=False, comment="行政区划码，运费计算用")
    tag: Mapped[str | None] = mapped_column(String(16), comment="家/公司/学校")
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1有效 2已删除"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class UserStateFlow(Base):
    """账号状态流转流水。**不可变的审计日志**。

    仿 ``trade.order_state_flow``：应用账号对这张表只授予 INSERT/SELECT
    （见 ``scripts/harden_grants.py`` 的 ``IMMUTABLE_TABLES``），从**权限层面**
    保证不可改。封禁/解封/注销/运营重置密码都往这里留一行 ——
    "谁在什么时候对谁做了什么、为什么"必须查得到，否则处置无法解释。

    ``user_id`` 刻意**不建外键**：注销会把用户行匿名化，审计行要能在那之后
    依然独立可读（也因此记的是 id 而不是关联对象）。
    """

    __tablename__ = "user_state_flow"
    __table_args__ = (
        Index("idx_user_state_flow_user", "user_id", "created_at"),
        {"schema": "account"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="BAN/UNBAN/CLOSE/RESET_PASSWORD"
    )
    from_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    to_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    operator_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="复用 OperatorType：1用户 2商家 3系统 4平台"
    )
    operator_id: Mapped[str | None] = mapped_column(
        String(64), comment="管理员 user id；自助注销时就是被注销的用户自己"
    )
    remark: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
