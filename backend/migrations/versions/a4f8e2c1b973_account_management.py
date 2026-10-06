"""account management

给账号补上"能被管、能自救、能退出"三件事所需的落点：

1. ``ban_reason`` / ``closed_at`` —— 封禁理由（会展示给用户）与注销时间。
   ``account.user.status`` 从建表起就写着「1正常 2冻结 3注销」，但**2/3 从来没被
   写过**：没有封禁能力，也没有注销能力。这次把状态真正用起来（写入口见
   ``account/service.py`` 的 ``ban_user`` / ``close_account``）。

2. ``uk_user_phone_hash`` 从**普通唯一约束**改成**部分唯一索引**（``WHERE status <> 3``）。
   ★ 这是本迁移唯一有风险的一步，动机：

   注销是终态、不可恢复。若手机号仍是普通唯一约束，注销后那个号就被**永久占死**，
   用户想回来也回不来 —— 是典型的客服投诉点。改成部分索引后，已注销的行不参与
   唯一性，手机号可以腾出来。

   但**不能一注销就腾**：那样"注销 → 立刻重注册"就能反复领新人券。所以注销时
   手机号仍占位，满 30 天冷静期后由 ``account/service.register`` **惰性**把旧行的
   ``phone_hash`` 改写成占位值（``hash("closed:{user_id}")``），索引随之腾出。
   惰性执行，不需要定时任务。先例：``product.sku`` 的 ``uk_sku_spu_code`` 也是
   drop_constraint + 部分唯一索引（见 c4d9e13f7b58）。

3. ``account.user_state_flow`` —— 不可变审计流水，仿 ``trade.order_state_flow``。
   封禁/解封/注销/运营重置密码都要留一行："谁在什么时候对谁做了什么、为什么"
   必须查得到，否则处置无法解释。应用账号对这张表只授予 INSERT/SELECT，
   由 ``scripts/harden_grants.py`` 的 ``IMMUTABLE_TABLES`` 收紧（**别忘了加**）。

★ 无回填：现有行全是 ``status = 1``，两个新列留空即可；索引按 ``status <> 3``
  过滤，全表都在索引内，与原来的唯一约束等价。

Revision ID: a4f8e2c1b973
Revises: c5e1a7d3b920
Create Date: 2026-10-06 13:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4f8e2c1b973"
down_revision: Union[str, Sequence[str], None] = "c5e1a7d3b920"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- 1. 账号状态的两个附属字段 ----------
    op.add_column(
        "user",
        sa.Column(
            "ban_reason",
            sa.String(length=255),
            nullable=True,
            comment="封禁理由。会展示给用户（登录失败的文案），解封时清空",
        ),
        schema="account",
    )
    op.add_column(
        "user",
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="注销时间。手机号 30 天冷静期以此为基准",
        ),
        schema="account",
    )

    # ---------- 2. 手机号唯一性 → 只对未注销的行生效 ----------
    op.drop_constraint("uk_user_phone_hash", "user", schema="account", type_="unique")
    op.create_index(
        "uk_user_phone_hash",
        "user",
        ["phone_hash"],
        unique=True,
        schema="account",
        postgresql_where=sa.text("status <> 3"),  # 3 = UserStatus.CLOSED
    )

    # ---------- 3. 账号状态审计流水（不可变） ----------
    op.create_table(
        "user_state_flow",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            nullable=False,
            comment="不建外键：注销会匿名化用户行，审计行要能独立留存",
        ),
        sa.Column(
            "event",
            sa.String(length=32),
            nullable=False,
            comment="BAN/UNBAN/CLOSE/RESET_PASSWORD",
        ),
        sa.Column("from_status", sa.SmallInteger(), nullable=False),
        sa.Column("to_status", sa.SmallInteger(), nullable=False),
        sa.Column(
            "operator_type",
            sa.SmallInteger(),
            nullable=False,
            comment="复用 OperatorType：1用户 2商家 3系统 4平台",
        ),
        sa.Column(
            "operator_id",
            sa.String(length=64),
            nullable=True,
            comment="管理员 user id；自助注销时就是被注销的用户自己",
        ),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="account",
    )
    op.create_index(
        "idx_user_state_flow_user",
        "user_state_flow",
        ["user_id", "created_at"],
        schema="account",
    )


def downgrade() -> None:
    """回退。

    ★ 恢复普通唯一约束是**可能失败**的：若退之前已经存在 ``status = 3`` 的行，
      它们的 ``phone_hash`` 是占位值（按 ``user_id`` 生成，彼此不重复），所以
      重建唯一约束本身不会撞 —— 但那些号**在旧代码里就再也注册不了了**，
      因为旧代码看的是普通唯一约束。回退生产库前请人工确认已注销行的数量。
    """
    op.drop_index("idx_user_state_flow_user", table_name="user_state_flow", schema="account")
    op.drop_table("user_state_flow", schema="account")

    op.drop_index("uk_user_phone_hash", table_name="user", schema="account")
    op.create_unique_constraint("uk_user_phone_hash", "user", ["phone_hash"], schema="account")

    op.drop_column("user", "closed_at", schema="account")
    op.drop_column("user", "ban_reason", schema="account")
