"""assistant: 一个用户多条会话

**去掉"每用户同时只有一条进行中会话"这条不变量**，以及随之失去意义的 `status` 列。

理由（这是需求的直接后果，不是打扫）：那条部分唯一索引当初是为了**省掉会话列表**
才加的 —— `docs/20 §0` 原话是"面板是一个持续的长会话加一个「新对话」按钮，所以
没有会话列表，也就不需要给用户看的会话编号展示位"。现在**会话列表本身成了需求**
（能翻回以前每一段、并切回去继续聊），留着它反而要在"切回旧会话"时来回改状态。

`status` 一起删掉：没有"结束/归档"这个动作之后，它就只剩一个取值，
留着就是第二个真相来源 —— 与项目里对"死列"（如 `sku_freight_bind.warehouse_id`）
的态度一致。**不预建"归档"**，等真有需求时再加，那时它才有明确含义。

同时补一个索引：原来的 `owner_user_id` 上只有那条**唯一**索引，删掉它就等于
"按用户取会话"没有索引可走了 —— 会话列表与"最近一条"都要靠它。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c8d51a3b47"
down_revision: Union[str, Sequence[str], None] = "e93b7c2d5f18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    """Upgrade schema."""
    # 1. 那条"每用户一条"的部分唯一索引
    op.drop_index(
        "uk_assistant_conversation_owner", table_name="conversation", schema="assistant"
    )

    # 2. status 与它的 CHECK
    #    ★ 名字要用 ``op.f()`` 标成"已最终" —— 项目的命名约定是
    #      ``ck_%(table_name)s_%(constraint_name)s``，直接传全名会被再套一层前缀
    #      （第一次跑就是这么失败的：去 drop ``ck_conversation_ck_...``）。
    op.drop_constraint(
        op.f("ck_conversation_status_valid"),
        "conversation",
        schema="assistant",
        type_="check",
    )
    op.drop_column("conversation", "status", schema="assistant")

    # 3. 补回"按用户取会话"的索引（原来的唯一索引是它唯一的索引）
    op.create_index(
        "idx_assistant_conversation_owner",
        "conversation",
        ["owner_user_id", sa.text("updated_at DESC")],
        schema="assistant",
    )


def downgrade() -> None:
    """Downgrade schema.

    ★ **存在多条会话时会失败**（部分唯一索引要求每用户至多一条 `status=10`）。
      这是数据本身与新约束不相容，没法自动调和 —— 要降级就得先自行清理会话。
    """
    op.drop_index(
        "idx_assistant_conversation_owner", table_name="conversation", schema="assistant"
    )

    op.add_column(
        "conversation",
        sa.Column(
            "status",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("10"),
            comment="10 进行中 30 已关闭",
        ),
        schema="assistant",
    )
    op.create_check_constraint(
        op.f("ck_conversation_status_valid"),
        "conversation",
        "status IN (10, 30)",
        schema="assistant",
    )
    op.create_index(
        "uk_assistant_conversation_owner",
        "conversation",
        ["owner_user_id"],
        unique=True,
        schema="assistant",
        postgresql_where=sa.text("status <> 30"),
    )
