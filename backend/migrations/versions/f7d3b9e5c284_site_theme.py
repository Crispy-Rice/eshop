"""site theme

新增 ``promotion.site_theme`` —— **全站单行的皮肤配置**。

背景：皮肤（中性 / 618 / 双 11 / 年货节）原先由**用户**在买家端顶部自己挑，
localStorage 各存各的。于是 ``docs/17 §1`` 列为核心原则的「大促可换肤」**根本不成立**：
运营想让全站变红搞大促做不到。而且那几套皮肤里还硬编码着优惠承诺文案
（「跨店每满 300 减 50」），用户切一下皮肤就"看到"了并不存在的活动。

改成：**皮肤由运营在后台启用、全站生效**，买家端不再有切换器。这一行就是唯一事实来源。

设计要点：

- **单行**用 ``CHECK (id = 1)`` 表达，而不是靠"约定只插一行"。迁移里顺手插好那一行，
  公开接口因此永远读得到，调用方不必处理"表是空的"。
- 放 ``promotion`` schema 下，与 ``banner`` 同理：它是**运营投放的东西**，
  归属与角色（admin / finance）跟营销一致，没必要为一行配置新建模块。
- 项目此前**没有任何站点级配置 / KV 表的先例**（``ops.switch`` 只存在于文档里、
  从未建表），所以这是第一个；形状照 ``banner`` 那一张来。

★ 皮肤的 id 白名单在后端 ``promotion/models.py`` 的 ``SITE_SKINS``（校验用），
  配色在同一套 id 的 ``web-mall/src/theme/themes.ts`` 里 —— 加皮肤要同时动这两处。

Revision ID: f7d3b9e5c284
Revises: a4f8e2c1b973
Create Date: 2026-10-06 16:20:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7d3b9e5c284"
down_revision: Union[str, Sequence[str], None] = "a4f8e2c1b973"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site_theme",
        sa.Column("id", sa.SmallInteger(), nullable=False, comment="恒为 1"),
        sa.Column(
            "skin",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'neutral'"),
            comment="启用的皮肤 id，与 web-mall 的 theme/themes.ts 对齐",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # ★ 单行：把"整站只有一个当前皮肤"这件事写进结构里
        sa.CheckConstraint("id = 1", name="single_row"),
        sa.PrimaryKeyConstraint("id"),
        schema="promotion",
        comment="站点主题（单行配置）",
    )
    # 立刻插入默认行：公开接口就永远有一行可读，不必处理"表是空的"
    op.execute("INSERT INTO promotion.site_theme (id, skin) VALUES (1, 'neutral')")


def downgrade() -> None:
    op.drop_table("site_theme", schema="promotion")
