"""SQLAlchemy ORM 基类。

约束/索引的命名统一在这里约定，保证 Alembic autogenerate 产生的名字
与 docs/13-schema.md §0.2 的规范一致（uk_ / ck_ / idx_ 前缀 + 表名）。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase

# 毫秒精度、带时区。全站统一存 UTC，展示时再转东八区
# （docs/13-schema.md §0.2）
TS = TIMESTAMP(timezone=True, precision=3)

NAMING_CONVENTION = {
    "ix": "idx_%(table_name)s_%(column_0_N_name)s",
    "uq": "uk_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
