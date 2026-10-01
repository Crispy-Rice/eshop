"""Redis 侧的库存操作：分片选择、回源初始化、售罄标记。

这一层只负责把 **DB 的真值搬进 Redis** 以及读 Redis 的状态，**不做业务变更**——
预占/回补等变更走 Lua 脚本（`app/core/redis.py` 的 `get_lua()`），
DB 侧变更走 `service.py`。三层职责分明，出问题时能一眼定位是哪层。

分片的现状（详见 ``config.inventory_shard_count`` 的说明）：
分片把可售量拆成 N 份，而跨片不凑整，所以 N > 1 时"超过单片余量的订单"
会失败。单实例 Redis 上分片换不来任何收益（Lua 脚本本就串行），
所以**默认 N = 1**；多分片的代码路径保留，供将来切 Redis Cluster 时启用。
"""

from __future__ import annotations

import hashlib
import time

from redis.asyncio import Redis
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_keys as rk
from app.core.config import get_settings
from app.modules.inventory.models import SkuStock

# 回源互斥锁的 TTL（docs/14 §2.6）。取得很短：它只保护"读一次 DB 再写 Redis"
# 这一小段，卡住也不能让别人一直等
INIT_LOCK_TTL = 3


def shard_count() -> int:
    return get_settings().inventory_shard_count


