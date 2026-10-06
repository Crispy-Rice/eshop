"""site contact

新增 ``promotion.site_contact`` —— **全站单行的客服联系方式**。

背景：封禁提示写的是「账号已被封禁，请联系客服」，但买家端**没有任何联系入口**
（``web-mall/src`` 里 0 处「联系 / 客服 / 热线 / 申诉」，页脚也在早前的轮次里删掉了）。
后端所有「客服」都是**运营侧**概念（客服补发券、券码客服核销、``operator_type = 4 平台客服``、
``phone_cipher`` 客服查看），没有一个是买家能触达的。于是那句话是个**死胡同**：
让用户去联系一个不存在的客服。

改成：联系方式由**运营**在后台填、公开接口下发，买家端在**登录页**与**封禁提示**处展示
—— 这两处正是"人还没登进来"的场景（被封的人本来就只能停在登录页）。

设计要点：

- **单行**用 ``CHECK (id = 1)``，与 ``site_theme`` 同一套做法；迁移里插好那一行，
  公开接口因此永远读得到，不必到处处理"表是空的"。
- **NULL = 未配置**（三列都不给 server_default）。与 ``sku_code`` 的取舍一致：
  写侧把空串归一成 NULL。这样"没配联系方式"是一个**可判断的状态** —— 买家端只展示
  真正填了的那几项，一项都没填就只说事实、不承诺渠道。
- 与 ``site_theme`` 分两张表而不是合成一张：形状不同（一个枚举 + 三个文本），
  改动节奏也不同（皮肤随大促换、联系方式基本不动）；合表则要把刚写完的 site_theme
  全套改名，换来的是零功能收益。

★ 这**不是**"站点配置中心"。等**第三个**站点级配置出现时再谈统一的 KV 表 ——
  为两个键位建一层抽象是负担，不是设计。

Revision ID: b8e1c4d7a902
Revises: f7d3b9e5c284
Create Date: 2026-10-06 18:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e1c4d7a902"
down_revision: Union[str, Sequence[str], None] = "f7d3b9e5c284"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site_contact",
        sa.Column("id", sa.SmallInteger(), nullable=False, comment="恒为 1"),
        sa.Column(
            "service_email",
            sa.String(length=128),
            nullable=True,
            comment="客服邮箱。NULL = 未配置",
        ),
        sa.Column(
            "service_phone",
            sa.String(length=32),
            nullable=True,
            comment="客服电话。NULL = 未配置。自由文本，400 号 / 固话 / 带分机都行",
        ),
        sa.Column(
            "service_hours",
            sa.String(length=64),
            nullable=True,
            comment="服务时间，如「工作日 9:00-18:00」。NULL = 未配置",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # ★ 单行：把"整站只有一份联系方式"这件事写进结构里
        sa.CheckConstraint("id = 1", name="single_row"),
        sa.PrimaryKeyConstraint("id"),
        schema="promotion",
        comment="平台客服联系方式（单行配置）",
    )
    # 立刻插入那一行（三列全 NULL = 未配置）：公开接口永远有一行可读
    op.execute("INSERT INTO promotion.site_contact (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("site_contact", schema="promotion")
