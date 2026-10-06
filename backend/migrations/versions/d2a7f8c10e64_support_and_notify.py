"""support and notify

**客服工单（support）+ 站内信（notify）**，见 docs/19-support.md。

1. 新 schema ``support`` 与三张表：

   - ``ticket`` —— 会话。``shop_id`` 用**哨兵 0 表示平台级**（不是 NULL）：
     PG 的唯一索引把 NULL 当作互不相同，可空的话
     ``uk_ticket_user_shop_active`` 这个部分唯一索引就形同虚设 ——
     同一用户能开出无数条平台级会话。
   - ``ticket_message`` —— 消息，只追加。
   - ``ticket_state_flow`` —— 状态流水，命名与 ``trade.order_state_flow`` 对齐；
     它是**审计表**，``scripts/harden_grants.py`` 会把 UPDATE/DELETE 收走。

2. ``notify.site_message`` —— 站内信。``notify`` 这个 schema 基线里就建好了
   （docs/13 §1 也一直列着这张表），但表从未建出来。它有两个来源，走两条不同的路：
   客服回复由 support **在同一事务里直写**；trade/payment/aftersale 那 6 个既有的
   outbox topic 由 worker 的投递循环分派进来（docs/19 §3）。

★ **``support`` 是基线之后第一个新 schema**，所以要在这里自己解决授权：
  ``deploy/postgres/init/01-init.sh`` 只在**空数据目录**上跑一次，老库不会被重跑，
  于是新 schema 既没有 USAGE 也没有 default privileges —— 线上应用角色一查这张表
  就是 ``permission denied for schema support``。角色名不写死，按"谁能用
  ``core.local_message``"推导（权限矩阵见 docs/16 §4.2）：
  能写的给 DML、只能读的给 SELECT。本地开发两者是同一个超级用户，循环自然跳过
  —— 所有者本来就不需要授权。
"""

import re
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2a7f8c10e64"
down_revision: Union[str, Sequence[str], None] = "c1e8f4a70b93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True, precision=3)

# 角色名要拼进 SQL（PG 的 GRANT 不接受标识符做参数），先卡一道格式
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 权限矩阵的判据：谁能写 local_message，谁就是应用角色
_PROBE_TABLE = "core.local_message"


