"""support 模块的纯函数。不碰库、不碰 Redis，便于单测（对标 inventory/routing.py）。"""

from __future__ import annotations

from app.modules.support.models import SENDER_USER, TICKET_OPEN


def staff_owes_reply(status: int, last_sender_type: int) -> bool:
    """客服（商家 / 平台）是否欠一个回复 —— 后台队列的「待回复」筛的就是它。

    ★ 由**最后一条消息的发送方**推出来，不另存一列状态：
      少一列 = 少一处能不一致的地方。
    """
    return status == TICKET_OPEN and last_sender_type == SENDER_USER


def is_buyer_sender(sender_type: int) -> bool:
    """这条消息是不是买家发的。

    未读因此只有两种口径：买家侧的未读 = 对方发的；客服侧的未读 = 买家发的。
    """
    return sender_type == SENDER_USER
