"""account 模块的数据访问层。

只碰 ``account`` schema 下的表。所有函数都接收外部传入的 session，
不自己 commit —— 事务边界由路由层统一控制。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.account.models import RefreshToken, Shop, ShopMember, User, UserAddress


# ============================================================
# 用户
# ============================================================
async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_phone_hash(
    session: AsyncSession, phone_hash: str, *, for_update: bool = False
) -> User | None:
    """按手机号哈希查用户。登录时用 for_update 锁行，避免并发改失败计数。"""
    stmt = select(User).where(User.phone_hash == phone_hash)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


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
