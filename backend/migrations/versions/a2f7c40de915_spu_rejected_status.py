"""spu rejected status

给「平台驳回」一个独立的状态值 6，不再和草稿（1）混在一起。

背景：审核不通过原本把商品写回 ``SPU_DRAFT``，于是商家列表里"从没提交过"和
"被平台打回来待改"长得一模一样 —— 分不清哪些是自己没写完的、哪些是等着他动手改的。

★ 这是**纯语义**变更，不动列类型：``product.spu.status`` 是 smallint 且**没有 CHECK
  约束**，所以不需要改值域就能写入 6。这里只补一句列注释，让库里的说明和
  ``models.py`` 对齐（否则下次 autogenerate 会报一条假的注释差异）。

存量回填：``audit_remark`` 只在驳回时写入、通过审核时清空，所以
「status=1 且有 audit_remark」**精确等价于**「被驳回、还没重新通过」——
从没提交过的真草稿不会有审核意见。据此把历史驳回件迁到 6。

Revision ID: a2f7c40de915
Revises: f8d2b5a70c14
Create Date: 2026-10-05 15:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2f7c40de915"
down_revision: Union[str, Sequence[str], None] = "f8d2b5a70c14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_COMMENT = "1草稿 2上架 3下架 4违规下架 5待审核 6已驳回"
_STATUS_COMMENT_OLD = "1草稿 2上架 3下架 4违规下架 5待审核"


def upgrade() -> None:
    op.alter_column(
        "spu",
        "status",
        existing_type=sa.SmallInteger(),
        existing_nullable=False,
        existing_server_default=sa.text("1"),
        comment=_STATUS_COMMENT,
        schema="product",
    )
    op.execute(
        "UPDATE product.spu SET status = 6, updated_at = now() "
        "WHERE status = 1 AND audit_remark IS NOT NULL AND NOT deleted"
    )


def downgrade() -> None:
    # 6 在旧版本里没有对应状态，只能退回它以前待的地方（草稿）
    op.execute("UPDATE product.spu SET status = 1 WHERE status = 6")
    op.alter_column(
        "spu",
        "status",
        existing_type=sa.SmallInteger(),
        existing_nullable=False,
        existing_server_default=sa.text("1"),
        comment=_STATUS_COMMENT_OLD,
        schema="product",
    )
