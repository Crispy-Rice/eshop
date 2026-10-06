"""notify 模块的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from app.core.schemas import CamelModel, SnowflakeId


class SiteMessageOut(CamelModel):
    """一条站内信。"""

    # id 是 IDENTITY 大整数，但**照样按字符串给前端** —— JS 的 number 装不下
    # int64（超过 2^53 会丢精度），与雪花 ID 同一个理由。
    id: SnowflakeId
    msg_type: str
    msg_type_text: str
    title: str
    body: str
    link_type: str
    link_value: str | None = None
    is_read: bool
    created_at: datetime


class SiteMessageListOut(CamelModel):
    items: list[SiteMessageOut]
    next_cursor: str | None = None
    has_more: bool


class UnreadCountOut(CamelModel):
    count: int
