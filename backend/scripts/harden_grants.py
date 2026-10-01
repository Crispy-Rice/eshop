"""发布流程的收尾一步：把审计表的 UPDATE/DELETE 权限从应用角色上收走。

**为什么需要这一步。** docs/07 §4.5 要求 ``trade.order_state_flow`` 对应用
账号只授予 ``INSERT, SELECT`` —— 状态流水是审计日志，纠纷时靠它还原订单的
完整生命历史，所以要从权限层面保证改不了。

但 ``deploy/postgres/init/01-init.sh`` 里的 ``ALTER DEFAULT PRIVILEGES`` 会给
**所有将来创建的表**授予 SELECT/INSERT/UPDATE/DELETE（否则每次加表都要手工
GRANT）。这张表是之后由 Alembic 迁移建出来的，建的时候自动就带上了
UPDATE/DELETE。所以必须在**迁移跑完之后**补一次 REVOKE。

**幂等**：REVOKE 重复执行没有副作用，每次发布都能安全地跑。

**和谁对齐**：从 ``DATABASE_URL`` 里解析出应用角色名，而不是写死
``eshop_app`` —— 这样将来改了角色名也不会漏掉。开发环境两者是同一个角色
（owner 即 app），此时跳过并提示，因为没有要收紧的对象。

用法（在 migrate 容器里，紧跟 ``alembic upgrade head``）::

    python scripts/harden_grants.py

退出码非 0 表示加固后校验仍未通过，发布应当中止。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# 直接 `python scripts/harden_grants.py` 时，Python 只把 scripts/ 放进模块
# 搜索路径，`app` 包会找不到。把 backend/ 补进去。必须在 import app.* 之前。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)

# 不可变的审计表：(schema, 表名)
IMMUTABLE_TABLES: tuple[tuple[str, str], ...] = (("trade", "order_state_flow"),)

# 角色名要拼进 SQL（PG 的 REVOKE 不接受标识符做参数），所以先卡一道格式。
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def _harden(engine, app_role: str) -> int:
    """执行 REVOKE 并回读校验。返回未通过的表数量。"""
    failed = 0

    async with engine.begin() as conn:
        for schema, table in IMMUTABLE_TABLES:
            qualified = f"{schema}.{table}"

            exists = await conn.scalar(
                text("SELECT to_regclass(:q)"), {"q": qualified}
            )
            if exists is None:
                # 表还没建出来（比如回滚到早期版本），不是错误
                logger.warning("审计表不存在，跳过", extra={"table": qualified})
                continue

            await conn.execute(
                text(f'REVOKE UPDATE, DELETE ON {qualified} FROM "{app_role}"')
            )

            # 回读确认。has_table_privilege 会考虑所有者身份，
            # 也是下面"同角色跳过"那个判断的依据。
            still_writable = await conn.scalar(
                text(
                    "SELECT has_table_privilege(:role, :q, 'UPDATE') "
                    "    OR has_table_privilege(:role, :q, 'DELETE')"
                ),
                {"role": app_role, "q": qualified},
            )
            if still_writable:
                logger.error(
                    "审计表加固未生效，应用角色仍可修改",
                    extra={"table": qualified, "role": app_role},
                )
                failed += 1
            else:
                logger.info(
                    "审计表已加固（应用角色只可 INSERT/SELECT）",
                    extra={"table": qualified, "role": app_role},
                )

    return failed


async def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level)

    migration_url = settings.migration_database_url or settings.database_url
    app_role = make_url(settings.database_url).username
    owner_role = make_url(migration_url).username

    if not app_role:
        logger.error("DATABASE_URL 里没有用户名，无法确定要收紧哪个角色")
        return 1

    if not _IDENT.match(app_role):
        logger.error("应用角色名格式非法，拒绝拼进 SQL", extra={"role": app_role})
        return 1

    if app_role == owner_role:
        logger.warning(
            "应用角色与迁移角色相同（本地开发环境），无对象可收紧 —— 跳过",
            extra={"role": app_role},
        )
        return 0

    # NullPool：这是个一次性脚本，用完即退，连接池没有意义
    engine = create_async_engine(migration_url, poolclass=NullPool, echo=False)
    try:
        failed = await _harden(engine, app_role)
    finally:
        await engine.dispose()

    if failed:
        logger.error("审计表权限加固失败，请中止发布", extra={"failed": failed})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
