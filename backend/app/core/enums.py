"""领域枚举。数据库里存 SMALLINT，代码里用 IntEnum。

集中定义，避免同一套状态码在不同模块里各写一遍数字
（docs/13-schema.md §8）。
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class SubOrderStatus(IntEnum):
    """子单状态机（docs/07-order-and-split.md §4.1）。母单复用同一套编码。"""

    WAIT_PAY = 10
    WAIT_DELIVER = 20
    WAIT_RECEIVE = 30
    FINISHED = 40
    CLOSED = 50
    REFUNDING = 60
    REFUNDED = 70


class OrderEvent(StrEnum):
    """触发子单状态流转的事件。"""

    PAY_SUCCESS = "PAY_SUCCESS"
    USER_CANCEL = "USER_CANCEL"
    TIMEOUT_CANCEL = "TIMEOUT_CANCEL"
    SHIP = "SHIP"
    PARTIAL_SHIP = "PARTIAL_SHIP"
    CONFIRM_RECEIVE = "CONFIRM_RECEIVE"
    AUTO_RECEIVE = "AUTO_RECEIVE"
    APPLY_REFUND = "APPLY_REFUND"
    APPLY_AFTERSALE = "APPLY_AFTERSALE"
    REFUND_SUCCESS = "REFUND_SUCCESS"
    REFUND_REJECT = "REFUND_REJECT"
    USER_REVOKE = "USER_REVOKE"


class RefundStatus(IntEnum):
    """售后单状态机（docs/08-aftersale.md §3.1）。"""

    APPLYING = 10
    MERCHANT_REJECTED = 11
    WAIT_REFUND = 20
    WAIT_RETURN = 30
    WAIT_RECEIVE = 40
    QUALITY_CHECKING = 50
    QUALITY_FAILED = 51
    REFUNDING = 60
    SUCCESS = 70
    CLOSED = 80
    USER_REVOKED = 81
    PLATFORM_INTERVENING = 90


class PayStatus(IntEnum):
    WAIT_PAY = 0
    PAYING = 1
    SUCCESS = 2
    FAILED = 3
    CLOSED = 4
    REFUNDED = 5


class ChannelType(IntEnum):
    MOCK = 0
    WECHAT = 1
    ALIPAY = 2


class PayStatusOfOrder(IntEnum):
    """母单的支付状态，与履约状态正交（docs/07 §5）。"""

    UNPAID = 0
    PAID = 1
    PARTIAL_REFUNDED = 2
    FULLY_REFUNDED = 3


class CouponStatus(IntEnum):
    UNUSED = 1
    LOCKED = 2
    USED = 3
    EXPIRED = 4
    VOIDED = 5


class StockChangeType(IntEnum):
    LOCK = 1
    CONFIRM = 2
    RELEASE = 3
    DELIVER = 4
    RETURN_IN = 5
    MANUAL = 6
    INIT = 7


class PointsChangeType(IntEnum):
    ORDER_USE = 1
    ORDER_FREEZE = 2
    PAY_CONFIRM = 3
    REFUND = 4
    SHOPPING_AWARD = 5
    REVIEW_AWARD = 6
    EXPIRE = 7
    SIGN_IN = 8
    MANUAL = 9


class ReviewStatus(IntEnum):
    PENDING_AUDIT = 0
    PUBLISHED = 1
    BLOCKED = 2
    REJECTED = 3


class UserRole(StrEnum):
    BUYER = "buyer"
    MERCHANT = "merchant"
    ADMIN = "admin"
    FINANCE = "finance"


class UserStatus(IntEnum):
    """账号状态（``account/models.py`` 的 ``User.status``）。

    ★ 2 与 3 的**唯一写入口**是 ``account/service`` 的 ``ban_user`` /
      ``unban_user`` / ``close_account`` —— 它们同时负责吊销令牌与写审计流水。
      别在别处直接改 status，否则登录拦截与审计会对不上。
    """

    NORMAL = 1
    BANNED = 2
    CLOSED = 3


class OperatorType(IntEnum):
    """状态流水的操作者类型（docs/07 §4.5）。"""

    USER = 1
    MERCHANT = 2
    SYSTEM = 3
    PLATFORM = 4


class TicketStatus(IntEnum):
    """客服会话状态（docs/19 §2）。

    ★ **只有两个状态**。"欠谁一个回复"由 ``ticket.last_sender_type`` 推出来，
      不另存一列 —— 少一列状态 = 少一处能不一致的地方。
      （``RefundStatus`` 存 ``source_status`` 是因为拒绝时要回到**原状态**，
      客服没有这个需求，所以不学它。）
    """

    OPEN = 10
    CLOSED = 30


class SiteMsgType(StrEnum):
    """站内信类型（``notify.site_message.msg_type``）。

    前三个由 outbox 的投递循环落库，``SUPPORT_REPLY`` 由客服回复时同事务直写。
    """

    ORDER_CLOSED = "ORDER_CLOSED"
    ORDER_PAID = "ORDER_PAID"
    REFUND_SUCCEEDED = "REFUND_SUCCEEDED"
    SUPPORT_REPLY = "SUPPORT_REPLY"


class MsgLinkType(StrEnum):
    """站内信点进去跳哪。``NONE`` = 纯文本，前端不渲染跳转。"""

    NONE = "NONE"
    ORDER = "ORDER"
    REFUND = "REFUND"
    TICKET = "TICKET"
