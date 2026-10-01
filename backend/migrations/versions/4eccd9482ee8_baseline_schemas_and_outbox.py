"""baseline schemas and outbox

建立 14 个模块 schema 与本地消息表（outbox）。

- schema 用 IF NOT EXISTS：Docker 初始化脚本（deploy/postgres/init/01-init.sh）
  已经建过一次，这里保证在没有初始化脚本的环境（如托管数据库）也能跑通。
- core.local_message 是最终一致性的基础设施，见 docs/13-schema.md §4：
  业务事务内写消息 → 投递协程推到 Redis Streams → 消费者按 biz_key 幂等消费。

Revision ID: 4eccd9482ee8
Revises:
Create Date: 2026-10-01 11:30:14.309128

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4eccd9482ee8"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 毫秒精度、带时区。全站统一存 UTC（docs/13-schema.md §0.2）
TS = postgresql.TIMESTAMP(timezone=True, precision=3)

# 一个模块一个 schema，模块之间不跨 schema 建外键（docs/13-schema.md §0.1）
SCHEMAS = (
    "account",
    "product",
    "inventory",
    "cart",
    "promotion",
    "freight",
    "trade",
    "payment",
    "aftersale",
    "review",
    "settlement",
    "notify",
    "core",
    "ops",
)


def upgrade() -> None:
    for schema in SCHEMAS:
        # DDL 里不能绑定参数，schema 名来自上面的常量元组，不是外部输入
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    op.create_table(
        "local_message",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("topic", sa.String(64), nullable=False, comment="事件主题，对应 Redis Stream stream:{topic}"),
        sa.Column("biz_key", sa.String(160), nullable=False, comment="幂等键，消费者据此去重"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
            comment="0待发送 1已发送 2发送失败(待重试) 3已放弃",
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_retry_at",
            TS,
            nullable=False,
            server_default=sa.text("now()"),
            comment="下次重试时间（指数退避）",
        ),
        sa.Column("error_msg", sa.String(512), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name="pk_local_message"),
        # 同一事件不会被写入两次
        sa.UniqueConstraint("topic", "biz_key", name="uk_local_message_topic_biz_key"),
        schema="core",
        comment="本地消息表（事务消息的替代）",
    )

    # 部分索引：只包含未完成的消息，体积始终很小，
    # 投递协程的扫描（WHERE status IN (0,2) AND next_retry_at <= now()）走它
    op.create_index(
        "idx_local_message_pending",
        "local_message",
        ["next_retry_at"],
        schema="core",
        postgresql_where=sa.text("status IN (0, 2)"),
    )


def downgrade() -> None:
    op.drop_index("idx_local_message_pending", table_name="local_message", schema="core")
    op.drop_table("local_message", schema="core")
    # 不删 schema：它们由环境初始化脚本负责，且可能已有其他数据