def _grant_support_schema() -> None:
    """把 support schema 按既有权限矩阵授出去。

    **必须在建表之前调用** —— 先 ``ALTER DEFAULT PRIVILEGES``，随后建的表就自动
    带上权限，不必再 ``GRANT ... ON ALL TABLES``（init.sh 用的是同一招）。
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT r.rolname AS rolname,"
            "       has_table_privilege(r.rolname, :probe, 'INSERT') AS can_write"
            "  FROM pg_roles r"
            " WHERE r.rolcanlogin"
            "   AND NOT r.rolsuper"
            "   AND has_table_privilege(r.rolname, :probe, 'SELECT')"
        ),
        {"probe": _PROBE_TABLE},
    ).all()

    for rolname, can_write in rows:
        if not _IDENT.match(rolname):
            continue
        quoted = f'"{rolname}"'
        bind.execute(sa.text(f"GRANT USAGE ON SCHEMA support TO {quoted}"))
        if can_write:
            bind.execute(
                sa.text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA support "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {quoted}"
                )
            )
            bind.execute(
                sa.text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA support "
                    f"GRANT USAGE, SELECT ON SEQUENCES TO {quoted}"
                )
            )
        else:
            bind.execute(
                sa.text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA support "
                    f"GRANT SELECT ON TABLES TO {quoted}"
                )
            )


def upgrade() -> None:
    """Upgrade schema."""
    # ------------------------------------------------------------------
    # 0. 新 schema + 授权（顺序要紧，见 _grant_support_schema 的 docstring）
    # ------------------------------------------------------------------
    op.execute('CREATE SCHEMA IF NOT EXISTS "support"')
    _grant_support_schema()

    # ------------------------------------------------------------------
    # 1. 会话
    # ------------------------------------------------------------------
    op.create_table(
        "ticket",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ticket_no", sa.String(length=32), nullable=False, comment="会话号"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="买家"),
        sa.Column(
            "shop_id",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
            comment="0 = 平台级会话。不可空，否则部分唯一索引对 NULL 不起作用",
        ),
        sa.Column("subject", sa.String(length=120), nullable=False, comment="一句话标题"),
        sa.Column(
            "source",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("5"),
            comment="1商品页 2订单页 3售后页 4申诉 5其他",
        ),
        sa.Column("order_main_no", sa.String(length=32), nullable=True, comment="上下文快照"),
        sa.Column("order_sub_no", sa.String(length=32), nullable=True),
        sa.Column("refund_no", sa.String(length=32), nullable=True),
        sa.Column(
            "status",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("10"),
            comment="10 进行中 30 已关闭",
        ),
        sa.Column(
            "last_message_at", TS, nullable=False, server_default=sa.text("now()"),
            comment="列表排序键",
        ),
        sa.Column(
            "last_sender_type",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("4"),
            comment="最后一条消息的发送方（1买家 2商家 3平台 4系统）。"
            "「欠谁回复」由它推；**新建时为 4 系统** —— 会话刚开出来还没人说过话，"
            "默认成买家会让空会话永远显示成「待回复」",
        ),
        sa.Column("user_read_at", TS, nullable=True, comment="买家读到哪了"),
        sa.Column("staff_read_at", TS, nullable=True, comment="商家/平台读到哪了"),
        sa.Column("close_by", sa.SmallInteger(), nullable=True, comment="1买家 2商家 3平台 4系统"),
        sa.Column("close_reason", sa.String(length=255), nullable=True),
        sa.Column("close_time", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket")),
        sa.UniqueConstraint("ticket_no", name="uk_ticket_no"),
        sa.CheckConstraint("status IN (10, 30)", name=op.f("ck_ticket_status_valid")),
        schema="support",
        comment="客服会话",
    )
    # 一个用户对同一个对象同时只有一条进行中会话（关闭的不占用）
    op.create_index(
        "uk_ticket_user_shop_active",
        "ticket",
        ["user_id", "shop_id"],
        unique=True,
        schema="support",
        postgresql_where=sa.text("status <> 30"),
    )
    op.create_index(
        "idx_ticket_user",
        "ticket",
        ["user_id", "status", sa.text("last_message_at DESC")],
        schema="support",
    )
    op.create_index(
        "idx_ticket_shop",
        "ticket",
        ["shop_id", "status", sa.text("last_message_at DESC")],
        schema="support",
    )

    # ------------------------------------------------------------------
    # 2. 消息（只追加）
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_message",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("ticket_no", sa.String(length=32), nullable=False),
        sa.Column(
            "sender_type",
            sa.SmallInteger(),
            nullable=False,
            comment="1买家 2商家 3平台 4系统",
        ),
        sa.Column("sender_id", sa.BigInteger(), nullable=True, comment="系统消息为空"),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column(
            "images",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="凭证图片",
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["ticket_no"],
            ["support.ticket.ticket_no"],
            name=op.f("fk_ticket_message_ticket_no"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_message")),
        schema="support",
        comment="客服会话消息",
    )
    op.create_index(
        "idx_ticket_message_ticket",
        "ticket_message",
        ["ticket_no", "created_at", "id"],
        schema="support",
    )

    # ------------------------------------------------------------------
    # 3. 状态流水（审计表，harden_grants 会收走 UPDATE/DELETE）
    # ------------------------------------------------------------------
    op.create_table(
        "ticket_state_flow",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("ticket_no", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.SmallInteger(), nullable=False),
        sa.Column("to_status", sa.SmallInteger(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False, comment="OPEN / CLOSE / REOPEN"),
        sa.Column(
            "operator_type",
            sa.SmallInteger(),
            nullable=False,
            comment="1用户 2商家 3系统 4平台",
        ),
        sa.Column("operator_id", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_state_flow")),
        schema="support",
        comment="客服会话状态流水",
    )
    op.create_index(
        "idx_ticket_state_flow_ticket",
        "ticket_state_flow",
        ["ticket_no", "created_at"],
        schema="support",
    )

    # ------------------------------------------------------------------
    # 4. 站内信（notify schema 基线里已有，授权也在那时一起给了）
    # ------------------------------------------------------------------
    op.create_table(
        "site_message",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "msg_type",
            sa.String(length=32),
            nullable=False,
            comment="ORDER_CLOSED / ORDER_PAID / REFUND_SUCCEEDED / SUPPORT_REPLY",
        ),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.String(length=512), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "biz_key", sa.String(length=160), nullable=False, comment="幂等键：同一件事只发一条"
        ),
        sa.Column(
            "link_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'NONE'"),
            comment="ORDER / REFUND / TICKET / NONE",
        ),
        sa.Column(
            "link_value", sa.String(length=64), nullable=True, comment="订单号 / 售后单号 / 会话号"
        ),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("read_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_message")),
        sa.UniqueConstraint("biz_key", name="uk_site_message_biz"),
        schema="notify",
        comment="站内信",
    )
    op.create_index(
        "idx_site_message_user",
        "site_message",
        ["user_id", "is_read", sa.text("created_at DESC")],
        schema="notify",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_site_message_user", table_name="site_message", schema="notify")
    op.drop_table("site_message", schema="notify")

    op.drop_index(
        "idx_ticket_state_flow_ticket", table_name="ticket_state_flow", schema="support"
    )
    op.drop_table("ticket_state_flow", schema="support")

    op.drop_index("idx_ticket_message_ticket", table_name="ticket_message", schema="support")
    op.drop_table("ticket_message", schema="support")

    for index in ("idx_ticket_shop", "idx_ticket_user", "uk_ticket_user_shop_active"):
        op.drop_index(index, table_name="ticket", schema="support")
    op.drop_table("ticket", schema="support")
    # 不删 schema：与基线迁移同样的谨慎（它可能已被别的对象引用）
