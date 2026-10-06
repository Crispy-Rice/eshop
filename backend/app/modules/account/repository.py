"""account 模块的数据访问层。

只碰 ``account`` schema 下的表。所有函数都接收外部传入的 session，
不自己 commit —— 事务边界由路由层统一控制。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserStatus
from app.modules.account.models import (
    RefreshToken,
    Shop,
    ShopMember,
    User,
    UserAddress,
    UserStateFlow,
)


# ============================================================
# 用户
# ============================================================
async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_live_user_by_phone_hash(
    session: AsyncSession, phone_hash: str, *, for_update: bool = False
) -> User | None:
    """按手机号哈希查**未注销**的用户。

    ★ 必须显式排除已注销行。注销是终态，而同一个号在冷静期过后可以被**重新注册**
      —— 那时同一个 ``phone_hash`` 下会同时存在"活跃的新账号"与"已注销的旧账号"。
      若还按手机号盲查，``scalar()`` 取到哪一行是不确定的，登录会莫名其妙地报
      "账号已注销"。``uk_user_phone_hash`` 是 ``WHERE status <> 3`` 的部分唯一索引，
      加了这个条件之后**至多一行**，结果确定。

    登录用 ``for_update`` 时要留意不能与"独立事务写失败计数"撞锁，
    见 ``service._record_login_failure``。
    """
    stmt = select(User).where(
        User.phone_hash == phone_hash, User.status != int(UserStatus.CLOSED)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def get_closed_user_by_phone_hash(session: AsyncSession, phone_hash: str) -> User | None:
    """按手机号哈希查**已注销**的记录，取最近一条。

    注册流程靠它判断冷静期。注销时**刻意保留** ``phone_hash`` 原值，正是为了让
    这里查得到 —— 详见 ``service.register`` 里对"为什么不像其他列那样覆写"的说明。
    """
    return await session.scalar(
        select(User)
        .where(User.phone_hash == phone_hash, User.status == int(UserStatus.CLOSED))
        .order_by(User.id.desc())
        .limit(1)
    )


async def insert_user(session: AsyncSession, user: User) -> User:
    session.add(user)
    await session.flush()
    return user


async def touch_login_success(session: AsyncSession, user_id: int, password_hash: str | None) -> None:
    """登录成功：清零失败计数、解锁，必要时顺带升级密码哈希。"""
    values: dict[str, object] = {
        "failed_logins": 0,
        "locked_until": None,
        "updated_at": func.now(),
    }
    if password_hash is not None:
        values["password_hash"] = password_hash
    await session.execute(update(User).where(User.id == user_id).values(**values))


async def increment_login_failure(
    session: AsyncSession, user_id: int, *, max_attempts: int, lock_minutes: int
) -> tuple[int, datetime | None]:
    """原子地累加登录失败次数，达到阈值时上锁。返回 (失败次数, 锁定到期时间)。

    用**一条 UPDATE** 完成"读 + 判断 + 写"：
    - 并发下不会丢计数（不是先 SELECT 再 UPDATE）
    - 调用方不需要对用户行加 FOR UPDATE，避免和"独立事务写计数"互相等锁

    ``make_interval`` 的参数顺序是 (years, months, weeks, days, hours, mins, secs)，
    这里用到第 6 个 mins。
    """
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(
            failed_logins=User.failed_logins + 1,
            locked_until=case(
                (
                    User.failed_logins + 1 >= max_attempts,
                    func.now() + func.make_interval(0, 0, 0, 0, 0, lock_minutes),
                ),
                # 已经锁着就保持原样，不因为又失败一次而延长或清除
                else_=User.locked_until,
            ),
            updated_at=func.now(),
        )
        .returning(User.failed_logins, User.locked_until)
    )
    row = (await session.execute(stmt)).one()
    return int(row.failed_logins), row.locked_until


async def update_user_fields(session: AsyncSession, user_id: int, values: dict[str, object]) -> None:
    """只更新传入的字段。service 负责决定哪些字段可以改。"""
    if not values:
        return
    values["updated_at"] = func.now()
    await session.execute(update(User).where(User.id == user_id).values(**values))


async def set_user_status_cas(
    session: AsyncSession,
    user_id: int,
    *,
    from_status: int,
    values: dict[str, object],
) -> int:
    """状态迁移的 CAS：只有当前状态等于 ``from_status`` 才写，返回受影响行数。

    ★ 封禁 / 解封 / 注销**共用它**，于是三者互斥且并发安全：重复封禁、封一个
      已注销的号、解封一个正常号，都会拿到 0 行（service 据此报明确的错），
      而不是把状态悄悄改坏。先例：``aftersale.cas_status``、``trade.mark_paid``。

    ``values`` 里必须带上新的 ``status``（以及这一步要一并写的其他列）。
    """
    stmt = (
        update(User)
        .where(User.id == user_id, User.status == from_status)
        .values(**values, updated_at=func.now())
    )
    return (await session.execute(stmt)).rowcount or 0


# ============================================================
# 刷新令牌
# ============================================================
async def insert_refresh_token(session: AsyncSession, token: RefreshToken) -> None:
    session.add(token)
    await session.flush()


async def get_refresh_token(
    session: AsyncSession, token_hash: str, *, for_update: bool = False
) -> RefreshToken | None:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def revoke_refresh_token(session: AsyncSession, token_id: int) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.id == token_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )


async def revoke_all_refresh_tokens(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    return result.rowcount or 0


async def purge_expired_refresh_tokens(session: AsyncSession, keep_days: int = 30) -> int:
    """清理已过期超过保留期的令牌。由 worker 的 cron 调用。

    make_interval 的参数顺序是 (years, months, weeks, days, hours, mins, secs)，
    这里只用到第 4 个 days。
    """
    result = await session.execute(
        delete(RefreshToken).where(
            RefreshToken.expires_at < func.now() - func.make_interval(0, 0, 0, keep_days)
        )
    )
    return result.rowcount or 0


# ============================================================
# 店铺
# ============================================================
async def get_shop_by_owner(session: AsyncSession, owner_user_id: int) -> Shop | None:
    return await session.scalar(select(Shop).where(Shop.owner_user_id == owner_user_id).limit(1))


async def get_shop_by_id(session: AsyncSession, shop_id: int) -> Shop | None:
    return await session.get(Shop, shop_id)


async def list_shops_by_ids(session: AsyncSession, shop_ids: Sequence[int]) -> list[Shop]:
    if not shop_ids:
        return []
    return list(await session.scalars(select(Shop).where(Shop.id.in_(shop_ids))))


async def list_shop_owner_ids(session: AsyncSession, user_ids: Sequence[int]) -> set[int]:
    """这批用户里，哪些名下有店铺。

    后台用户列表要标出来 —— 店主不能自助注销（没有"停用店铺"机制），
    运营看到标记才知道该先让商家处理店铺。一次查询搞定，别按行查成 N+1。
    """
    if not user_ids:
        return set()
    rows = await session.scalars(
        select(Shop.owner_user_id).where(Shop.owner_user_id.in_(user_ids))
    )
    return set(rows)


async def search_shops(session: AsyncSession, *, keyword: str | None, limit: int) -> list[Shop]:
    """按名称模糊搜店铺，最新在前。

    ★ 给运营侧的"指定店铺"选择器用，**刻意不做游标分页**：那是个"输入关键词收窄"
      的选择器，不是列表页 —— 在选择器里翻页没有意义，多打两个字比翻页快。
      所以只给它 ``keyword`` 加一个结果上限。

    不按状态过滤：关掉的店也列出来，由界面标出"已关闭"。
    过滤掉反而会让运营搜不到自己的店，且说不清为什么。
    """
    stmt = select(Shop)
    if keyword:
        stmt = stmt.where(Shop.name.ilike(f"%{keyword}%"))
    return list(await session.scalars(stmt.order_by(Shop.id.desc()).limit(limit)))


async def list_users_by_ids(session: AsyncSession, user_ids: Sequence[int]) -> list[User]:
    """批量取用户。评价列表要展示昵称与头像，逐个查就是 N+1。"""
    if not user_ids:
        return []
    return list(await session.scalars(select(User).where(User.id.in_(user_ids))))


async def insert_shop(session: AsyncSession, shop: Shop) -> Shop:
    session.add(shop)
    await session.flush()
    return shop


async def insert_shop_member(session: AsyncSession, member: ShopMember) -> None:
    session.add(member)
    await session.flush()


async def update_shop_fields(session: AsyncSession, shop_id: int, values: dict[str, object]) -> None:
    """只更新传入的字段。service 负责决定哪些字段可以改。"""
    if not values:
        return
    values["updated_at"] = func.now()
    await session.execute(update(Shop).where(Shop.id == shop_id).values(**values))


# ============================================================
# 收货地址
# ============================================================
async def list_addresses(session: AsyncSession, user_id: int) -> list[UserAddress]:
    """默认地址排在最前，其余按创建时间倒序。"""
    result = await session.scalars(
        select(UserAddress)
        .where(UserAddress.user_id == user_id, UserAddress.status == 1)
        .order_by(UserAddress.is_default.desc(), UserAddress.created_at.desc())
    )
    return list(result)


async def get_address(session: AsyncSession, address_id: int, user_id: int) -> UserAddress | None:
    """按 (id, user_id) 取，一次查询同时完成归属校验。

    越权访问返回 None，由上层转成 NOT_FOUND —— 不区分"不存在"和"不是你的"
    （docs/15-api-and-errors.md §3.3）。
    """
    return await session.scalar(
        select(UserAddress).where(
            UserAddress.id == address_id,
            UserAddress.user_id == user_id,
            UserAddress.status == 1,
        )
    )


async def clear_default_address(session: AsyncSession, user_id: int) -> None:
    await session.execute(
        update(UserAddress)
        .where(UserAddress.user_id == user_id, UserAddress.is_default.is_(True), UserAddress.status == 1)
        .values(is_default=False, updated_at=func.now())
    )


async def insert_address(session: AsyncSession, address: UserAddress) -> UserAddress:
    session.add(address)
    await session.flush()
    return address


async def update_address(session: AsyncSession, address_id: int, values: dict[str, object]) -> None:
    values["updated_at"] = func.now()
    await session.execute(update(UserAddress).where(UserAddress.id == address_id).values(**values))


async def soft_delete_address(session: AsyncSession, address_id: int, user_id: int) -> int:
    """软删。历史订单里的地址是快照，删除地址不影响订单。"""
    result = await session.execute(
        update(UserAddress)
        .where(UserAddress.id == address_id, UserAddress.user_id == user_id, UserAddress.status == 1)
        .values(status=2, is_default=False, updated_at=func.now())
    )
    return result.rowcount or 0


async def count_addresses(session: AsyncSession, user_id: int) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(UserAddress)
            .where(UserAddress.user_id == user_id, UserAddress.status == 1)
        )
    ) or 0


# ============================================================
# 运营端：用户管理与状态流水
# ============================================================
async def list_users(
    session: AsyncSession,
    *,
    status: int | None,
    phone_hash: str | None,
    nickname: str | None,
    cursor: int | None,
    limit: int,
) -> list[User]:
    """运营用户列表，最新在前。

    ★ 关键词的**分派在 service**，这里只收"已经确定的条件"：手机号只能走
      ``phone_hash`` 等值匹配（库里存的是 HMAC，没法 LIKE），只有昵称才能模糊搜。
      让 repository 知道怎么哈希是越界的。

    ★ 多取一条：调用方用"返回条数 > limit"判断 ``hasMore``，
      不用额外跑一次 count，也不会在正好取满时误判成还有下一页。
    """
    stmt: Select = select(User)
    if status is not None:
        stmt = stmt.where(User.status == status)
    if phone_hash is not None:
        stmt = stmt.where(User.phone_hash == phone_hash)
    if nickname is not None:
        stmt = stmt.where(User.nickname.ilike(f"%{nickname}%"))
    if cursor is not None:
        stmt = stmt.where(User.id < cursor)
    stmt = stmt.order_by(User.id.desc()).limit(limit + 1)
    return list(await session.scalars(stmt))


async def soft_delete_all_addresses(session: AsyncSession, user_id: int) -> int:
    """注销时清空该用户的全部收货地址（返回处理条数）。

    ★ 顺带把三条**强 PII** 覆写掉（收货人姓名 / 电话 / 门牌）：地址行虽然已经
      软删、应用侧不会再读，但明文 PII 留在库里就还是 PII。省市区保留 ——
      它们是行政区划、不指向个人，留着便于日后核对运费口径。
    """
    result = await session.execute(
        update(UserAddress)
        .where(UserAddress.user_id == user_id, UserAddress.status == 1)
        .values(
            status=2,
            is_default=False,
            receiver_name="",
            phone="",
            detail="",
            updated_at=func.now(),
        )
    )
    return result.rowcount or 0


async def insert_user_state_flow(session: AsyncSession, flow: UserStateFlow) -> None:
    session.add(flow)
    await session.flush()
