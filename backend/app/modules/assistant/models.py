"""assistant 模块的 ORM 模型，见 docs/20-assistant.md。六张表，两个界面各三张：

**后台助手**（商家 / 平台运营那个悬浮面板）

1. ``conversation`` —— 会话。**一个用户可以有任意多条**，每条各自能翻、能切回去继续聊。
   ★ 曾经有"每用户同时只有一条进行中"这条不变量（部分唯一索引），**已去掉** ——
   它当初是为了省掉会话列表才加的，而列表现在是需求本身。
2. ``message`` —— 消息，只追加。**只存 user / assistant 两种角色**。
3. ``usage_daily`` —— token 记账，按 (日, 主体) 累加，支撑每日预算。两个界面共用。

**店小蜜**（买家在商城端问，AI 以店铺身份答）

4. ``shop_setting`` —— 一店一行的开关（**默认关**，由商户自己打开）。
5. ``shop_faq`` —— 商户自己维护的问答（这家店的话）。
6. ``bot_turn`` —— 每次回答的执行台账：``source_message_id`` 唯一 = **幂等闩锁**，
   同时留审计与 token。**消息本身不在这里** —— 它只落 ``support.ticket_message``，
   一个事实只有一个来源。

★ **``shop_id`` 这里用 NULL 表示"运营没有店铺"，不用 support 那套哨兵 0。**
  support 用哨兵是因为它的唯一索引建在 ``(user_id, shop_id)`` 上，而 PG 把 NULL
  当作互不相同，可空会让"一条进行中会话"的约束失效。这里**没有任何涉及 shop_id
  的唯一索引**，所以 NULL 是安全的，而且比 0 诚实 ——
  "没有店铺"是**不存在**，不是"店铺零号"。

★ **不存 tool 角色的消息行**（原设计有，砍掉了）。工具调用与结果只落进
  ``message.tool_calls`` / ``bot_turn.tool_calls`` 作为**审计**，不参与下一轮的历史。
  两个理由：
  1. 下一轮只需要 user / assistant 的**对话文本**，模型要数据会重新调工具；
  2. 若把上一轮的工具结果当历史喂回去，模型会把**过期的数据**当成当前事实 ——
     这不只是冗余，是**会答错**。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# ---------------- 消息角色 ----------------
MESSAGE_USER = 1
MESSAGE_ASSISTANT = 2

# ---------------- 消息状态 ----------------
# **只有 assistant 行会离开 DONE**：user 行写进去就是终态。
MESSAGE_PENDING = 10  # 已入队，等 worker 取
MESSAGE_RUNNING = 20  # worker 正在调模型 / 工具
MESSAGE_DONE = 30
MESSAGE_FAILED = 40  # 上游错误、超时、身份失效
MESSAGE_DEGRADED = 50  # 答上来了但信息可能不完整（轮次用尽 / 反复调同一工具）
INFLIGHT_STATUSES = (MESSAGE_PENDING, MESSAGE_RUNNING)

# ---------------- 记账主体 ----------------
USAGE_SCOPE_USER = 1
USAGE_SCOPE_SHOP = 2

# 提问者角色。**直接用 core.enums.UserRole 的字符串值**，不再造一套小整数映射 ——
# 多一套映射就多一处能不一致的地方，而这里除了 merchant / admin 不会有第三种。
ACTOR_MERCHANT = "merchant"
ACTOR_ADMIN = "admin"
# ★ 第三个作用域：**买家**。店小蜜以「这家店」的身份回答「这个买家」，
#   所以它的工具面是"本店目录 × 本人订单"的交集（比商家面窄得多）。
ACTOR_BUYER = "buyer"

# ---------------- 店小蜜：一次回答的执行台账 ----------------
# **只有"买家说了话、该 AI 答"这件事才能产生一行**，所以 source_message_id 是
# 幂等闩锁（唯一）：同一条买家消息永远只答一次，扫描多跑几轮也只会撞唯一键。
BOT_PENDING = 10  # 已排上，等 worker 取
BOT_RUNNING = 20  # worker 正在调模型
BOT_DONE = 30  # 答了
BOT_FAILED = 40  # 上游失败等，答不了（已转人工）
BOT_SKIPPED = 50  # **没答**：开关关了 / 已转人工 / 真人抢答 / 纯图 / 额度用尽
BOT_INFLIGHT_STATUSES = (BOT_PENDING, BOT_RUNNING)


class Conversation(Base):
    """一条助手会话。**一个用户可以有任意多条**（没有"同时只有一条"的约束了）。"""

    __tablename__ = "conversation"
    __table_args__ = (
        UniqueConstraint("conversation_no", name="uk_assistant_conversation_no"),
        # 会话列表与"最近一条"都按 (owner, updated_at 倒序) 取 ——
        # 原来的唯一索引是 owner_user_id 上**唯一**的索引，删掉它就得补这一条
        Index(
            "idx_assistant_conversation_owner",
            "owner_user_id",
            text("updated_at DESC"),
        ),
        {"schema": "assistant", "comment": "AI 助手会话"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="雪花")
    conversation_no: Mapped[str] = mapped_column(String(32), nullable=False, comment="会话号")
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 提问那一刻的角色快照。商家与运营看到的工具集不同，历史要能解释"当时为什么那样答"
    owner_role: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="merchant / admin，提问那一刻的快照"
    )
    shop_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="商家=本店；运营=NULL（见模块 docstring：这里 NULL 是安全的）",
    )
    title: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("''"), comment="首个问题的截断"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class Message(Base):
    """会话里的一条消息。只追加。

    ★ ``id`` 用 ``Identity`` 而不是雪花：它是**日志表**，严格保序比"ID 里带时间"重要
      （两轮问答可能落在同一毫秒）。``trade.order_item`` / ``support.ticket_message``
      都是这个取舍。
    """

    __tablename__ = "message"
    __table_args__ = (
        Index("idx_assistant_message_conv", "conversation_no", "id"),
        # reaper 扫描用：只索引"还在飞"的那几行，稳态下索引几乎为空
        Index(
            "idx_assistant_message_inflight",
            "created_at",
            postgresql_where=text("status IN (10, 20)"),
        ),
        # ★ 这两条**必须**和迁移里建的一致。Alembic 的 autogenerate **不比对
        #   CHECK 约束**（官方限制），所以模型里漏声明它也不会报 —— 但在
        #   ``create_all`` / 重建表的场合，约束就真的不存在了。
        CheckConstraint("role IN (1, 2)", name="role_valid"),
        CheckConstraint("status IN (10, 20, 30, 40, 50)", name="status_valid"),
        {"schema": "assistant", "comment": "AI 助手消息（含审计）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("assistant.conversation.conversation_no"), nullable=False
    )
    role: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="1 用户 2 助手")
    status: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text(str(MESSAGE_DONE)),
        comment="user 行恒为 30；assistant 行从 10 出发（10待处理 20处理中 "
        "30完成 40失败 50降级）",
    )
    content: Mapped[str | None] = mapped_column(Text, comment="答案正文；失败时为空")
    error_code: Mapped[str | None] = mapped_column(
        String(32), comment="失败时的 ErrorCode 名，前端据此给不同文案"
    )
    # ★ 审计：这一轮为了回答而调的每个工具（名字 / 参数 / 耗时 / 结果摘要）。
    #   不参与后续轮次的历史，见模块 docstring。
    tool_calls: Mapped[Any | None] = mapped_column(
        JSONB,
        comment="工具调用审计（名字/参数/耗时/结果摘要，已脱敏截断）。"
        "★ 不参与后续轮次的历史，见 models.py 的模块 docstring",
    )

    llm_model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # ★ 提交时冻结的身份快照。worker **只信这一份**，绝不信模型给的任何东西，
    #   也绝不信 job 参数（入队到执行之间，参数可能被改）。
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="身份快照，worker 只信它"
    )
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_shop_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class UsageDaily(Base):
    """每日 token 记账，按 (日, 主体) 累加。

    ★ **账本放 PG 而不是 Redis**：演示机的 Redis 是 ``maxmemory 192MB`` 带淘汰策略的，
      预算计数器一旦被淘汰就等于**静默变成无限额**。限流计数仍在 Redis（被淘汰只是
      短暂松一下，无害），但"花了多少"必须落在不会自己消失的地方。
    """

    __tablename__ = "usage_daily"
    __table_args__ = (
        UniqueConstraint("day", "scope", "scope_id", name="uk_assistant_usage_daily"),
        Index("idx_assistant_usage_day", "day"),
        CheckConstraint("scope IN (1, 2)", name="scope_valid"),
        {"schema": "assistant", "comment": "AI 助手每日 token 记账"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    day: Mapped[str] = mapped_column(String(8), nullable=False, comment="yyyymmdd（UTC）")
    scope: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1 按用户 2 按店铺"
    )
    scope_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class ShopSetting(Base):
    """店铺的智能客服开关（店小蜜）。

    ★ **默认关**。这是唯一一个"AI 面向的是公众（买家）"的开关，所以由**商户自己**
      决定打开 —— 平台不替他们开。没有这一行 = 没开过 = 关。
    """

    __tablename__ = "shop_setting"
    __table_args__ = ({"schema": "assistant", "comment": "店铺的智能客服设置"},)

    # ★ 主键就是店铺 id：一店一行，不需要另造雪花（``promotion.site_theme`` 的
    #   「单行配置」是同一个思路，只是这里每个店一行）。
    shop_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, comment="一店一行，主键即店铺"
    )
    ai_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="智能客服是否对买家开放。**默认 false**，由商户自己打开",
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class ShopFaq(Base):
    """商户自己维护的问答 —— 店小蜜的知识库。

    ★ 与 ``knowledge/*.md``（平台买家规则）分开：那些是**平台规则**，改它要发版；
      这些是**这家店自己的话**（发货时效、发票、能不能议价…），随店铺走。

    ★ 不做 ``sort`` 列：按 id 排就够了（这里是"一问一答的清单"，不是排版）
    """

    __tablename__ = "shop_faq"
    __table_args__ = (
        Index("idx_assistant_shop_faq_shop", "shop_id", "id"),
        {"schema": "assistant", "comment": "商户维护的问答（店小蜜知识库）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="雪花")
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(String(200), nullable=False, comment="买家可能怎么问")
    answer: Mapped[str] = mapped_column(String(1000), nullable=False, comment="该怎么答")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="停用但不删"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class BotTurn(Base):
    """店小蜜**一次回答**的执行台账。

    ★ 两个身份，缺一不可：
      1. **幂等闩锁** —— ``source_message_id`` 唯一。扫描是每 3 秒一轮的，
         靠它把"同一条买家消息"收敛成一行、一次回答。
      2. **审计** —— 它调了哪些工具、花了多少 token。消息本身只落
         ``support.ticket_message``（一个事实只有一个来源）。
    """

    __tablename__ = "bot_turn"
    __table_args__ = (
        UniqueConstraint("source_message_id", name="uk_assistant_bot_turn_source"),
        Index("idx_assistant_bot_turn_ticket", "ticket_no", text("id DESC")),
        # reaper 扫描用：只索引"还在飞"的行
        Index(
            "idx_assistant_bot_turn_inflight",
            "created_at",
            postgresql_where=text(f"status IN ({BOT_PENDING}, {BOT_RUNNING})"),
        ),
        # ★ 与迁移里建的那条**必须**一致：Alembic 不比对 CHECK（见 Message 的注释）
        CheckConstraint("status IN (10, 20, 30, 40, 50)", name="status_valid"),
        {"schema": "assistant", "comment": "店小蜜每次回答的执行台账"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="雪花")
    source_message_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="触发这一轮的买家消息（support.ticket_message.id）—— 幂等闩锁",
    )
    ticket_no: Mapped[str] = mapped_column(String(32), nullable=False)
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    buyer_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="提问的买家；工具身份快照"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text(str(BOT_PENDING))
    )
    content: Mapped[str | None] = mapped_column(Text, comment="最终答复；没答时为空")
    error_code: Mapped[str | None] = mapped_column(String(32))
    tool_calls: Mapped[Any | None] = mapped_column(
        JSONB, comment="工具调用审计（名字/参数/耗时/结果摘要）"
    )
    llm_model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
