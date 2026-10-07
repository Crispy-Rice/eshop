"""assistant

**平台 AI 助手（后台端：商家 / 平台运营）**，见 docs/20-assistant.md。

新 schema ``assistant`` 与三张表：

1. ``conversation`` —— 会话。**每个用户同时只有一条进行中**（部分唯一索引）。
   CLI 面板是一个持续的长会话加一个「新对话」按钮，所以没有会话列表，
   也就不需要给用户看的会话编号展示位。
2. ``message`` —— 消息，只追加。``id`` 用 ``Identity`` 而不是雪花：它是日志表，
   严格保序比"ID 里带时间"重要（两轮问答可能落在同一毫秒）。**身份快照
   （actor_user_id / actor_role / actor_shop_id）冻结在行上**，worker 只信这一份。
3. ``usage_daily`` —— token 记账，按 (日, 主体) 累加。账本放 PG 而不是 Redis：
   演示机的 Redis 带淘汰策略，计数器被淘汰就等于静默变成无限额。

★ ``assistant`` 是基线之后**第二个**新 schema，授权要在这里自己解决 ——
  理由与写法与 ``d2a7f8c10e64`` 完全相同（``init.sh`` 只在空数据目录上跑一次，
  老库不会被重跑，新 schema 会既没有 USAGE 也没有 default privileges，
  线上应用角色一查就是 ``permission denied for schema assistant``）。
  角色名不写死，按"谁能写 ``core.local_message``"推导。
"""

import re
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e93b7c2d5f18"
down_revision: Union[str, Sequence[str], None] = "d2a7f8c10e64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True, precision=3)

# 角色名要拼进 SQL（PG 的 GRANT 不接受标识符做参数），先卡一道格式
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 权限矩阵的判据：谁能写 local_message，谁就是应用角色
_PROBE_TABLE = "core.local_message"


