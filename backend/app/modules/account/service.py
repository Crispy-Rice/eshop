"""account 模块的领域逻辑。

这是本模块对外的唯一入口：其他模块只允许 import 这个文件里的函数，
不允许直接碰 models 或 repository（docs/01-overview.md §2）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import mask_name, mask_phone, phone_encrypt, phone_hash
from app.core.db import get_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.core.snowflake import next_id
from app.modules.account import repository as repo
from app.modules.account.models import RefreshToken, Shop, ShopMember, User, UserAddress
from app.modules.account.schemas import (
    AddressIn,
    AddressOut,
    AddressUpdate,
    ShopCreateRequest,
    ShopOut,
    ShopUpdateRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserOut,
)

logger = get_logger(__name__)

# 登录防爆破：连续失败达到上限后锁定一段时间
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15

# 单用户地址上限
MAX_ADDRESSES = 20


# ============================================================
# 内部工具
# ============================================================
def _now() -> datetime:
    return datetime.now(UTC)


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """用于"账号不存在"时也走一遍密码校验，抹平响应时间差异。

    否则攻击者可以通过响应快慢（几十毫秒 vs 几百毫秒）枚举出哪些手机号已注册。
    """
    return hash_password("dummy-password-for-timing-equalization")


def _to_user_out(user: User, shop_id: int | None = None) -> UserOut:
    return UserOut(
        id=user.id,
        phone=user.phone_masked,
        nickname=user.nickname,
        avatar=user.avatar,
        gender=user.gender,
        role=user.role,
        member_level=user.member_level,
        credit_score=user.credit_score,
        shop_id=shop_id,
        register_time=user.register_time,
    )


async def _shop_id_of(session: AsyncSession, user: User) -> int | None:
    if user.role == "buyer":
        return None
    shop = await repo.get_shop_by_owner(session, user.id)
    return shop.id if shop else None


async def _issue_tokens(
    session: AsyncSession, user: User, *, ip: str | None, user_agent: str | None
) -> TokenResponse:
    settings = get_settings()
    shop_id = await _shop_id_of(session, user)

    raw_token, token_hash = generate_refresh_token()
    await repo.insert_refresh_token(
        session,
        RefreshToken(
            id=next_id(),
            user_id=user.id,
            token_hash=token_hash,
            expires_at=_now() + timedelta(days=settings.jwt_refresh_ttl_days),
            user_agent=(user_agent or "")[:255] or None,
            ip=(ip or "")[:64] or None,
        ),
    )

    return TokenResponse(
        access_token=create_access_token(user.id, user.role, shop_id),
        refresh_token=raw_token,
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


# ============================================================
# 认证
# ============================================================
async def register(
    session: AsyncSession,
    *,
    phone: str,
    password: str,
    nickname: str | None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TokenResponse:
    phone_h = phone_hash(phone)
    existing = await repo.get_user_by_phone_hash(session, phone_h, for_update=True)
    if existing is not None:
        # 不暴露"这个手机号已注册"以外的信息（如注册时间、昵称）
        raise BizError(ErrorCode.VALIDATION_ERROR, "该手机号已注册")

    user = User(
        id=next_id(),
        phone_hash=phone_h,
        phone_cipher=phone_encrypt(phone),
        phone_masked=mask_phone(phone),
        password_hash=hash_password(password),
        nickname=nickname or f"用户{mask_phone(phone)[-4:]}",
        role="buyer",
        status=1,
        register_time=_now(),
    )
    await repo.insert_user(session, user)

    logger.info("用户注册成功", extra={"userId": user.id})
    return await _issue_tokens(session, user, ip=ip, user_agent=user_agent)


async def _record_login_failure(user_id: int) -> tuple[int, datetime | None]:
    """把登录失败计数写进**独立事务**，返回 (失败次数, 锁定到期时间)。

    ★ 为什么不能用调用方传进来的 session：登录失败后我们紧接着要抛
    ``BizError``，而路由里是 ``async with session.begin():`` —— 抛异常会让
    整个事务回滚，刚写进去的失败计数就一起没了，账号锁定形同虚设。
    这类"先写审计/计数再抛错"的场景，必须走独立事务。

    也正因如此，调用方**不能**对用户行加 FOR UPDATE：主事务持锁时，
    这个独立事务的 UPDATE 会一直等锁，形成死锁。计数用一条原子 UPDATE 累加，
    本身就不需要行锁。
    """
    async with get_session_factory()() as session, session.begin():
        return await repo.increment_login_failure(
            session, user_id, max_attempts=MAX_FAILED_LOGINS, lock_minutes=LOCK_MINUTES
        )


async def login(
    session: AsyncSession,
    *,
    phone: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TokenResponse:
    # 不加 FOR UPDATE：失败计数走独立事务，加锁会导致死锁（见 _record_login_failure）
    user = await repo.get_user_by_phone_hash(session, phone_hash(phone))

    # 账号不存在：也跑一次哈希校验，抹平时间差，然后返回与密码错误**完全相同**的提示
    if user is None:
        verify_password(password, _dummy_password_hash())
        raise BizError(ErrorCode.LOGIN_FAILED)

    now = _now()
    if user.locked_until is not None and user.locked_until > now:
        remaining = int((user.locked_until - now).total_seconds() // 60) + 1
        raise BizError(ErrorCode.ACCOUNT_LOCKED, f"登录失败次数过多，请 {remaining} 分钟后再试")

    if user.status != 1:
        raise BizError(ErrorCode.FORBIDDEN, "账号已被冻结，请联系客服")

    if not verify_password(password, user.password_hash):
        failed, locked_until = await _record_login_failure(user.id)
        if locked_until is not None:
            logger.warning("账号被锁定", extra={"userId": user.id, "failedLogins": failed})
        raise BizError(ErrorCode.LOGIN_FAILED)

    # 登录成功：清零失败计数；密码哈希参数升级过就顺手重算
    new_hash = hash_password(password) if password_needs_rehash(user.password_hash) else None
    await repo.touch_login_success(session, user.id, new_hash)

    return await _issue_tokens(session, user, ip=ip, user_agent=user_agent)


async def refresh(
    session: AsyncSession,
    *,
    refresh_token: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TokenResponse:
    """刷新 access token，并**轮换** refresh token。

    轮换的好处：旧令牌立即失效，被窃取的 refresh token 只能用一次，
    而且一旦真用户刷新就会让攻击者的令牌失效，异常可被发现。
    """
    row = await repo.get_refresh_token(session, hash_refresh_token(refresh_token), for_update=True)
    if row is None or row.revoked_at is not None:
        raise BizError(ErrorCode.UNAUTHORIZED, "登录已过期，请重新登录")
    if row.expires_at <= _now():
        raise BizError(ErrorCode.UNAUTHORIZED, "登录已过期，请重新登录")

    user = await repo.get_user_by_id(session, row.user_id)
    if user is None or user.status != 1:
        raise BizError(ErrorCode.UNAUTHORIZED, "账号不可用")

    await repo.revoke_refresh_token(session, row.id)
    return await _issue_tokens(session, user, ip=ip, user_agent=user_agent)


async def logout(session: AsyncSession, *, refresh_token: str) -> None:
    """登出：吊销这个 refresh token。access token 是无状态的，等它自然过期。"""
    row = await repo.get_refresh_token(session, hash_refresh_token(refresh_token))
    if row is not None:
        await repo.revoke_refresh_token(session, row.id)


# ============================================================
# 用户资料
# ============================================================
async def get_me(session: AsyncSession, user_id: int) -> UserOut:
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return _to_user_out(user, await _shop_id_of(session, user))


async def update_me(session: AsyncSession, user_id: int, req: UpdateProfileRequest) -> UserOut:
    values: dict[str, object] = {}
    if req.nickname is not None:
        values["nickname"] = req.nickname
    if req.avatar is not None:
        values["avatar"] = req.avatar
    if req.gender is not None:
        values["gender"] = req.gender

    # birthday 用 model_fields_set 区分"没传"和"显式传 null"（允许清空生日）
    if "birthday" in req.model_fields_set:
        values["birthday"] = req.birthday

    await repo.update_user_fields(session, user_id, values)
    return await get_me(session, user_id)


# ============================================================
# 店铺
# ============================================================
async def create_shop(session: AsyncSession, user_id: int, req: ShopCreateRequest) -> ShopOut:
    """开店。一期一个用户一个店铺，开通后角色升级为 merchant。"""
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.UNAUTHORIZED)

    # ★ 平台账号不开店。两道理由：
    #   1. 权限设计本就是这个口径 —— ``/api/merchant/*`` 按店铺归属判权，前端也刻意
    #      不给平台账号看商家菜单（运营维护的是平台数据，不该假装能管某个店的库存）。
    #      菜单都不给看却留着开店入口，是自相矛盾。
    #   2. 运营在自己的平台上卖货是利益冲突：商品审核只放给 admin，
    #      等于自家审核自家。
    #   只在前端藏按钮等于没挡 —— 接口本身必须拒绝。
    if user.role in {"admin", "finance"}:
        raise BizError(ErrorCode.FORBIDDEN, "平台账号不支持开店")

    if await repo.get_shop_by_owner(session, user_id) is not None:
        raise BizError(ErrorCode.VALIDATION_ERROR, "你已经开过店铺了")

    shop = Shop(
        id=next_id(),
        name=req.name,
        logo=req.logo,
        description=req.description,
        owner_user_id=user_id,
        status=1,
    )
    await repo.insert_shop(session, shop)
    await repo.insert_shop_member(session, ShopMember(shop_id=shop.id, user_id=user_id, member_role="owner"))

    if user.role == "buyer":
        await repo.update_user_fields(session, user_id, {"role": "merchant"})

    logger.info("店铺创建成功", extra={"userId": user_id, "shopId": shop.id})
    return _to_shop_out(shop)


async def get_shop_id(session: AsyncSession, user_id: int) -> int | None:
    """当前用户的店铺 ID，没有店铺返回 None。

    其他模块（product、trade…）用它判断"这个用户能不能卖东西"。
    """
    shop = await repo.get_shop_by_owner(session, user_id)
    return shop.id if shop else None


async def get_my_shop(session: AsyncSession, user_id: int) -> ShopOut:
    shop = await repo.get_shop_by_owner(session, user_id)
    if shop is None:
        raise BizError(ErrorCode.NOT_FOUND, "你还没有店铺")
    return _to_shop_out(shop)


async def update_my_shop(session: AsyncSession, user_id: int, req: ShopUpdateRequest) -> ShopOut:
    """改店铺设置：名称 / LOGO / 简介。

    ★ ``logo`` 与 ``description`` 用 ``model_fields_set`` 区分"没传"和"显式传
      null" —— 后者是**清空**（比如把简介删掉），不能当成不改。
    ``name`` 反之：传 None 直接忽略，店铺不能没有名字。

    改名不影响历史订单 —— trade 的 ``order_sub.shop_name_snap`` 存的是下单时的
    快照，改动只作用于之后的展示，正是快照存在的意义。

    归属校验和取值合成一次查询：按 owner_user_id 找店，找不到就是没开店。
    """
    shop = await repo.get_shop_by_owner(session, user_id)
    if shop is None:
        raise BizError(ErrorCode.NOT_FOUND, "你还没有店铺")

    values: dict[str, object] = {}
    if req.name is not None:
        values["name"] = req.name
    for field in ("logo", "description"):
        if field in req.model_fields_set:
            values[field] = getattr(req, field)

    await repo.update_shop_fields(session, shop.id, values)

    updated = await repo.get_shop_by_id(session, shop.id)
    if updated is None:  # pragma: no cover - 刚更新过，理论上不会发生
        raise BizError(ErrorCode.NOT_FOUND, "店铺不存在")
    logger.info("店铺设置已更新", extra={"userId": user_id, "shopId": shop.id})
    return _to_shop_out(updated)


async def get_public_shop(session: AsyncSession, shop_id: int) -> ShopOut:
    """按 ID 取店铺的**公开**信息，商品详情页用来显示"这件商品是哪家店的"。

    ★ 刻意做成 account 自己的公开接口，而不是让 product 直接读 account：
      docs/01 §2 的依赖图里没有 product → account 这条边，商品模块的 repository
      也声明了"只碰 product schema"。由前端把「商品」和「店铺」两个请求拼起来，
      两边都不越界 —— 而且顺带能拿到 logo 与简介，不只是一个店名。

    返回的就是 ShopOut（id / 名称 / logo / 简介 / 状态），这几个本来就是对外的字段，
    不含 owner_user_id 之类。
    """
    shop = await repo.get_shop_by_id(session, shop_id)
    if shop is None:
        raise BizError(ErrorCode.NOT_FOUND, "店铺不存在")
    return _to_shop_out(shop)


async def list_public_shops(session: AsyncSession, shop_ids: Sequence[int]) -> list[ShopOut]:
    """批量取店铺公开信息。

    商品列表页一次显示 N 个商品，逐个请求就是 N+1 —— 而且那些商品往往只来自
    一两个店，批量去重之后通常只查一次。
    """
    if not shop_ids:
        return []
    shops = await repo.list_shops_by_ids(session, list(shop_ids))
    return [_to_shop_out(shop) for shop in shops]


def _to_shop_out(shop: Shop) -> ShopOut:
    return ShopOut(
        id=shop.id, name=shop.name, logo=shop.logo, description=shop.description, status=shop.status
    )


async def search_shops(
    session: AsyncSession, *, keyword: str | None, limit: int
) -> list[ShopOut]:
    """按名称搜店铺。给运营侧的选择器用（券的"指定店铺"）。

    返回公开的 ``ShopOut``：id / 名称 / logo / 简介 / 状态，本来就是对外的字段。
    调用方（promotion）需要的是"认出是哪家店 + 看出它已关闭"，够用。
    """
    return [
        _to_shop_out(s) for s in await repo.search_shops(session, keyword=keyword, limit=limit)
    ]


async def list_shop_names(session: AsyncSession, shop_ids: Sequence[int]) -> dict[int, str]:
    """批量取店铺名。购物车按店铺分组展示时用，避免逐个查询。"""
    shops = await repo.list_shops_by_ids(session, list(shop_ids))
    return {s.id: s.name for s in shops}


async def list_user_profiles(
    session: AsyncSession, user_ids: Sequence[int]
) -> dict[int, tuple[str, str | None]]:
    """批量取用户的 ``(昵称, 头像)``。评价列表展示评价人时用，避免逐个查询。

    评价模块只拿昵称与头像做展示，**不暴露手机号等其它字段** ——
    所以这里返回元组而不是完整 DTO。
    """
    users = await repo.list_users_by_ids(session, list(user_ids))
    return {u.id: (u.nickname, u.avatar) for u in users}


async def find_user_by_phone(session: AsyncSession, phone: str) -> tuple[int, str, str] | None:
    """按手机号定位用户，返回 ``(userId, 昵称, 打码手机号)``；查不到返回 None。

    给运营侧的"定向发券"用：那个场景下运营手上只有**用户报出来的手机号**，
    拿不到雪花 ID —— 所以入口必须能按手机号找人。

    ★ 只回这三个字段。手机号是加密存的，但"昵称 + 打码号"已经够运营确认
      "发给谁"，不必（也不该）把整行 ``User`` 漏给别的模块。
    """
    user = await repo.get_user_by_phone_hash(session, phone_hash(phone))
    if user is None:
        return None
    return user.id, user.nickname, user.phone_masked or ""


async def user_exists(session: AsyncSession, user_id: int) -> bool:
    """用户是否存在。给别的模块的写入口做存在性校验用。"""
    return await repo.get_user_by_id(session, user_id) is not None


async def list_user_labels(session: AsyncSession, user_ids: Sequence[int]) -> dict[int, tuple[str, str]]:
    """批量取 ``(昵称, 打码手机号)``。

    给运营侧的审计列表用（补发记录）：那里既要认人、又要防重名，
    所以比 ``list_user_profiles`` 多带一个打码号。
    """
    if not user_ids:
        return {}
    users = await repo.list_users_by_ids(session, list(user_ids))
    return {u.id: (u.nickname, u.phone_masked or "") for u in users}


# ============================================================
# 收货地址
# ============================================================
def _to_address_out(a: UserAddress) -> AddressOut:
    return AddressOut(
        id=a.id,
        receiver_name=a.receiver_name,
        phone=mask_phone(a.phone),
        province=a.province,
        city=a.city,
        district=a.district,
        detail=a.detail,
        region_code=a.region_code,
        tag=a.tag,
        is_default=a.is_default,
    )


async def list_addresses(session: AsyncSession, user_id: int) -> list[AddressOut]:
    return [_to_address_out(a) for a in await repo.list_addresses(session, user_id)]


async def create_address(session: AsyncSession, user_id: int, req: AddressIn) -> AddressOut:
    if await repo.count_addresses(session, user_id) >= MAX_ADDRESSES:
        raise BizError(ErrorCode.VALIDATION_ERROR, f"最多只能保存 {MAX_ADDRESSES} 个收货地址")

    # 第一个地址自动设为默认，避免用户下单时没有默认地址
    is_default = req.is_default or await repo.count_addresses(session, user_id) == 0
    if is_default:
        await repo.clear_default_address(session, user_id)

    address = UserAddress(
        id=next_id(),
        user_id=user_id,
        receiver_name=req.receiver_name,
        phone=req.phone,
        province=req.province,
        city=req.city,
        district=req.district,
        detail=req.detail,
        region_code=req.region_code,
        tag=req.tag,
        is_default=is_default,
        status=1,
    )
    await repo.insert_address(session, address)
    return _to_address_out(address)


async def update_address(
    session: AsyncSession, user_id: int, address_id: int, req: AddressUpdate
) -> AddressOut:
    # 归属校验和取值合并成一次查询
    if await repo.get_address(session, address_id, user_id) is None:
        raise BizError(ErrorCode.NOT_FOUND, "地址不存在")

    if req.is_default:
        await repo.clear_default_address(session, user_id)

    await repo.update_address(
        session,
        address_id,
        {
            "receiver_name": req.receiver_name,
            "phone": req.phone,
            "province": req.province,
            "city": req.city,
            "district": req.district,
            "detail": req.detail,
            "region_code": req.region_code,
            "tag": req.tag,
            "is_default": req.is_default,
        },
    )

    updated = await repo.get_address(session, address_id, user_id)
    if updated is None:  # pragma: no cover - 理论上不会发生
        raise BizError(ErrorCode.NOT_FOUND, "地址不存在")
    return _to_address_out(updated)


async def delete_address(session: AsyncSession, user_id: int, address_id: int) -> None:
    if await repo.soft_delete_address(session, address_id, user_id) == 0:
        raise BizError(ErrorCode.NOT_FOUND, "地址不存在")


async def get_address_for_order(session: AsyncSession, user_id: int, address_id: int) -> UserAddress:
    """给 trade 模块下单时用：取原始地址（未脱敏），用于写入订单快照。

    返回的是 ORM 对象，调用方**不要**把它直接返回给前端。
    """
    address = await repo.get_address(session, address_id, user_id)
    if address is None:
        raise BizError(ErrorCode.NOT_FOUND, "收货地址不存在")
    return address


# 供其他模块复用的格式化函数
__all__ = [
    "create_address",
    "create_shop",
    "delete_address",
    "get_address_for_order",
    "get_me",
    "get_my_shop",
    "list_addresses",
    "login",
    "logout",
    "mask_name",
    "refresh",
    "register",
    "update_address",
    "update_me",
    "update_my_shop",
]
