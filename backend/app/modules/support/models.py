"""support 模块的 ORM 模型 —— 买家 ↔ 商家 / 平台的**异步客服工单**。

对应 docs/19-support.md 与 docs/13-schema.md §1。三张表：

1. ``ticket`` —— 会话。一个买家对一个对象（一个店铺，或平台）一条。
2. ``ticket_message`` —— 消息，**只追加，不可改不可删**（客服也不能删自己的话）。
3. ``ticket_state_flow`` —— 状态流水（命名照 ``trade.order_state_flow`` /
   ``account.user_state_flow``）：回答"这条会话什么时候被谁关的、被谁重开过"。

★ **``shop_id`` 用哨兵 ``0`` 表示平台级，不用 NULL。** PG 的唯一索引把 NULL 当作
  **互不相同**，可空的话 "同一用户 + 同一个对象只能有一条进行中会话" 这个部分唯一
  索引就形同虚设 —— 能开出无数条平台级会话。``0`` 与 ``inventory/routing.REGION_ALL``
  ("0" = 全国) 是同一套哨兵思路。

★ 设计取舍（docs/19 §2）：

- **状态只存两个**（10 进行中 / 30 已关闭）。"欠谁一个回复"由 ``last_sender_type``
  推出来，不另存一列 —— 少一列状态 = 少一处能不一致的地方。
  （``refund_order`` 存 ``source_status`` 是因为拒绝时要回退到**原状态**，客服没有
  这个需求，所以不学它。）
- **未读用两条游标**（``user_read_at`` / ``staff_read_at``），不建按消息的已读表。
  一期一条会话只由一个客服处理，没有"多客服各自未读"的需求；一旦引入坐席分配，
  这里就得变 N×M —— 两者是绑定的取舍。
- **冗余的未读计数不存。** 游标算未读是**一次分组查询**（``repository.unread_of``），
  再存一列计数就是同一事实的第二个来源。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# ---------------- 会话状态（与 core.enums.TicketStatus 一一对应）----------------
TICKET_OPEN = 10
TICKET_CLOSED = 30

TICKET_STATUS_TEXT: dict[int, str] = {
    TICKET_OPEN: "进行中",
    TICKET_CLOSED: "已关闭",
}

# 「进行中」的条件，部分唯一索引与列表筛选共用同一份定义
TICKET_ACTIVE_WHERE = f"status <> {TICKET_CLOSED}"

# ---------------- 会话来源（决定详情页默认展开什么上下文）----------------
SOURCE_PRODUCT = 1
SOURCE_ORDER = 2
SOURCE_AFTERSALE = 3
SOURCE_APPEAL = 4
SOURCE_OTHER = 5
# AI 助手答不了 → 转人工。与前面几个的区别是"从哪来的"：那些是买家从商品页/
# 订单页点进来的，这个是**后台助手把问题转交过来**，首条消息里带着对话摘要
SOURCE_AI_ASSISTANT = 6

SOURCE_TEXT: dict[int, str] = {
    SOURCE_PRODUCT: "商品咨询",
    SOURCE_ORDER: "订单咨询",
    SOURCE_AFTERSALE: "售后咨询",
    SOURCE_APPEAL: "账号申诉",
    SOURCE_OTHER: "其他",
    SOURCE_AI_ASSISTANT: "助手转人工",
}

# 来源的**上界**。``OpenTicketRequest.source`` 的 ``le`` 用它，不再写死数字 ——
# 加了来源却忘了改上界，表现是调用方收到一个看不出原因的 422
# （``product`` 那边的 status 上界踩过同一个坑，见 product/router.py 的注释）。
SOURCE_MAX = SOURCE_AI_ASSISTANT

# ---------------- 消息发送方 ----------------
SENDER_USER = 1
SENDER_MERCHANT = 2
SENDER_PLATFORM = 3
SENDER_SYSTEM = 4
# 店铺侧的智能客服（店小蜜）。它**不是人**，所以在读游标与站内信上必须与
# 真人区分开（见 service._reply 里那段白名单）。
SENDER_AI = 5

SENDER_TEXT: dict[int, str] = {
    SENDER_USER: "买家",
    SENDER_MERCHANT: "商家",
    SENDER_PLATFORM: "平台客服",
    SENDER_SYSTEM: "系统",
    # ★ 面向公众的生成式 AI 必须**标识**自己。这个字符串就是标识本身，
    #   前端直接渲染后端给的这个文案（商城端没有 1-4 的映射表），改这里就够
    SENDER_AI: "智能客服",
}

# 关闭/重开的发起方
CLOSE_BY_TEXT: dict[int, str] = SENDER_TEXT

# 「后台（商家/平台）欠一个回复」的条件 —— 商家队列的「待回复」与角标共用。
#
# ★ 与 TICKET_ACTIVE_WHERE 同一套路：写成常量，让两处 SQL（列表筛选、角标 count）
#   与 rules.staff_owes_reply 说的是**同一句话**，不会各自漂移。
#
# ★ 为什么要 `need_human_at` 这一项：原来「欠回复」只看"最后一条是买家"。
#   但 AI 一开口这条就不成立了 —— AI 答完球在买家手里（对，不该进队列）；
#   而 **AI 答不了时也掉出队列**（错，那恰恰是必须人工的一条）。
#   所以"已转人工"要单独记一笔：它表达的是**最后一条消息推不出来的事实**。
OWES_REPLY_WHERE = (
    f"status = {TICKET_OPEN}"
    f" AND (last_sender_type = {SENDER_USER} OR need_human_at IS NOT NULL)"
)

# 平台级会话的哨兵店铺 id（见模块 docstring）
PLATFORM_SHOP_ID = 0

MAX_BODY_LEN = 2000
MAX_IMAGES = 9


class Ticket(Base):
    """客服会话。"""

    __tablename__ = "ticket"
    __table_args__ = (
        UniqueConstraint("ticket_no", name="uk_ticket_no"),
        # ★ 一个用户对同一个对象同时只有一条进行中会话。关闭的不占用 ——
        #   所以"关了可以再开"，而"进行中再点联系客服"返回的是同一条（不是报错）。
        Index(
            "uk_ticket_user_shop_active",
            "user_id",
            "shop_id",
            unique=True,
            postgresql_where=text(TICKET_ACTIVE_WHERE),
        ),
        Index("idx_ticket_user", "user_id", "status", text("last_message_at DESC")),
        Index("idx_ticket_shop", "shop_id", "status", text("last_message_at DESC")),
        CheckConstraint(f"status IN ({TICKET_OPEN}, {TICKET_CLOSED})", name="status_valid"),
        {"schema": "support", "comment": "客服会话"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="买家")
    shop_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text(str(PLATFORM_SHOP_ID)),
        comment="0 = 平台级会话。**不可空**，见模块 docstring",
    )

    subject: Mapped[str] = mapped_column(String(120), nullable=False, comment="一句话标题")
    source: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("5"), comment="见 SOURCE_TEXT"
    )

    # 上下文快照：弱关联，详情页据此**跳转**回既有页面，不在会话里重复渲染
    order_main_no: Mapped[str | None] = mapped_column(String(32))
    order_sub_no: Mapped[str | None] = mapped_column(String(32))
    refund_no: Mapped[str | None] = mapped_column(String(32))
    # ★ 商品上下文。**只放 id，不放标题** —— 标题是快照会过期，而 id 永远指向
    #   当前那份数据（商品改名/改价后，会话里说的应该是**现在**的样子）。
    #   店小蜜的 ``ticket_product`` 工具读它（服务端注入，不由模型指定）。
    spu_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="会话的商品上下文（商品页点进来时带上）"
    )

    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text(str(TICKET_OPEN))
    )
    last_message_at: Mapped[datetime] = mapped_column(
        TS, nullable=False, server_default=func.now(), comment="列表排序键"
    )
    last_sender_type: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        # ★ 初值是 **系统**（不是买家）：会话刚开出来时**还没有人说过话**。
        #   默认成买家的话，一个"点了联系客服但没发消息就走了"的空会话
        #   会在后台永远显示成「待回复」—— 「欠谁回复」的判定只看这一列。
        server_default=text(str(SENDER_SYSTEM)),
        comment="最后一条消息的发送方 —— 「欠谁回复」由它推。新建时为系统",
    )

    user_read_at: Mapped[datetime | None] = mapped_column(TS, comment="买家读到哪了")
    staff_read_at: Mapped[datetime | None] = mapped_column(TS, comment="商家/平台读到哪了")

    need_human_at: Mapped[datetime | None] = mapped_column(
        TS,
        comment="已转人工的时刻。买家点「转人工」或 AI 判定答不了时置位，"
        "客服回复或关单时清空 —— 「欠回复」判定要用（见 OWES_REPLY_WHERE）",
    )

    close_by: Mapped[int | None] = mapped_column(SmallInteger, comment="见 CLOSE_BY_TEXT")
    close_reason: Mapped[str | None] = mapped_column(String(255))
    close_time: Mapped[datetime | None] = mapped_column(TS)

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class TicketMessage(Base):
    """会话消息。**只追加** —— 与流水表同级的审计要求。"""

    __tablename__ = "ticket_message"
    __table_args__ = (
        Index("idx_ticket_message_ticket", "ticket_no", "created_at", "id"),
        {"schema": "support", "comment": "客服会话消息"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ticket_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("support.ticket.ticket_no"), nullable=False
    )
    sender_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="见 SENDER_TEXT"
    )
    sender_id: Mapped[int | None] = mapped_column(BigInteger, comment="系统消息为空")
    body: Mapped[str] = mapped_column(String(MAX_BODY_LEN), nullable=False)
    images: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="凭证图片"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class TicketStateFlow(Base):
    """会话的状态流水。命名与 ``trade.order_state_flow`` 对齐。

    ★ 只该被追加：``scripts/harden_grants.py`` 会把这张表的 UPDATE/DELETE 从
      应用角色上收走。
    """

    __tablename__ = "ticket_state_flow"
    __table_args__ = (
        Index("idx_ticket_state_flow_ticket", "ticket_no", "created_at"),
        {"schema": "support", "comment": "客服会话状态流水"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    to_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    event: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="OPEN / CLOSE / REOPEN"
    )
    operator_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="见 core.enums.OperatorType"
    )
    operator_id: Mapped[str | None] = mapped_column(String(64))
    remark: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


# 会话状态流转的事件名
EVENT_OPEN = "OPEN"
EVENT_CLOSE = "CLOSE"
EVENT_REOPEN = "REOPEN"
