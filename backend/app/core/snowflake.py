"""雪花 ID 生成。

结构（docs/13-schema.md §0.4）::

    1 bit 符号位(0) | 41 bit 毫秒时间戳 | 10 bit worker id | 12 bit 序列

worker id 为什么要**向 Redis 租**：Uvicorn 多 worker、docker compose
--scale 多容器时，环境变量区分不了进程。启动时抢占一个 ``worker:{i}``
键（``SET NX EX``），拿到就用，拿不到就试下一个；运行期定期续期，
续期失败说明租约被别人抢走，本进程必须停止发号（宁可重启也不能重复 ID）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time

from redis.asyncio import Redis

from app.core import redis_keys
from app.core.logging import get_logger

logger = get_logger(__name__)

# 自定义纪元：2026-01-01T00:00:00Z，41 位时间戳可用到 2095 年
EPOCH_MS = 1_767_225_600_000
WORKER_BITS = 10
SEQUENCE_BITS = 12
MAX_WORKER_ID = (1 << WORKER_BITS) - 1  # 1023
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1  # 4095

WORKER_ID_TTL_SECONDS = 60
RENEW_INTERVAL_SECONDS = 20
MAX_CLOCK_BACKWARDS_MS = 5


class ClockBackwardsError(RuntimeError):
    """系统时钟回拨过多，拒绝发号，避免产生重复 ID。"""


class SnowflakeGenerator:
    def __init__(self, worker_id: int) -> None:
        if not 0 <= worker_id <= MAX_WORKER_ID:
            raise ValueError(f"worker_id 必须在 0..{MAX_WORKER_ID} 之间，当前 {worker_id}")
        self.worker_id = worker_id
        self._sequence = 0
        self._last_ms = -1

    def next_id(self) -> int:
        """生成一个 ID。

        纯计算、无 await。事件循环是单线程的，同一进程内不会有并发问题；
        多进程之间靠不同的 worker id 隔离。
        """
        now_ms = int(time.time() * 1000)

        if now_ms < self._last_ms:
            drift = self._last_ms - now_ms
            if drift > MAX_CLOCK_BACKWARDS_MS:
                raise ClockBackwardsError(f"时钟回拨 {drift}ms，拒绝发号")
            # 小幅回拨：等到时间追上再发
            while now_ms < self._last_ms:
                now_ms = int(time.time() * 1000)

        if now_ms == self._last_ms:
            self._sequence = (self._sequence + 1) & MAX_SEQUENCE
            if self._sequence == 0:
                # 当前毫秒的 4096 个序列号用尽，等下一毫秒
                while now_ms <= self._last_ms:
                    now_ms = int(time.time() * 1000)
        else:
            self._sequence = 0

        self._last_ms = now_ms
        return (
            ((now_ms - EPOCH_MS) << (WORKER_BITS + SEQUENCE_BITS))
            | (self.worker_id << SEQUENCE_BITS)
            | self._sequence
        )


def _holder_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def lease_worker_id(redis: Redis) -> int:
    """启动时抢一个 worker id。已经是自己的租约则直接续期。"""
    holder = _holder_name()
    for worker_id in range(MAX_WORKER_ID + 1):
        key = redis_keys.snowflake_worker(worker_id)
        if await redis.set(key, holder, nx=True, ex=WORKER_ID_TTL_SECONDS):
            logger.info("已租用雪花 worker id", extra={"workerId": worker_id})
            return worker_id
        if await redis.get(key) == holder:
            await redis.expire(key, WORKER_ID_TTL_SECONDS)
            return worker_id

    raise RuntimeError(f"没有可用的雪花 worker id（{MAX_WORKER_ID + 1} 个全部被占用）")


async def keep_worker_id_alive(redis: Redis, worker_id: int) -> None:
    """续期循环。租约丢失时抛异常终止进程 —— 重复 ID 比服务中断严重得多。"""
    key = redis_keys.snowflake_worker(worker_id)
    holder = _holder_name()
    while True:
        await asyncio.sleep(RENEW_INTERVAL_SECONDS)
        if await redis.get(key) != holder:
            raise RuntimeError(f"雪花 worker id {worker_id} 的租约已丢失，进程需要重启")
        await redis.expire(key, WORKER_ID_TTL_SECONDS)


_generator: SnowflakeGenerator | None = None
_lease_task: asyncio.Task[None] | None = None


async def start_snowflake(redis: Redis) -> SnowflakeGenerator:
    """应用启动时调用：租 worker id 并起续期协程。"""
    global _generator, _lease_task
    worker_id = await lease_worker_id(redis)
    _generator = SnowflakeGenerator(worker_id)
    _lease_task = asyncio.create_task(keep_worker_id_alive(redis, worker_id))
    return _generator


async def stop_snowflake() -> None:
    global _generator, _lease_task
    if _lease_task is not None:
        _lease_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _lease_task
    _lease_task = None
    _generator = None


def next_id() -> int:
    """全局取号入口。未初始化就调用属于编码错误，直接抛。"""
    if _generator is None:
        raise RuntimeError("雪花生成器尚未初始化，请先 await start_snowflake(redis)")
    return _generator.next_id()
