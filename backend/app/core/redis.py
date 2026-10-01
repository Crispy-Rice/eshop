"""Redis 连接。

Redis 在本系统里承担四种角色（docs/14-redis-keys.md）：
1. 库存/券的"闸门"（高并发拦截）
2. 缓存
3. 幂等键
4. Redis Streams 事件 + ARQ 任务队列

超时设得很短（默认 500ms）：Redis 卡住时应用要**快速失败并走降级**，
而不是把请求线程都挂在那里等。
"""

from __future__ import annotations

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import get_settings

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


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def ping_redis() -> bool:
    """健康检查用。异常一律吞掉，由调用方决定怎么报告。"""
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False
