"""support ticket need_human_at

给 ``support.ticket`` 加「已转人工」闩锁。

★ 为什么需要它：商家的「待回复」队列原来判定为 `进行中 且 最后一条是买家`。
  智能客服（店小蜜）一开口这条就不成立了 —— AI 答完球在买家手里（对），
  但 **AI 答不了时也掉出队列**（错，那恰恰是必须人工的一条）。所以"已转人工"
  要单独记一笔：它表达的是**最后一条消息推不出来的事实**。
  判定统一到 ``models.OWES_REPLY_WHERE``（列表筛选 + 角标 count），
  ``rules.staff_owes_reply`` 镜像同一句话。

配套：买家侧新增「转人工」按钮，也置这一列（与 AI 的升级共用一个闩锁）。

Revision ID: d6da37b7f895
Revises: f2c8d51a3b47
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6da37b7f895"
down_revision: str | Sequence[str] | None = "f2c8d51a3b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 core/base.py 的 TS 一致（毫秒精度、带时区；precision 只有 PG 方言认）
TS = TIMESTAMP(timezone=True, precision=3)


def upgrade() -> None:
    op.add_column(
        "ticket",
        sa.Column(
            "need_human_at",
            TS,
            nullable=True,
            comment="已转人工的时刻。买家点「转人工」或 AI 判定答不了时置位，"
            "客服回复或关单时清空 —— 「欠回复」判定要用（见 OWES_REPLY_WHERE）",
        ),
        schema="support",
    )


def downgrade() -> None:
    op.drop_column("ticket", "need_human_at", schema="support")