def _grant_assistant_schema() -> None:
    """把 assistant schema 按既有权限矩阵授出去。

    **必须在建表之前调用** —— 先 ``ALTER DEFAULT PRIVILEGES``，随后建的表就自动
    带上权限，不必再 ``GRANT ... ON ALL TABLES``（init.sh 用的是同一招）。
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT r.rolname AS rolname,"
            "       has_table_privilege(r.rolname, :probe, 'INSERT') AS can_write"
            "  FROM pg_roles r"
            " WHERE r.rolname NOT LIKE 'pg\\_%'"
            "   AND has_schema_privilege(r.rolname, 'core', 'USAGE')"
        ),
        {"probe": _PROBE_TABLE},
    ).all()

    for rolname, can_write in rows:
        if not _IDENT.match(rolname):
            continue
        quoted = f'"{rolname}"'
        bind.execute(sa.text(f"GRANT USAGE ON SCHEMA assistant TO {quoted}"))
        if can_write:
            bind.execute(
                sa.text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA assistant "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {quoted}"
                )
            )
            bind.execute(
                sa.text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA assistant "
                    f"GRANT USAGE, SELECT ON SEQUENCES TO {quoted}"
                )
            )
        else:
            bind.execute(
                sa.text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA assistant "
                    f"GRANT SELECT ON TABLES TO {quoted}"
                )
            )


def upgrade() -> None:
    """Upgrade schema."""
    # ------------------------------------------------------------------
    # 0. 新 schema + 授权（顺序要紧，见 _grant_assistant_schema 的 docstring）
    # ------------------------------------------------------------------
    op.execute('CREATE SCHEMA IF NOT EXISTS "assistant"')
    _grant_assistant_schema()

    # ------------------------------------------------------------------
    # 1. 会话
    # ------------------------------------------------------------------
    op.create_table(
        "conversation",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="雪花"),
        sa.Column("conversation_no", sa.String(length=32), nullable=False, comment="会话号"),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "owner_role",
            sa.String(length=16),
            nullable=False,
            comment="merchant / admin，提问那一刻的快照",
        ),
        sa.Column(
            "shop_id",
            sa.BigInteger(),
            nullable=True,
            comment="商家=本店；运营=NULL。★ 这里 NULL 是安全的：唯一索引只建在 "
            "owner_user_id 上，不涉及本列，所以没有 support 那套哨兵问题",
        ),
        sa.Column(
            "title",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
            comment="首个问题的截断",
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("10"),
            comment="10 进行中 30 已关闭",
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation")),
        sa.UniqueConstraint("conversation_no", name="uk_assistant_conversation_no"),
        sa.CheckConstraint("status IN (10, 30)", name=op.f("ck_conversation_status_valid")),
        schema="assistant",
        comment="AI 助手会话",
    )
    # 一个用户同时只有一条进行中会话（关闭的不占用）。「新对话」= 关旧开新
    op.create_index(
        "uk_assistant_conversation_owner",
        "conversation",
        ["owner_user_id"],
        unique=True,
        schema="assistant",
        postgresql_where=sa.text("status <> 30"),
    )

    # ------------------------------------------------------------------
    # 2. 消息
    # ------------------------------------------------------------------
    op.create_table(
        "message",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("conversation_no", sa.String(length=32), nullable=False),
        sa.Column("role", sa.SmallInteger(), nullable=False, comment="1 用户 2 助手"),
        sa.Column(
            "status",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("30"),
            comment="user 行恒为 30；assistant 行从 10 出发（10待处理 20处理中 "
            "30完成 40失败 50降级）",
        ),
        sa.Column("content", sa.Text(), nullable=True, comment="答案正文；失败时为空"),
        sa.Column(
            "error_code",
            sa.String(length=32),
            nullable=True,
            comment="失败时的 ErrorCode 名，前端据此给不同文案",
        ),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="工具调用审计（名字/参数/耗时/结果摘要，已脱敏截断）。"
            "★ 不参与后续轮次的历史，见 models.py 的模块 docstring",
        ),
        sa.Column("llm_model", sa.String(length=64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "actor_user_id", sa.BigInteger(), nullable=False, comment="身份快照，worker 只信它"
        ),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("actor_shop_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message")),
        sa.ForeignKeyConstraint(
            ["conversation_no"],
            ["assistant.conversation.conversation_no"],
            name=op.f("fk_message_conversation_no_conversation"),
        ),
        sa.CheckConstraint("role IN (1, 2)", name=op.f("ck_message_role_valid")),
        sa.CheckConstraint(
            "status IN (10, 20, 30, 40, 50)", name=op.f("ck_message_status_valid")
        ),
        schema="assistant",
        comment="AI 助手消息（含审计）",
    )
    op.create_index(
        "idx_assistant_message_conv",
        "message",
        ["conversation_no", "id"],
        schema="assistant",
    )
    # reaper 扫描用：只索引"还在飞"的行，稳态下这个索引几乎为空
    op.create_index(
        "idx_assistant_message_inflight",
        "message",
        ["created_at"],
        schema="assistant",
        postgresql_where=sa.text("status IN (10, 20)"),
    )

    # ------------------------------------------------------------------
    # 3. token 记账
    # ------------------------------------------------------------------
    op.create_table(
        "usage_daily",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("day", sa.String(length=8), nullable=False, comment="yyyymmdd（UTC）"),
        sa.Column("scope", sa.SmallInteger(), nullable=False, comment="1 按用户 2 按店铺"),
        sa.Column("scope_id", sa.BigInteger(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_daily")),
        sa.UniqueConstraint("day", "scope", "scope_id", name="uk_assistant_usage_daily"),
        sa.CheckConstraint("scope IN (1, 2)", name=op.f("ck_usage_daily_scope_valid")),
        schema="assistant",
        comment="AI 助手每日 token 记账",
    )
    op.create_index("idx_assistant_usage_day", "usage_daily", ["day"], schema="assistant")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_assistant_usage_day", table_name="usage_daily", schema="assistant")
    op.drop_table("usage_daily", schema="assistant")

    op.drop_index("idx_assistant_message_inflight", table_name="message", schema="assistant")
    op.drop_index("idx_assistant_message_conv", table_name="message", schema="assistant")
    op.drop_table("message", schema="assistant")

    op.drop_index("uk_assistant_conversation_owner", table_name="conversation", schema="assistant")
    op.drop_table("conversation", schema="assistant")

    op.execute('DROP SCHEMA IF EXISTS "assistant"')
