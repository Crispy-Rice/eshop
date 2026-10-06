"""notify 模块 —— 站内信。

``notify`` 这个 schema 在基线迁移里就建好了，``docs/13-schema.md §1`` 也一直把它
列在表清单里（"站内信，千万级，满一年可删"），但**表从未建出来** —— 这一轮才落地。

它有两个来源，走的是**两条不同的路**（docs/19 §3）：

1. **客服回复** —— ``support`` 在**自己的业务事务里直写**（``notify_service.push``）。
   站内信与业务**同库**，能随事务一起回滚，outbox 想解决的问题（Redis / 短信这类
   **回滚不了**的副作用）在这里根本不存在。
2. **既有的 6 个 outbox topic** —— ``trade`` / ``payment`` / ``aftersale`` **不能**
   import 本模块（那是给文档里的依赖图加边），所以它们继续写 ``core.local_message``，
   由 worker 的投递循环分派进来（``worker/outbox_delivery.py`` + ``notify/handlers.py``）。

★ 未读用 ``is_read``（每行一个布尔），**不像会话那样用游标**：站内信是**平铺的流**，
  按行标记才是它天然的粒度；会话要按会话分组算，才用游标。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

MAX_TITLE_LEN = 120
MAX_BODY_LEN = 512
MAX_BIZ_KEY_LEN = 160

# link_type = NONE 时 link_value 为空，前端不渲染跳转
LINK_NONE = "NONE"

# 站内信类型文案。**与 core.enums.SiteMsgType 的取值一一对应**
# （枚举只定义码，中文文案统一放这里，照 aftersale.models 的做法）
SITE_MSG_TYPE_TEXT: dict[str, str] = {
    "ORDER_CLOSED": "订单关闭",
    "ORDER_PAID": "支付成功",
    "REFUND_SUCCEEDED": "退款到账",
    "SUPPORT_REPLY": "客服回复",
}


class SiteMessage(Base):
    """站内信。"""

    __tablename__ = "site_message"
    __table_args__ = (
        # ★ 幂等键：同一件事只发一条。既挡住 outbox 的**至少一次**投递，
        #   也挡住"客服连发两条一样的回复"这种手滑。
        UniqueConstraint("biz_key", name="uk_site_message_biz"),
        Index("idx_site_message_user", "user_id", "is_read", text("created_at DESC")),
        {"schema": "notify", "comment": "站内信"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    msg_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="见 core.enums.SiteMsgType"
    )
    title: Mapped[str] = mapped_column(String(MAX_TITLE_LEN), nullable=False)
    body: Mapped[str] = mapped_column(
        String(MAX_BODY_LEN), nullable=False, server_default=text("''")
    )
    biz_key: Mapped[str] = mapped_column(
        String(MAX_BIZ_KEY_LEN), nullable=False, comment="幂等键：同一件事只发一条"
    )
    link_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text(f"'{LINK_NONE}'")
    )
    link_value: Mapped[str | None] = mapped_column(String(64), comment="订单号 / 售后单号 / 会话号")
    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    read_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
