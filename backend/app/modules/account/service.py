"""account 模块的领域逻辑。

这是本模块对外的唯一入口：其他模块只允许 import 这个文件里的函数，
不允许直接碰 models 或 repository（docs/01-overview.md §2）。
"""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import after_commit
from app.core.config import get_settings
from app.core.crypto import mask_name, mask_phone, phone_encrypt, phone_hash
from app.core.db import get_session_factory
from app.core.enums import OperatorType, UserStatus
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    generate_temp_password,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.core.snowflake import next_id
from app.modules.account import repository as repo
from app.modules.account.models import (
    SHOP_STATUS_ACTIVE,
    SHOP_STATUS_TEXT,
    RefreshToken,
    Shop,
    ShopMember,
    User,
    UserAddress,
    UserStateFlow,
)
from app.modules.account.schemas import (
    PHONE_PATTERN,
    USER_STATUS_TEXT,
    AddressIn,
    AddressOut,
    AddressUpdate,
    AdminUserDetailOut,
    AdminUserListOut,
    AdminUserOut,
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

# 平台账号（运营 / 财务）。开不了店（见 create_shop），也不能被别的运营封禁或
# 重置密码 —— 否则一次越权就能把平台自己锁死，且没有第二条路能救回来。
PLATFORM_ROLES = frozenset({"admin", "finance"})

# 注销后同号重新注册的冷静期。目的是挡住"注销 → 立刻重注册"刷新人券，
# 而不是永久占用号码（见 register 与 close_account）。
PHONE_COOLDOWN_DAYS = 30

# 注销后写进昵称的占位。★ **不能是空串**：评价区、导航栏都直接显示它。
# 它同时也是"这条评价来自已注销用户"的唯一标识（review 不存昵称快照，
# 是实时读 user 表的，所以这里改一下，该用户全部历史评价立刻跟着变）。
CLOSED_NICKNAME = "已注销用户"
CLOSED_PHONE_MASKED = "已注销"


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


def _ban_message(reason: str | None) -> str:
    """封禁提示的**完整句子**（接口原样透传、也进日志的那种）。

    ★ 理由放进**括号**。原来是 ``f"{base}：{reason}"``，于是
      「账号已被封禁，请联系客服：疑似刷单」读起来像"客服的联系方式是疑似刷单" ——
      冒号前面那句话把理由变成了它的宾语。
    ★ 买家文案用「冻结」而不是「封禁」：封禁是运营侧的术语（后台按钮、审计流水都用
      它），对买家偏重；而 ``account.user.status`` 的建表注释本来就写的是「2冻结」。
    """
    base = "该账号已被冻结，暂时无法登录"
    return f"{base}（原因：{reason}）" if reason else base


def _ban_error(user: User) -> BizError:
    """构造封禁异常。

    ★ 理由同时走**结构化 data**：前端据此自己排版（标题 / 原因 / 联系方式分三行），
      不用去切 ``message`` 里那个中文冒号。原来的毛病就出在拼字符串上 ——
      把"发生了什么 / 为什么 / 该怎么办"三件事挤进一个字符串，必然读不通。
    """
    return BizError(
        ErrorCode.ACCOUNT_BANNED,
        _ban_message(user.ban_reason),
        data={"reason": user.ban_reason},
    )


def _looks_like_phone(keyword: str) -> bool:
    """后台关键词搜索时判断"这串是不是手机号"。

    是手机号就走 ``phone_hash`` 等值匹配（库里存的是 HMAC，没法 LIKE），
    否则按昵称模糊搜 —— 分派规则见 ``admin_list_users``。
    """
    return re.fullmatch(PHONE_PATTERN, keyword) is not None


async def _require_usable_user(session: AsyncSession, user_id: int) -> User:
    """取账号并确认当前可用（未封禁、未注销）。自助改密 / 注销共用这个入口。

    ★ 这两件事都不该对已封禁 / 已注销的账号生效 —— 否则在被封后、access token
      还没过期的那个窗口里（最长 30 分钟，见 docs/18-account.md §3），
      被封的人还能自己改密码甚至注销。
    """
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    if user.status == int(UserStatus.BANNED):
        raise _ban_error(user)
    if user.status == int(UserStatus.CLOSED):
        raise BizError(ErrorCode.ACCOUNT_CLOSED)
    return user


async def _write_user_flow(
    session: AsyncSession,
    *,
    user_id: int,
    event: str,
    from_status: int,
    to_status: int,
    operator_type: OperatorType,
    operator_id: str | None,
    remark: str | None,
) -> None:
    """写一行账号状态流水（不可变审计，见 account.models.UserStateFlow）。

    与状态变更在**同一个事务**里：审计与状态必须同生共死，否则会出现
    "状态变了但没有记录"，或者反过来的鬼影记录。
    """
    await repo.insert_user_state_flow(
        session,
        UserStateFlow(
            user_id=user_id,
            event=event,
            from_status=from_status,
            to_status=to_status,
            operator_type=int(operator_type),
            operator_id=operator_id,
            remark=remark,
        ),
    )


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

    # 先看有没有**未注销**的账号。部分唯一索引（WHERE status <> 3）保证至多一条。
    # 加 FOR UPDATE 把并发的同号注册串起来，让后面的插入不会撞唯一索引。
    live = await repo.get_live_user_by_phone_hash(session, phone_h, for_update=True)
    if live is not None:
        # 不暴露"这个手机号已注册"以外的信息（如注册时间、昵称）
        raise BizError(ErrorCode.VALIDATION_ERROR, "该手机号已注册")

    # 再看有没有"注销未满冷静期"的记录。
    #
    # ★ 这里能查到，是因为 close_account **刻意保留了 phone_hash 原值**。
    #   如果当初把它覆写成与手机号无关的占位值，这条查询就永远查不到，
    #   冷静期会静默失效 —— 同号立刻就能重新注册。
    closed = await repo.get_closed_user_by_phone_hash(session, phone_h)
    if closed is not None and closed.closed_at is not None:
        released_at = closed.closed_at + timedelta(days=PHONE_COOLDOWN_DAYS)
        if released_at > _now():
            raise BizError(
                ErrorCode.PHONE_IN_COOLDOWN,
                f"该手机号注销未满 {PHONE_COOLDOWN_DAYS} 天，"
                f"{released_at:%Y-%m-%d} 之后可重新注册",
            )
    # 冷静期已过：**不必**改写那条旧记录 —— 已注销的行不在
    # `uk_user_phone_hash` 这个部分唯一索引里，新行带同样的 phone_hash 也能插进去。
    # （closed_at 为 NULL 的 status=3 行只可能来自手工改库，按"已释放"处理：
    #  让一个号永久无法注册，比漏掉一次冷静期更糟。）
    elif closed is not None:
        logger.warning("已注销记录缺少 closed_at，按已释放处理", extra={"userId": closed.id})

    user = User(
        id=next_id(),
        phone_hash=phone_h,
        phone_cipher=phone_encrypt(phone),
        phone_masked=mask_phone(phone),
        password_hash=hash_password(password),
        nickname=nickname or f"用户{mask_phone(phone)[-4:]}",
        role="buyer",
        status=int(UserStatus.NORMAL),
        register_time=_now(),
    )
    try:
        await repo.insert_user(session, user)
    except IntegrityError as exc:
        # 并发兜底：两个同号注册可能同时通过上面的检查（谁也看不见谁），
        # 最终由部分唯一索引拦住。转成与单线程完全一致的 400，不要漏成 500。
        raise BizError(ErrorCode.VALIDATION_ERROR, "该手机号已注册") from exc

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
    # ★ 只查**未注销**的行：同号重新注册后，同一个 phone_hash 下会同时存在
    #   活跃的新号与已注销的旧号，盲查会随机命中其中一个（结果不确定）。
    user = await repo.get_live_user_by_phone_hash(session, phone_hash(phone))

    # 账号不存在**或已注销**：走同一条路 —— 跑一次哈希校验抹平时间差，
    # 然后返回与密码错误**完全相同**的提示。
    # ★ 已注销刻意做得与"不存在"不可区分：否则任何人报个手机号就能问出
    #   "这个号曾经注册过"（账号枚举）。注销后登不进来是本分 —— 号确实没了。
    if user is None:
        verify_password(password, _dummy_password_hash())
        raise BizError(ErrorCode.LOGIN_FAILED)

    now = _now()
    if user.locked_until is not None and user.locked_until > now:
        remaining = int((user.locked_until - now).total_seconds() // 60) + 1
        raise BizError(ErrorCode.ACCOUNT_LOCKED, f"登录失败次数过多，请 {remaining} 分钟后再试")

    # 封禁据实告知，并带上运营填的理由 —— 封禁不是秘密，用户需要知道找谁申诉。
    # （注销到不了这里，上面已按"不存在"处理。）
    if user.status == int(UserStatus.BANNED):
        raise _ban_error(user)
    if user.status != int(UserStatus.NORMAL):
        # 兜底：状态被改成了未知值（只可能来自手工改库）
        raise BizError(ErrorCode.FORBIDDEN, "账号状态异常，请联系客服")

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
    if row is None:
        raise BizError(ErrorCode.UNAUTHORIZED, "登录已过期，请重新登录")

    user = await repo.get_user_by_id(session, row.user_id)

    # ★ 账号状态要**排在令牌有效性之前**判。
    #
    #   封禁与注销都会**吊销该用户的全部 refresh token**，所以按原来"先查吊销、
    #   再查状态"的顺序，被封的人拿到的永远是一句「登录已过期，请重新登录」——
    #   而他会照着做，然后再失败一次，并且始终不知道自己的号已经被冻结、该找谁。
    #   这与下面那句注释想做的事正好相反（意图对、顺序错）。
    #
    #   代价是拿一个**已被吊销**的令牌来刷新时会多一次主键查询；这是错误分支，
    #   无所谓。安全性上也没有变化：能看到这个回答的人本来就持有一个曾签发出去的
    #   refresh token（不然连 row 都查不到，只会拿到上面那个 401）。
    if user is not None and user.status == int(UserStatus.BANNED):
        raise _ban_error(user)

    if row.revoked_at is not None:
        raise BizError(ErrorCode.UNAUTHORIZED, "登录已过期，请重新登录")
    if row.expires_at <= _now():
        raise BizError(ErrorCode.UNAUTHORIZED, "登录已过期，请重新登录")

    if user is None:
        raise BizError(ErrorCode.UNAUTHORIZED, "账号不可用")
    # 走到这里说明令牌本身有效，不存在账号枚举问题，可以据实说明。
    if user.status == int(UserStatus.CLOSED):
        raise BizError(ErrorCode.ACCOUNT_CLOSED)
    if user.status != int(UserStatus.NORMAL):
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
    if user.role in PLATFORM_ROLES:
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


def shop_status_text(status: int) -> str:
    """``Shop.status`` 的中文文案。

    ★ 单开一个函数而不是让调用方 import 我们的 ``models``：
      ``ShopOut`` 只带整数 ``status``，而"这个数字是什么意思"是 account 的事。
      跨模块的合法表面是 ``service``（docs/01 §2）。
    """
    return SHOP_STATUS_TEXT.get(status, str(status))


def shop_is_active(status: int) -> bool:
    """店铺是否处于正常营业状态。

    ★ 给"提问之后、真正取数之前"的复核用（AI 助手在 worker 里会再查一次）：
      提交那一刻店铺是好的，不等于几秒后还是。关店期间不该读到数据。
    """
    return status == SHOP_STATUS_ACTIVE


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

    ★ 只找未注销的账号：给一个已经注销的号发券没有意义，而"能查到"本身
      也会让运营误以为这个号还活着。
    """
    user = await repo.get_live_user_by_phone_hash(session, phone_hash(phone))
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


# ============================================================
# 账号安全（自助）
# ============================================================
async def change_password(
    session: AsyncSession,
    user_id: int,
    *,
    old_password: str,
    new_password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TokenResponse:
    """改密码。**成功后吊销该用户全部 refresh token**，再给当前会话补发一对新的。

    为什么全吊销而不是"除了当前这条"：改密的动机往往就是"怀疑别人在用我的号"，
    留着旧设备上的会话等于没改。给当前会话补发一对，用户自己无感；其它设备
    下次刷新时拿到 401 被登出 —— 那正是想要的效果。

    新密码强度由 ``ChangePasswordRequest`` 校验（与注册同一套规则）。
    """
    user = await _require_usable_user(session, user_id)
    if not verify_password(old_password, user.password_hash):
        raise BizError(ErrorCode.VALIDATION_ERROR, "原密码不正确")
    if old_password == new_password:
        raise BizError(ErrorCode.VALIDATION_ERROR, "新密码不能与原密码相同")

    await repo.update_user_fields(session, user_id, {"password_hash": hash_password(new_password)})
    revoked = await repo.revoke_all_refresh_tokens(session, user_id)
    logger.info("用户修改密码", extra={"userId": user_id, "revokedTokens": revoked})
    return await _issue_tokens(session, user, ip=ip, user_agent=user_agent)


async def close_account(session: AsyncSession, user_id: int, *, password: str) -> None:
    """注销账号：软删 + 匿名化。**不可恢复**。

    ★ **前置守卫不在这里** —— "有没有未完成订单 / 进行中的售后 / 名下有店铺"
      要查 trade 与 aftersale，而 account 不能 import 下游模块的 service
      （docs/01 §2）。编排放路由层，本函数只负责"守卫过了之后"的落地。

    ★ 匿名化清单（改这里要同步 docs/18-account.md 的表）：

    | 位置 | 处理 |
    |---|---|
    | ``account.user`` | 手机号密文/掩码覆写、口令换随机、昵称→「已注销用户」、头像等清空、status=3 |
    | ``account.user_address`` | 全部软删，并覆写姓名/电话/门牌三处强 PII |
    | ``account.refresh_token`` | 全部吊销 |
    | ``trade.order_main`` 收货人快照 | **保留不动** —— 法律/对账凭证（docs/07） |
    | ``review.review`` | **不动** —— 昵称是实时读 user 表的，上面改了昵称，历史评价自动显示「已注销用户」 |
    | 磁盘 ``avatars/{user_id}/`` | 提交后删除（IO 不进事务，见下） |

    ★ ``phone_hash`` **刻意保留原值**：注销后还要能用手机号查出这条记录，才能判断
      "冷静期内不许同号重注册"（见 ``register``）。它不可反查出号码，而且已注销的
      行不在 ``uk_user_phone_hash`` 部分唯一索引里，不会挡住新账号注册。
    """
    user = await _require_usable_user(session, user_id)
    if not verify_password(password, user.password_hash):
        raise BizError(ErrorCode.VALIDATION_ERROR, "密码不正确")

    values: dict[str, object] = {
        # 密文是"能解出真号"的那一份，必须覆写（写一个合法但无意义的密文，
        # 列格式保持不变）。掩码同理。
        "phone_cipher": phone_encrypt("__closed__"),
        "phone_masked": CLOSED_PHONE_MASKED,
        # 口令换成一条谁也不知道的随机串：即使旧口令泄漏也登不进来
        "password_hash": hash_password(secrets.token_urlsafe(32)),
        "nickname": CLOSED_NICKNAME,
        "avatar": None,
        "gender": 0,
        "birthday": None,
        "status": int(UserStatus.CLOSED),
        "closed_at": _now(),
        "ban_reason": None,
    }
    changed = await repo.set_user_status_cas(
        session, user_id, from_status=int(UserStatus.NORMAL), values=values
    )
    if changed == 0:
        # 期间被别的请求封禁 / 注销了（或起始状态并非正常）
        raise BizError(ErrorCode.VALIDATION_ERROR, "账号状态已变化，请刷新后重试")

    addresses = await repo.soft_delete_all_addresses(session, user_id)
    await repo.revoke_all_refresh_tokens(session, user_id)
    await _write_user_flow(
        session,
        user_id=user_id,
        event="CLOSE",
        from_status=int(UserStatus.NORMAL),
        to_status=int(UserStatus.CLOSED),
        operator_type=OperatorType.USER,
        operator_id=str(user_id),
        remark=None,
    )
    # 头像文件在磁盘上：删除是 IO、无法随事务回滚，所以放提交后回调。
    # 回调失败只记日志 —— 残留一个头像文件不影响一致性，也没有重试的意义。
    # ★ 回调必须是**可 await 的**（core/after_commit 会 `await callback()`），
    #   而且磁盘操作要丢进线程，别在事件循环里做阻塞 IO。
    async def _cleanup_avatar() -> None:
        await asyncio.to_thread(_remove_avatar_dir, user_id)

    after_commit.defer(session, _cleanup_avatar)
    logger.info("账号已注销", extra={"userId": user_id, "addresses": addresses})


def _remove_avatar_dir(user_id: int) -> None:
    """删掉 ``media/avatars/{user_id}/`` 整个目录。**只删头像**。

    评价图（``reviews/``）与售后凭证（``aftersale/``）保留 —— 它们挂在评价与
    售后单上，属于交易记录的一部分，不属于"这个人的资料"。

    延迟 import ``storage``：路径规则归 files 模块所有，这里不该自己拼路径；
    放在函数内也让这条磁盘依赖只在真的注销时被引用。
    """
    from app.modules.files import storage

    storage.remove_user_dir(biz="avatars", user_id=user_id)


# ============================================================
# 运营：用户管理
# ============================================================
def _encode_cursor(user_id: int) -> str:
    """游标就是行 id 的 base64。

    列表按 ``id desc`` 翻页，没有排序字段要一起带，所以比订单那种
    ``isoformat|id`` 的复合游标简单。base64 只是让它看起来不像"可以随便猜的
    连续编号"。项目里没有公共游标工具，各模块自带一份（先例：promotion / aftersale）。
    """
    return base64.urlsafe_b64encode(str(user_id).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return int(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, TypeError) as exc:
        raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新加载") from exc


def _to_admin_user_out(user: User, *, is_shop_owner: bool) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        nickname=user.nickname,
        phone=user.phone_masked,
        avatar=user.avatar,
        gender=user.gender,
        role=user.role,
        status=user.status,
        status_text=USER_STATUS_TEXT.get(user.status, "未知"),
        member_level=user.member_level,
        credit_score=user.credit_score,
        is_shop_owner=is_shop_owner,
        register_time=user.register_time,
        closed_at=user.closed_at,
    )


async def admin_list_users(
    session: AsyncSession,
    *,
    status: int | None,
    keyword: str | None,
    cursor: str | None,
    limit: int,
) -> AdminUserListOut:
    """后台用户列表。关键词的分派规则见 ``_looks_like_phone``。

    ``is_shop_owner`` 用**一次**批量查询补齐 —— 一页最多 20 行，别按行查成 N+1。
    """
    phone_h: str | None = None
    nickname: str | None = None
    if keyword and keyword.strip():
        kw = keyword.strip()
        if _looks_like_phone(kw):
            phone_h = phone_hash(kw)
        else:
            nickname = kw

    rows = await repo.list_users(
        session,
        status=status,
        phone_hash=phone_h,
        nickname=nickname,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=limit,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    owners = await repo.list_shop_owner_ids(session, [u.id for u in page])
    return AdminUserListOut(
        items=[_to_admin_user_out(u, is_shop_owner=u.id in owners) for u in page],
        next_cursor=_encode_cursor(page[-1].id) if has_more and page else None,
        has_more=has_more,
    )


async def admin_get_user(session: AsyncSession, user_id: int) -> AdminUserDetailOut:
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    owners = await repo.list_shop_owner_ids(session, [user_id])
    base = _to_admin_user_out(user, is_shop_owner=user_id in owners)
    return AdminUserDetailOut(**base.model_dump(), ban_reason=user.ban_reason)


async def ban_user(session: AsyncSession, *, admin_id: int, user_id: int, reason: str) -> None:
    """封禁（第一版**只禁登录**：不拦下单/评价，见 docs/18-account.md §3）。

    ★ 三条守卫：不能封自己、不能封平台账号、已注销的不必封。前两条是为了让
      "一次误操作锁死平台自己"不可能发生 —— 平台账号没有第二条救济路径。

    生效方式是"登录/刷新被拒" + 吊销全部 refresh token；**已经签发的 access token
    在最长 30 分钟内仍然有效**（认证依赖不查库，见 docs/18-account.md §3）。
    """
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    if user.id == admin_id:
        raise BizError(ErrorCode.VALIDATION_ERROR, "不能封禁自己")
    if user.role in PLATFORM_ROLES:
        raise BizError(ErrorCode.FORBIDDEN, "不能封禁平台账号")
    if user.status == int(UserStatus.CLOSED):
        raise BizError(ErrorCode.VALIDATION_ERROR, "该账号已注销，无需封禁")

    changed = await repo.set_user_status_cas(
        session,
        user_id,
        from_status=int(UserStatus.NORMAL),
        values={"status": int(UserStatus.BANNED), "ban_reason": reason},
    )
    if changed == 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "该账号当前不是正常状态")

    revoked = await repo.revoke_all_refresh_tokens(session, user_id)
    await _write_user_flow(
        session,
        user_id=user_id,
        event="BAN",
        from_status=int(UserStatus.NORMAL),
        to_status=int(UserStatus.BANNED),
        operator_type=OperatorType.PLATFORM,
        operator_id=str(admin_id),
        remark=reason,
    )
    logger.warning(
        "用户被封禁", extra={"userId": user_id, "operatorId": admin_id, "revokedTokens": revoked}
    )


async def unban_user(session: AsyncSession, *, admin_id: int, user_id: int) -> None:
    """解封。**不恢复任何令牌** —— 用户用密码重新登录即可。

    注销是终态：已注销的账号解不了（status=3 不在 CAS 的 ``from_status`` 里）。
    """
    changed = await repo.set_user_status_cas(
        session,
        user_id,
        from_status=int(UserStatus.BANNED),
        values={"status": int(UserStatus.NORMAL), "ban_reason": None},
    )
    if changed == 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "该账号当前不是封禁状态")

    await _write_user_flow(
        session,
        user_id=user_id,
        event="UNBAN",
        from_status=int(UserStatus.BANNED),
        to_status=int(UserStatus.NORMAL),
        operator_type=OperatorType.PLATFORM,
        operator_id=str(admin_id),
        remark=None,
    )
    logger.info("用户已解封", extra={"userId": user_id, "operatorId": admin_id})


async def reset_password_by_admin(session: AsyncSession, *, admin_id: int, user_id: int) -> str:
    """运营把用户重置为一个随机临时口令，**一次性明文返回**。

    ★ 它意味着运营短暂地知道这个口令 —— 没有短信通道时这是唯一的兜底手段
      （docs/16 §6）。所以：只显示一次、提示用户登录后立即自行修改、并留审计。
      本期**不做**"强制下次登录改密"（写在 docs/18-account.md 的明确边界里）。

    同时吊销该用户全部 refresh token：重置的意义就是"把当前登录者请出去"。
    """
    user = await repo.get_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    if user.role in PLATFORM_ROLES:
        raise BizError(ErrorCode.FORBIDDEN, "不能重置平台账号的密码")
    if user.status == int(UserStatus.CLOSED):
        raise BizError(ErrorCode.VALIDATION_ERROR, "该账号已注销，无法重置密码")

    temp = generate_temp_password()
    await repo.update_user_fields(session, user_id, {"password_hash": hash_password(temp)})
    revoked = await repo.revoke_all_refresh_tokens(session, user_id)
    await _write_user_flow(
        session,
        user_id=user_id,
        event="RESET_PASSWORD",
        from_status=user.status,
        to_status=user.status,
        operator_type=OperatorType.PLATFORM,
        operator_id=str(admin_id),
        remark=None,
    )
    logger.warning(
        "运营重置了用户密码",
        extra={"userId": user_id, "operatorId": admin_id, "revokedTokens": revoked},
    )
    return temp


# 供其他模块复用的格式化函数
__all__ = [
    "admin_get_user",
    "admin_list_users",
    "ban_user",
    "change_password",
    "close_account",
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
    "reset_password_by_admin",
    "unban_user",
    "update_address",
    "update_me",
    "update_my_shop",
]
