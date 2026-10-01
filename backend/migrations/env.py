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

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

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

# 这些是数据库自带的，不该参与比对
EXCLUDED_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})

# 分区子表（如 inventory.stock_flow_202610）由迁移和定时任务用 DDL 创建，
# 不在 metadata 里。不排除的话 autogenerate 会认为它们"库里多出来的表"
# 并生成 drop_table —— 把流水数据删掉。
# 用 pg_inherits 判断而不是靠表名模式匹配，分区命名变了也不会失效。
_partition_tables: set[str] = set()


def _load_partition_tables(connection: Connection) -> None:
    rows = connection.execute(
        text(
            "SELECT n.nspname || '.' || c.relname "
            "FROM pg_class c "
            "JOIN pg_inherits i ON i.inhrelid = c.oid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace"
        )
    )
    _partition_tables.update(row[0] for row in rows)


def _include_object(obj: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    """过滤掉不该被 autogenerate 插手的对象。

    两类必须排除：

    1. **alembic 自己的版本表**：开了 include_schemas 之后它会出现在
       public schema 里却不在 metadata 中，autogenerate 会生成
       ``op.drop_table('alembic_version')`` —— 直接把迁移历史删掉。
    2. **分区子表**：见 ``_partition_tables`` 的说明。
    """
    if type_ == "table":
        if name == "alembic_version":
            return False
        schema = getattr(obj, "schema", None)
        if f"{schema}.{name}" in _partition_tables:
            return False
    return getattr(obj, "schema", None) not in EXCLUDED_SCHEMAS


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=_include_object,
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
        # ★ 分区表名单必须在 begin_transaction() **之内**加载。
        #   在它之前碰连接会先开启一个隐式事务，Alembic 见状会认为事务
        #   已由外部管理、不再负责提交，连接关闭时整个迁移被静默回滚 ——
        #   表现是 downgrade/upgrade 报成功却什么都没发生，且退出码为 0。
        _load_partition_tables(connection)
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
