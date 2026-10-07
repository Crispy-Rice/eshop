"""assistant: 店小蜜三张表（开关 / 商户问答 / 每次回答的台账）

买家在商城端问，AI 以**这家店**的身份答（见 docs/20 §11）。三张表各有明确职责：

1. ``shop_setting`` —— 一店一行的开关，**默认关**。这是唯一一个"AI 面向公众"的
   开关，所以由商户自己打开，平台不替他们开。
2. ``shop_faq`` —— 商户自己维护的问答（这家店的话）。与 ``knowledge/*.md``
   （平台买家规则，改它要发版）分开。
3. ``bot_turn`` —— 每次回答的执行台账。两个身份：
   **幂等闩锁**（``source_message_id`` 唯一：扫描每 3 秒一轮，靠它把"同一条买家消息"
   收敛成一行、一次回答）+ **审计**（调了哪些工具、花了多少 token）。
   消息本身只落 ``support.ticket_message`` —— 一个事实只有一个来源。

★ 权限沿用 ``assistant`` schema 的 ``ALTER DEFAULT PRIVILEGES``（e93b7c2d5f18
  里设的），新表自动带上，不必再 GRANT 一遍。

Revision ID: 7486e2ecb95f
Revises: d6da37b7f895
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7486e2ecb95f"
down_revision: str | Sequence[str] | None = "d6da37b7f895"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = postgresql.TIMESTAMP(timezone=True, precision=3)


def upgrade() -> None:
    op.create_table(
        "shop_setting",
        sa.Column("shop_id", sa.BigInteger(), nullable=False, comment="一店一行，主键即店铺"),
        sa.Column(
            "ai_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="智能客服是否对买家开放。**默认 false**，由商户自己打开",
        ),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("shop_id", name=op.f("pk_shop_setting")),
        schema="assistant",
        comment="店铺的智能客服设置",
    )

    op.create_table(
        "shop_faq",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="雪花"),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column("question", sa.String(length=200), nullable=False, comment="买家可能怎么问"),
        sa.Column("answer", sa.String(length=1000), nullable=False, comment="该怎么答"),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="停用但不删",
        ),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shop_faq")),
        schema="assistant",
        comment="商户维护的问答（店小蜜知识库）",
    )
    op.create_index(
        "idx_assistant_shop_faq_shop",
        "shop_faq",
        ["shop_id", "id"],
        unique=False,
        schema="assistant",
    )

    op.create_table(
        "bot_turn",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="雪花"),
        sa.Column(
            "source_message_id",
            sa.BigInteger(),
            nullable=False,
            comment="触发这一轮的买家消息（support.ticket_message.id）—— 幂等闩锁",
        ),
        sa.Column("ticket_no", sa.String(length=32), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "buyer_user_id",
            sa.BigInteger(),
            nullable=False,
            comment="提问的买家；工具身份快照",
        ),
        sa.Column("status", sa.SmallInteger(), server_default=sa.text("10"), nullable=False),
        sa.Column("content", sa.Text(), nullable=True, comment="最终答复；没答时为空"),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="工具调用审计（名字/参数/耗时/结果摘要）",
        ),
        sa.Column("llm_model", sa.String(length=64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN (10, 20, 30, 40, 50)", name=op.f("ck_bot_turn_status_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_turn")),
        sa.UniqueConstraint("source_message_id", name="uk_assistant_bot_turn_source"),
        schema="assistant",
        comment="店小蜜每次回答的执行台账",
    )
    op.create_index(
        "idx_assistant_bot_turn_inflight",
        "bot_turn",
        ["created_at"],
        unique=False,
        schema="assistant",
        postgresql_where=sa.text("status IN (10, 20)"),
    )
    op.create_index(
        "idx_assistant_bot_turn_ticket",
        "bot_turn",
        ["ticket_no", sa.literal_column("id DESC")],
        unique=False,
        schema="assistant",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_assistant_bot_turn_ticket", table_name="bot_turn", schema="assistant"
    )
    op.drop_index(
        "idx_assistant_bot_turn_inflight",
        table_name="bot_turn",
        schema="assistant",
        postgresql_where=sa.text("status IN (10, 20)"),
    )
    op.drop_table("bot_turn", schema="assistant")
    op.drop_index("idx_assistant_shop_faq_shop", table_name="shop_faq", schema="assistant")
    op.drop_table("shop_faq", schema="assistant")
    op.drop_table("shop_setting", schema="assistant")
