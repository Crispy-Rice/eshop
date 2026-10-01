"""事务提交后执行的回调。

**为什么需要它**：有些副作用既不能放在事务里，也不能放在提交前。

- 放在事务里：Redis 操作、投递队列**无法随事务回滚**。先回补 Redis 再提交，
  事务一失败 Redis 就已经多出库存 —— 直接超卖（docs/07 §7.3）。
- 放在提交前：事务失败时副作用已经发生。队列里多一个指向不存在订单的任务，
  通知发出去而订单不存在。

做法：把回调注册到 ``session.info``，``get_session`` 在 **commit 成功之后**
统一执行；回滚则整批丢弃。这样"业务变更提交"与"副作用发生"是同向的。

回调**不参与事务**，所以它必须自己是幂等的 —— 进程在提交后、回调执行前崩溃时，
回调会丢失。因此这类回调只用来做「提前量」（比如提前投递超时关单任务），
真正的正确性永远由兜底的定时扫描 + 幂等函数保证。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

_KEY = "after_commit"


def defer(session: AsyncSession, callback: Callable[[], Awaitable[Any]]) -> None:
    """注册一个提交后回调。多次注册按注册顺序执行。"""
    session.info.setdefault(_KEY, []).append(callback)


async def run_pending(session: AsyncSession) -> None:
    """执行并清空待执行的回调。由 ``get_session`` 在 commit 之后调用。"""
    callbacks: list[Callable[[], Awaitable[Any]]] = session.info.pop(_KEY, [])
    for callback in callbacks:
        await callback()


def discard_pending(session: AsyncSession) -> None:
    """丢弃待执行的回调。事务回滚时调用 —— 业务没提交，副作用也不该发生。"""
    session.info.pop(_KEY, None)
