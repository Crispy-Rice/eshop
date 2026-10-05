"""homepage banner

首页轮播图。落在 promotion schema 下 —— 它是**运营投放内容**，归属和营销一致，
管理端点也沿用同一套角色（admin / finance），没必要为一张表新建模块。

``image`` 存完整 url（``/media/banners/<uid>/xxx.webp``），与 products / shops /
avatars 同一套契约：直接当 ``<img src>`` 用。``link_url`` 只接受站内路径。

刻意**不做**生效时间窗：没有人要预约投放，加了就是给不存在的需求写代码。

Revision ID: f8d2b5a70c14
Revises: c3a91f7b6e28
Create Date: 2026-10-05 09:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f8d2b5a70c14"
down_revision: Union[str, Sequence[str], None] = "c3a91f7b6e28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "banner",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=False, comment="运营看的名字，也当 alt"),
        sa.Column("image", sa.String(length=255), nullable=False, comment="完整 url"),
        sa.Column(
            "link_url", sa.String(length=255), nullable=True, comment="站内路径，空=不可点"
        ),
        sa.Column("sort", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="1启用 2停用",
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True, precision=3),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_banner")),
        schema="promotion",
        comment="首页轮播图",
    )
    # 公开接口的查询就是 WHERE status=1 ORDER BY sort, id，正好吃这个索引
    op.create_index(
        "idx_banner_status_sort",
        "banner",
        ["status", "sort", "id"],
        schema="promotion",
    )


def downgrade() -> None:
    op.drop_index("idx_banner_status_sort", table_name="banner", schema="promotion")
    op.drop_table("banner", schema="promotion")
