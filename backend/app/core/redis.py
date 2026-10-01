"""Redis 连接与 Lua 脚本注册。

Redis 在本系统里承担四种角色（docs/14-redis-keys.md）：
1. 库存/券的"闸门"（高并发拦截）
2. 缓存
3. 幂等键
4. Redis Streams 事件 + ARQ 任务队列

超时设得很短（默认 500ms）：Redis 卡住时应用要**快速失败并走降级**，
而不是把请求线程都挂在那里等。
"""

from __future__ import annotations

from pathlib import Path

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from app.core.config import get_settings

# backend/lua/ —— 从 app/core/redis.py 往上三层
LUA_DIR = Path(__file__).resolve().parents[2] / "lua"

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        s = get_settings()
        _client = aioredis.from_url(
            s.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=s.redis_max_connections,
            socket_timeout=s.redis_socket_timeout,
            socket_connect_timeout=s.redis_socket_timeout,
            health_check_interval=30,
        )
    return _client


class LuaScripts:
    """所有 Lua 脚本的注册处（docs/14 §3）。

    ``register_script`` 返回的对象调用时走 ``EVALSHA``；脚本缓存丢失
    （Redis 重启、``SCRIPT FLUSH``）导致 ``NOSCRIPT`` 时，redis-py 会
    自动退回 ``EVAL`` 并把脚本重新装进缓存 —— 所以调用方不用关心缓存状态。

    ★ 脚本里的**时间戳一律由应用传入**，不要用 ``redis.call('TIME')``：
    脚本执行期间 Redis 是阻塞的，用 Redis 自己的时钟会让"同一批操作
    看到不同时间"，排查时序问题时很麻烦（docs/04 §4.1）。
    """

    def __init__(self, redis: Redis) -> None:
        def load(name: str) -> AsyncScript:
            return redis.register_script((LUA_DIR / f"{name}.lua").read_text(encoding="utf-8"))

        # 库存
        self.stock_lock = load("stock_lock")
        self.stock_lock_batch = load("stock_lock_batch")
        self.stock_release = load("stock_release")
        self.stock_release_batch = load("stock_release_batch")
        self.stock_confirm = load("stock_confirm")

        # 优惠券
        self.coupon_receive = load("coupon_receive")
        self.coupon_lock = load("coupon_lock")

        # 后续模块（秒杀排队）实现后在这里追加：
        #   self.seckill_enqueue = load("seckill_enqueue")


_lua: LuaScripts | None = None


def get_lua() -> LuaScripts:
    global _lua
    if _lua is None:
        _lua = LuaScripts(get_redis())
    return _lua


async def close_redis() -> None:
    global _client, _lua
    if _client is not None:
        await _client.aclose()
    _client = None
    # 脚本对象持有已关闭的连接，必须一起丢弃
    _lua = None


async def ping_redis() -> bool:
    """健康检查用。异常一律吞掉，由调用方决定怎么报告。"""
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False
