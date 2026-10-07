"""support 模块的纯函数。不碰库、不碰 Redis，便于单测（对标 inventory/routing.py）。"""

from __future__ import annotations

from app.modules.support.models import SENDER_USER, TICKET_OPEN


def staff_owes_reply(status: int, last_sender_type: int, need_human: bool) -> bool:
    """客服（商家 / 平台）是否欠一个回复 —— 后台队列的「待回复」筛的就是它。

    ★ 判定是**两句话的或**：最后一条是买家，**或者**这条会话已经转人工。
      第二项不能省：AI 一开口「最后一条是买家」就不成立了 —— 它答完球在买家手里
      （对），但它答不了时也掉出队列（错，那恰恰是要人工的一条）。

    ★ SQL 那份在 ``models.OWES_REPLY_WHERE``（列表筛选与角标 count 共用），
      这里必须与它逐字对齐；有集成测试钉着"三处一致"。
    """
    return status == TICKET_OPEN and (last_sender_type == SENDER_USER or need_human)


def is_buyer_sender(sender_type: int) -> bool:
    """这条消息是不是买家发的。

    未读因此只有两种口径：买家侧的未读 = 对方发的；客服侧的未读 = 买家发的。
    """
    return sender_type == SENDER_USER
