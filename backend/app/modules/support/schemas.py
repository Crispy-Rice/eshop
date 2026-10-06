"""support 模块的请求/响应模型。

约定同全站：请求/响应都走 ``CamelModel``（JSON 侧 camelCase），雪花 ID 在 JSON 里
是**字符串**（``SnowflakeId``），时间戳是 ISO 字符串。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.core.schemas import CamelModel, SnowflakeId
from app.modules.support.models import (
    MAX_BODY_LEN,
    MAX_IMAGES,
    PLATFORM_SHOP_ID,
    SOURCE_OTHER,
)


class OpenTicketRequest(CamelModel):
    """开会话。

    ★ **不带首条消息**：首条消息走 ``POST /tickets/{no}/messages``。分两步的好处是
      "开会话"天然幂等（同一对象已有进行中的会话就把那条还给你），而"发消息"
      自己带 ``Idempotency-Key`` —— 两者各自好推理，不必在一个接口里兼顾。
    """

    shop_id: SnowflakeId | None = Field(
        default=None, description=f"不传 = 平台级会话（内部记 {PLATFORM_SHOP_ID}）"
    )
    source: int = Field(default=SOURCE_OTHER, ge=1, le=5)
    subject: str | None = Field(default=None, max_length=120, description="不传则按来源自动生成")
    order_main_no: str | None = Field(default=None, max_length=32)
    order_sub_no: str | None = Field(default=None, max_length=32)
    refund_no: str | None = Field(default=None, max_length=32)


class SendMessageRequest(CamelModel):
    body: str = Field(min_length=1, max_length=MAX_BODY_LEN)
    images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)


class CloseTicketRequest(CamelModel):
    reason: str | None = Field(default=None, max_length=255)


class TicketMessageOut(CamelModel):
    id: SnowflakeId
    sender_type: int
    sender_type_text: str
    sender_id: SnowflakeId | None = None
    body: str
    images: list[str]
    created_at: datetime


class TicketContextOut(CamelModel):
    """弱关联的上下文。

    ★ 服务端**只当展示用**，绝不用它做鉴权 —— 归属永远来自会话自己的 ``user_id`` /
      ``shop_id``，而点进去的订单页 / 售后页各自还有自己的归属校验。
    """

    order_main_no: str | None = None
    order_sub_no: str | None = None
    refund_no: str | None = None


class TicketListItemOut(CamelModel):
    ticket_no: str
    shop_id: SnowflakeId
    shop_name: str
    user_id: SnowflakeId
    buyer_nickname: str
    buyer_phone: str
    subject: str
    source: int
    source_text: str
    status: int
    status_text: str
    last_message_at: datetime
    last_sender_type: int
    last_sender_text: str
    unread: int
    staff_owes_reply: bool
    created_at: datetime


class TicketListOut(CamelModel):
    items: list[TicketListItemOut]
    next_cursor: str | None = None
    has_more: bool


class TicketCountOut(CamelModel):
    """「待回复」条数（后台导航角标）。"""

    count: int


class TicketDetailOut(CamelModel):
    ticket_no: str
    shop_id: SnowflakeId
    shop_name: str
    user_id: SnowflakeId
    buyer_nickname: str
    buyer_phone: str
    subject: str
    source: int
    source_text: str
    status: int
    status_text: str
    context: TicketContextOut
    last_message_at: datetime
    staff_owes_reply: bool
    close_by_text: str | None = None
    close_reason: str | None = None
    close_time: datetime | None = None
    created_at: datetime
    messages: list[TicketMessageOut]
    # 还有更早的消息可以往前翻（P0 只带"最近一页"，往前翻用 ?before=<id>）
    has_more_messages: bool
    next_message_cursor: str | None = None
