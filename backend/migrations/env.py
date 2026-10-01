"""Alembic 环境（async）。

与项目约定的关系（docs/13-schema.md §0.3）：
- 连接串从应用配置读取，**不写在 alembic.ini 里**，避免密钥进版本库
- 迁移用 `MIGRATION_DATABASE_URL`（eshop_owner，有 DDL 权限）；
  应用运行角色 eshop_app 没有 DDL 权限，就算出问题也删不了表
- 表分散在 14 个 schema 里，所以开启 include_schemas，把 alembic_version
  单独放在 public
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的比对基准：所有在 app/models.py 里登记过的模型
target_metadata = Base.metadata

_settings = get_settings()

# 用迁移专用连接串；没有配就回落到应用连接串
_url = _settings.migration_database_url or _settings.database_url
# ConfigParser 会做 % 插值，密码里含 % 时必须转义，否则读取时报错
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

# 版本表固定放 public，不跟着业务 schema 走
VERSION_TABLE_SCHEMA = "public"


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema=VERSION_TABLE_SCHEMA,
        compare_type=True,  # 列类型变化也生成迁移
        compare_server_default=True,
        # 保留默认的 % 渲染
        literal_binds=connection is None,
        dialect_opts={"paramstyle": "named"},
    )


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。用于人工审查将要执行的 DDL。"""
    url = config.get_main_option("sqlalchemy.url")
    _configure(url=url)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
