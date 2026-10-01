"""ops alert table

Revision ID: d5b9e2f4c810
Revises: c3f1a72b9d04
Create Date: 2026-10-01 16:05:00.000000

运维告警表。库存对账发现 Redis 与 DB 漂移时写入（docs/03 §7），
后续售后、支付的对账任务同样用它。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd5b9e2f4c810'
down_revision: Union[str, Sequence[str], None] = 'c3f1a72b9d04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "alert",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "level",
            sa.SmallInteger(),
            server_default=sa.text("2"),
            nullable=False,
            comment="1=P0 2=P1 3=P2",
        ),
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            comment="产生告警的模块/任务，如 inventory.reconcile",
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "handled_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            nullable=True,
            comment="NULL 表示未处理",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert")),
        schema="ops",
    )
    # 部分索引：只包含未处理的告警
    op.create_index(
        "idx_alert_unhandled",
        "alert",
        ["created_at"],
        schema="ops",
        postgresql_where=sa.text("handled_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "idx_alert_unhandled",
        table_name="alert",
        schema="ops",
        postgresql_where=sa.text("handled_at IS NULL"),
    )
    op.drop_table("alert", schema="ops")