def shard_index(sku_id: int, user_id: int, offset: int = 0) -> int:
    """用 ``(skuId, userId)`` 散列决定落在哪一片。

    ★ 用 userId 参与散列而不是纯随机：同一用户固定落同一片，只有该片售罄
    才失败；纯随机会出现"A 第一次落 shard 3 失败、第二次落 shard 5 成功"，
    体验不一致且难以排查（docs/03 §3.3）。

    ``offset`` 用于"本片不足时按序回落其他片"——**整片扣，不跨片凑**
    （docs/03 §10）。

    用 blake2b 而不是内置 ``hash()``：后者对 str 是按进程随机加盐的，
    同一个 sku 在不同 worker 进程会散到不同片，分片就失去意义了。
    """
    digest = hashlib.blake2b(f"{sku_id}:{user_id}".encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") + offset) % shard_count()


def shard_key(sku_id: int, wh_id: int, user_id: int, offset: int = 0) -> str:
    """该用户本次下单应该扣哪个分片。"""
    return rk.stock_shard(sku_id, wh_id, shard_index(sku_id, user_id, offset))


def all_shard_keys(sku_id: int, wh_id: int) -> list[str]:
    return [rk.stock_shard(sku_id, wh_id, i) for i in range(shard_count())]


async def read_available(redis: Redis, sku_id: int, wh_id: int) -> int | None:
    """求和展示给用户（"剩余 37 件"）。

    任一分片不存在就返回 ``None`` 表示未初始化 —— 不能把缺失当成 0，
    否则会把"没加载"误判成"售罄"。
    """
    values = await redis.mget(all_shard_keys(sku_id, wh_id))
    if any(v is None for v in values):
        return None
    return sum(int(v) for v in values)


async def push_shards(redis: Redis, sku_id: int, wh_id: int, available: int) -> None:
    """把可售量均摊到各分片。

    余数给前面的分片，保证各分片求和**恰好等于** ``available``——
    对账任务依赖这个等式判断漂移。
    """
    n = shard_count()
    base, remainder = divmod(available, n)
    pipe = redis.pipeline()
    for i in range(n):
        pipe.set(rk.stock_shard(sku_id, wh_id, i), base + (1 if i < remainder else 0))
    await pipe.execute()


async def write_meta(redis: Redis, row: SkuStock | Row) -> None:
    """把 DB 的四个数量同步到 meta hash，供对账比对。"""
    await redis.hset(
        rk.stock_meta(row.sku_id, row.warehouse_id),
        mapping={
            "total": row.total,
            "available": row.available,
            "locked": row.locked,
            "frozen": row.frozen,
            "version": row.version,
            "syncTs": int(time.time() * 1000),
        },
    )


async def ensure_loaded(redis: Redis, session: AsyncSession, sku_id: int, wh_id: int) -> bool:
    """分片不存在时从 **DB 回源**加载。返回是否已就绪。

    Lua 脚本对缺失的分片返回 ``-1``，调用方据此调用本函数然后**重试一次**。

    用 ``lock:stock_init:{skuId}:{whId}`` 做互斥：热点商品首次上架时会有大量
    并发请求同时发现"未初始化"，不加锁就会各自读一遍 DB 写一遍 Redis。
    抢不到锁的直接返回 False，让调用方稍后重试（别人马上就写好了）。
    """
    probe = rk.stock_shard(sku_id, wh_id, 0)
    if await redis.exists(probe):
        return True

    lock_key = rk.stock_init_lock(sku_id, wh_id)
    if not await redis.set(lock_key, "1", nx=True, ex=INIT_LOCK_TTL):
        # 别人正在回源，调用方稍后重试即可
        return False

    try:
        # 双重检查：等锁期间可能已经被别人写好了
        if await redis.exists(probe):
            return True

        # 用原生 SQL 而不是 ORM：调用方可能刚用原生 UPDATE 改过这一行，
        # 而 ORM 的 identity map 那时还是旧值（见 ``service._sync_one_to_redis``）
        row = (
            await session.execute(
                text(
                    "SELECT sku_id, warehouse_id, total, available, locked, frozen, version "
                    "FROM inventory.sku_stock "
                    "WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id"
                ),
                {"sku_id": sku_id, "warehouse_id": wh_id},
            )
        ).one_or_none()
        if row is None:
            # 该 SKU 在这个仓没有库存记录，属于业务上的"不存在"
            return False

        await push_shards(redis, sku_id, wh_id, row.available)
        await write_meta(redis, row)
        return True
    finally:
        await redis.delete(lock_key)


async def mark_sold_out_if_empty(redis: Redis, sku_id: int, wh_id: int) -> bool:
    """全部分片为 0 时打售罄标记。

    由调用方在脚本返回**之后**检查，不放进 Lua 脚本里：售罄标记的 key 与分片
    是同一个 hash tag 下的兄弟 key，放进去会让脚本的 KEYS 列表翻倍
    （docs/14 §3.2）。
    """
    available = await read_available(redis, sku_id, wh_id)
    if available is not None and available <= 0:
        await redis.set(
            rk.stock_zero(sku_id, wh_id),
            1,
            ex=get_settings().inventory_zero_marker_ttl,
        )
        return True
    return False


async def clear_sold_out(redis: Redis, sku_id: int, wh_id: int) -> None:
    """回补之后必须在 Redis 侧清掉售罄标记，否则该 SKU 会被一直快速拒绝。

    DB 侧回补了但 Redis 标记没清 → 用户看到"已售罄"而实际有货（少卖），
    这是对账任务会修正的那一类漂移。
    """
    await redis.delete(rk.stock_zero(sku_id, wh_id))


async def rebuild_from_db(redis: Redis, row: SkuStock | Row) -> None:
    """用 DB 的值**覆盖** Redis（``SET`` 而非 ``INCR``）。

    对账修正与每日全量重建都用它。方向规则见 docs/03 §7：
    Redis 大于 DB 会超卖、小于 DB 会少卖，**两种都以 DB 为准**——DB 是账本。

    ``row`` 可以是 ORM 对象，也可以是原生 SQL 返回的 ``Row`` —— 后者用于
    刚被原生 UPDATE 改过的行，ORM 的 identity map 那时还是旧值（见
    ``service._sync_one_to_redis`` 的说明）。
    """
    await push_shards(redis, row.sku_id, row.warehouse_id, row.available)
    await write_meta(redis, row)
    if row.available <= 0:
        await mark_sold_out_if_empty(redis, row.sku_id, row.warehouse_id)
    else:
        await clear_sold_out(redis, row.sku_id, row.warehouse_id)
