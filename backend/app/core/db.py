"""数据库引擎与会话。

会话/事务边界约定（docs/07-order-and-split.md §4.4）：
    路由层用 ``async with session.begin():`` 包住整个业务操作，
    service 函数接收 session、内部不 commit。
    这样一个请求里的多次 service 调用能组合进同一个事务 ——
    这正是"模块化单体"相对微服务的最大红利：下单、预占库存、锁券、
    写 outbox 全在一个本地事务里，不需要分布式事务。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            s.database_url,
            echo=s.db_echo,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_recycle=s.db_pool_recycle_seconds,
            pool_pre_ping=True,
            connect_args={
                # 全站统一 UTC（docs/13-schema.md §0.2）；
                # application_name 让 pg_stat_activity 里能一眼看出是谁的连接
                "server_settings": {"timezone": "UTC", "application_name": "eshop-api"}
            },
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            # 提交后仍可读取对象属性，省掉一次刷新查询
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级会话，**由它统一管理事务**。

    一个请求 = 一个事务：成功提交，异常回滚。

    为什么不把 ``async with session.begin()`` 写进路由：依赖（如
    "取当前店铺"）也会查库，SQLAlchemy 的 autobegin 会先把事务开起来，
    路由再调 ``session.begin()`` 就会抛 "A transaction is already begun"。
    把事务边界收到这一层，路由和依赖都能自由读写同一个事务。

    这也正是模块化单体相对微服务的最大红利：下单、预占库存、锁券、
    写 outbox 全在一个本地事务里（docs/07-order-and-split.md）。
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
