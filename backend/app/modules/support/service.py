"""support 模块的领域逻辑 —— 客服会话。

三个受众共用同一套函数，差别只在**归属校验**与**发送方身份**：

| 受众 | 归属校验 | 发送方 |
|---|---|---|
| 买家 | ``user_id`` | ``SENDER_USER`` |
| 商家 | ``shop_id`` | ``SENDER_MERCHANT`` |
| 平台 | **不校验**（可介入任何会话） | ``SENDER_PLATFORM`` |

★ 归属不对一律 **404**（不是 403）—— 照 ``aftersale/service.py``：不区分"不存在"
  与"不是你的"，否则就是一个可以遍历探测会话号的口子。

★ 站内信**同事务直写**（``notify_service.push``）：与业务同库、能随事务回滚，
  outbox 要解决的"回滚不了的副作用"在这里根本不存在（docs/19 §3）。

★ **读游标由"读"推进，不单独开端点**：打开详情（GET）时把自己的那侧游标推到
  ``now``；发消息时同理（自己发的当然算读过）。所以路由表里没有 ``/read``。
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgLinkType, OperatorType, SiteMsgType
from app.core.errors import BizError, ErrorCode
from app.core.redis_keys import support_send_rate
from app.core.snowflake import next_id
from app.modules.account import service as account_service
from app.modules.notify import service as notify_service
from app.modules.support import repository as repo
from app.modules.support.models import (
    CLOSE_BY_TEXT,
    EVENT_CLOSE,
    EVENT_OPEN,
    EVENT_REOPEN,
    PLATFORM_SHOP_ID,
    SENDER_MERCHANT,
    SENDER_PLATFORM,
    SENDER_TEXT,
    SENDER_USER,
    SOURCE_AFTERSALE,
    SOURCE_ORDER,
    SOURCE_TEXT,
    TICKET_CLOSED,
    TICKET_OPEN,
    TICKET_STATUS_TEXT,
    Ticket,
    TicketMessage,
)
from app.modules.support.rules import staff_owes_reply
from app.modules.support.schemas import (
    OpenTicketRequest,
    TicketContextOut,
    TicketCountOut,
    TicketDetailOut,
    TicketListItemOut,
    TicketListOut,
    TicketMessageOut,
)
from app.modules.trade.order_no import build_ticket_no

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
# 一屏最多给多少条消息。往前翻用 ?before=<最早那条的 id>
MESSAGE_PAGE = 50
# 发消息限流：固定窗口
MAX_MESSAGES_PER_MINUTE = 20
RATE_WINDOW_SECONDS = 60
# 站内信正文预览长度
PREVIEW_LEN = 40
PLATFORM_SHOP_NAME = "平台客服"

# ★ 发送方 → 状态流水的操作者。**两套枚举的数值不一样**（SenderType 里
#   3=平台、4=系统；OperatorType 里 3=系统、4=平台），所以必须显式映射，
#   不能靠数值相等 —— 否则审计流水里平台与系统会对调。
_OPERATOR_OF_SENDER = {
    SENDER_USER: OperatorType.USER,
    SENDER_MERCHANT: OperatorType.MERCHANT,
    SENDER_PLATFORM: OperatorType.PLATFORM,
}


def _text(mapping: dict[int, str], key: int | None, default: str = "") -> str:
    if key is None:
        return default
    return mapping.get(int(key), default)


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _preview(body: str) -> str:
    """站内信正文：压掉换行、截断到 ``PREVIEW_LEN``。"""
    flat = " ".join(body.split())
    return flat if len(flat) <= PREVIEW_LEN else f"{flat[:PREVIEW_LEN]}…"


def _default_subject(source: int, order_main_no: str | None, refund_no: str | None) -> str:
    """没给标题就按来源生成一个 —— 后台列表里一眼能看出这是关于什么的。"""
    if source == SOURCE_ORDER and order_main_no:
        return f"关于订单 {order_main_no}"
    if source == SOURCE_AFTERSALE and refund_no:
        return f"关于售后 {refund_no}"
    return _text(SOURCE_TEXT, source, "咨询")


# ---------------------------------------------------------------
# 游标：与 aftersale 同构（keyset 而非 OFFSET）
#
# 会话按 last_message_at 排，会有并列，所以游标必须是 (时间, id) 复合；
# 两个模块各持一份，不为一个十行的帮助函数新增共享模块。
# ---------------------------------------------------------------
def _encode_cursor(last_message_at: datetime, row_id: int) -> str:
    raw = f"{last_message_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw_time, raw_id = base64.urlsafe_b64decode(padded).decode().split("|")
        return datetime.fromisoformat(raw_time), int(raw_id)
    except (ValueError, TypeError) as exc:
        raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新加载") from exc


async def _check_rate_limit(redis: Redis, user_id: int) -> None:
    """固定窗口计数限流。防的是"客户端卡住狂发"和刷屏，不是防攻击。"""
    key = support_send_rate(user_id)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, RATE_WINDOW_SECONDS)
    if count > MAX_MESSAGES_PER_MINUTE:
        raise BizError(ErrorCode.SUPPORT_RATE_LIMITED)


# ---------------------------------------------------------------
# 输出组装
# ---------------------------------------------------------------
def _to_message(row: TicketMessage) -> TicketMessageOut:
    return TicketMessageOut(
        id=row.id,
        sender_type=int(row.sender_type),
        sender_type_text=_text(SENDER_TEXT, int(row.sender_type)),
        sender_id=row.sender_id,
        body=row.body,
        images=list(row.images or []),
        created_at=row.created_at,
    )


async def _shop_name(session: AsyncSession, shop_id: int) -> str:
    if shop_id == PLATFORM_SHOP_ID:
        return PLATFORM_SHOP_NAME
    try:
        return (await account_service.get_public_shop(session, shop_id)).name
    except BizError:
        # 店铺已注销等情况：不能因为一个名字取不到就让整个列表/详情 500
        return ""


async def _shop_names(session: AsyncSession, shop_ids: Sequence[int]) -> dict[int, str]:
    """批量取店铺名（平台级那个不查库）。同一页通常只来自一两个店。"""
    real = [i for i in dict.fromkeys(int(s) for s in shop_ids) if i != PLATFORM_SHOP_ID]
    names = await account_service.list_shop_names(session, real) if real else {}
    names[PLATFORM_SHOP_ID] = PLATFORM_SHOP_NAME
    return names


async def _buyer_labels(session: AsyncSession, user_ids: Sequence[int]) -> dict[int, tuple[str, str]]:
    """批量取买家的 ``(昵称, 打码手机号)``。

    ★ 打码号就够 —— 平台**刻意不提供**解密查看（``account/router.py`` 的
      用户详情接口注释写明了这条边界），本轮不动它（docs/19 §4）。
    """
    ids = list(dict.fromkeys(int(u) for u in user_ids))
    return await account_service.list_user_labels(session, ids) if ids else {}


def _to_list_item(
    ticket: Ticket,
    *,
    shop_name: str,
    buyer: tuple[str, str],
    unread: int,
) -> TicketListItemOut:
    last_sender = int(ticket.last_sender_type)
    return TicketListItemOut(
        ticket_no=ticket.ticket_no,
        shop_id=ticket.shop_id,
        shop_name=shop_name,
        user_id=ticket.user_id,
        buyer_nickname=buyer[0],
        buyer_phone=buyer[1],
        subject=ticket.subject,
        source=int(ticket.source),
        source_text=_text(SOURCE_TEXT, int(ticket.source)),
        status=int(ticket.status),
        status_text=_text(TICKET_STATUS_TEXT, int(ticket.status)),
        last_message_at=ticket.last_message_at,
        last_sender_type=last_sender,
        last_sender_text=_text(SENDER_TEXT, last_sender),
        unread=unread,
        staff_owes_reply=staff_owes_reply(int(ticket.status), last_sender),
        created_at=ticket.created_at,
    )


async def _to_list(
    session: AsyncSession, rows: list[Ticket], *, as_staff: bool, limit: int
) -> TicketListOut:
    has_more = len(rows) > limit
    items = rows[:limit]
    nos = [t.ticket_no for t in items]
    unread = await repo.unread_of(session, nos, mine_is_buyer=not as_staff)
    names = await _shop_names(session, [t.shop_id for t in items])
    buyers = await _buyer_labels(session, [t.user_id for t in items])
    out = [
        _to_list_item(
            t,
            shop_name=names.get(int(t.shop_id), ""),
            buyer=buyers.get(int(t.user_id), ("", "")),
            unread=unread.get(t.ticket_no, 0),
        )
        for t in items
    ]
    return TicketListOut(
        items=out,
        next_cursor=(
            _encode_cursor(items[-1].last_message_at, int(items[-1].id))
            if has_more and items
            else None
        ),
        has_more=has_more,
    )


async def _detail(
    session: AsyncSession,
    ticket: Ticket,
    *,
    as_staff: bool,
    before_id: int | None = None,
    advance_read: bool = True,
) -> TicketDetailOut:
    now = datetime.now(UTC)
    rows = await repo.list_messages(
        session, ticket.ticket_no, before_id=before_id, limit=MESSAGE_PAGE
    )
    has_more_messages = len(rows) > MESSAGE_PAGE
    if has_more_messages:
        # 多取的那条是**最旧**的（倒序取 limit+1 再翻正序），丢掉它
        rows = rows[1:]
    if advance_read:
        if as_staff:
            await repo.set_staff_read(session, ticket.ticket_no, now=now)
        else:
            await repo.set_user_read(session, ticket.ticket_no, now=now)

    names = await _shop_names(session, [ticket.shop_id])
    buyers = await _buyer_labels(session, [ticket.user_id])
    buyer = buyers.get(int(ticket.user_id), ("", ""))
    close_by = int(ticket.close_by) if ticket.close_by is not None else None
    return TicketDetailOut(
        ticket_no=ticket.ticket_no,
        shop_id=ticket.shop_id,
        shop_name=names.get(int(ticket.shop_id), ""),
        user_id=ticket.user_id,
        buyer_nickname=buyer[0],
        buyer_phone=buyer[1],
        subject=ticket.subject,
        source=int(ticket.source),
        source_text=_text(SOURCE_TEXT, int(ticket.source)),
        status=int(ticket.status),
        status_text=_text(TICKET_STATUS_TEXT, int(ticket.status)),
        context=TicketContextOut(
            order_main_no=ticket.order_main_no,
            order_sub_no=ticket.order_sub_no,
            refund_no=ticket.refund_no,
        ),
        last_message_at=ticket.last_message_at,
        staff_owes_reply=staff_owes_reply(int(ticket.status), int(ticket.last_sender_type)),
        close_by_text=_text(CLOSE_BY_TEXT, close_by, "") if close_by is not None else None,
        close_reason=ticket.close_reason,
        close_time=ticket.close_time,
        created_at=ticket.created_at,
        messages=[_to_message(m) for m in rows],
        has_more_messages=has_more_messages,
        next_message_cursor=str(rows[0].id) if has_more_messages and rows else None,
    )


# ---------------------------------------------------------------
# 买家
# ---------------------------------------------------------------
async def open_ticket(
    session: AsyncSession, *, user_id: int, req: OpenTicketRequest
) -> TicketDetailOut:
    """开一条会话。**同一对象已有进行中的会话就把那条还给你**（不是报错）。

    首条消息不在这里发 —— 走 ``POST /tickets/{no}/messages``，见 schemas 的说明。
    """
    shop_id = int(req.shop_id) if req.shop_id is not None else PLATFORM_SHOP_ID
    if shop_id != PLATFORM_SHOP_ID:
        # 店铺必须存在。挡掉"发往一个不存在的店"的垃圾会话 —— 那种会话
        # 商家永远看不到，只能堆在平台列表里
        await account_service.get_public_shop(session, shop_id)

    now = datetime.now(UTC)
    snowflake = next_id()
    created = await repo.insert_ticket_ignore_conflict(
        session,
        ticket_id=snowflake,
        ticket_no=build_ticket_no(snowflake),
        user_id=user_id,
        shop_id=shop_id,
        subject=req.subject or _default_subject(req.source, req.order_main_no, req.refund_no),
        source=req.source,
        order_main_no=req.order_main_no,
        order_sub_no=req.order_sub_no,
        refund_no=req.refund_no,
        now=now,
    )
    ticket = await repo.get_active(session, user_id=user_id, shop_id=shop_id)
    if ticket is None:  # 理论上到不了：要么刚插进去，要么本来就有一条
        raise BizError(ErrorCode.INTERNAL_ERROR, "会话创建失败，请重试")
    if created:
        await repo.insert_state_flow(
            session,
            ticket_no=ticket.ticket_no,
            from_status=TICKET_OPEN,
            to_status=TICKET_OPEN,
            event=EVENT_OPEN,
            operator_type=OperatorType.USER,
            operator_id=user_id,
        )
    return await _detail(session, ticket, as_staff=False)


async def list_mine(
    session: AsyncSession, *, user_id: int, cursor: str | None, limit: int
) -> TicketListOut:
    capped = _clamp(limit)
    rows = await repo.list_tickets(
        session,
        user_id=user_id,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=capped,
    )
    return await _to_list(session, rows, as_staff=False, limit=capped)


async def get_for_user(
    session: AsyncSession, *, user_id: int, ticket_no: str, before_id: int | None
) -> TicketDetailOut:
    ticket = await repo.get_by_no(session, ticket_no)
    if ticket is None or int(ticket.user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    return await _detail(session, ticket, as_staff=False, before_id=before_id)


async def reply_as_user(
    session: AsyncSession,
    redis: Redis,
    *,
    user_id: int,
    ticket_no: str,
    body: str,
    images: Sequence[str],
) -> TicketMessageOut:
    return await _reply(
        session,
        redis,
        ticket_no=ticket_no,
        body=body,
        images=images,
        sender_type=SENDER_USER,
        sender_id=user_id,
        user_id=user_id,
    )


async def close_by_user(
    session: AsyncSession, *, user_id: int, ticket_no: str, reason: str | None
) -> None:
    await _close(
        session,
        ticket_no=ticket_no,
        reason=reason,
        close_by=SENDER_USER,
        operator_type=OperatorType.USER,
        operator_id=user_id,
        user_id=user_id,
    )


# ---------------------------------------------------------------
# 商家
# ---------------------------------------------------------------
async def list_for_shop(
    session: AsyncSession,
    *,
    shop_id: int,
    pending_only: bool,
    cursor: str | None,
    limit: int,
) -> TicketListOut:
    capped = _clamp(limit)
    rows = await repo.list_tickets(
        session,
        shop_id=shop_id,
        owes_reply_only=pending_only,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=capped,
    )
    return await _to_list(session, rows, as_staff=True, limit=capped)


async def pending_count(session: AsyncSession, *, shop_id: int | None) -> TicketCountOut:
    """「待回复」条数。``shop_id=None`` = 平台视角（全部店铺 + 平台级）。"""
    return TicketCountOut(count=await repo.count_owes_reply(session, shop_id=shop_id))


async def get_for_shop(
    session: AsyncSession, *, shop_id: int, ticket_no: str, before_id: int | None
) -> TicketDetailOut:
    ticket = await repo.get_by_no(session, ticket_no)
    # 平台级会话（shop_id=0）商家看不到 —— 这条等式天然把它们挡在外面
    if ticket is None or int(ticket.shop_id) != shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    return await _detail(session, ticket, as_staff=True, before_id=before_id)


async def reply_as_merchant(
    session: AsyncSession,
    redis: Redis,
    *,
    shop_id: int,
    operator_id: int,
    ticket_no: str,
    body: str,
    images: Sequence[str],
) -> TicketMessageOut:
    return await _reply(
        session,
        redis,
        ticket_no=ticket_no,
        body=body,
        images=images,
        sender_type=SENDER_MERCHANT,
        sender_id=operator_id,
        shop_id=shop_id,
    )


async def close_by_merchant(
    session: AsyncSession,
    *,
    shop_id: int,
    operator_id: int,
    ticket_no: str,
    reason: str | None,
) -> None:
    await _close(
        session,
        ticket_no=ticket_no,
        reason=reason,
        close_by=SENDER_MERCHANT,
        operator_type=OperatorType.MERCHANT,
        operator_id=operator_id,
        shop_id=shop_id,
    )


# ---------------------------------------------------------------
# 平台（可介入任何会话，含平台级）
# ---------------------------------------------------------------
async def list_all(
    session: AsyncSession,
    *,
    shop_id: int | None,
    status: int | None,
    pending_only: bool,
    cursor: str | None,
    limit: int,
) -> TicketListOut:
    capped = _clamp(limit)
    rows = await repo.list_tickets(
        session,
        shop_id=shop_id,
        status=status,
        owes_reply_only=pending_only,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=capped,
    )
    return await _to_list(session, rows, as_staff=True, limit=capped)


async def get_any(
    session: AsyncSession, *, ticket_no: str, before_id: int | None
) -> TicketDetailOut:
    ticket = await repo.get_by_no(session, ticket_no)
    if ticket is None:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    return await _detail(session, ticket, as_staff=True, before_id=before_id)


async def reply_as_platform(
    session: AsyncSession,
    redis: Redis,
    *,
    operator_id: int,
    ticket_no: str,
    body: str,
    images: Sequence[str],
) -> TicketMessageOut:
    return await _reply(
        session,
        redis,
        ticket_no=ticket_no,
        body=body,
        images=images,
        sender_type=SENDER_PLATFORM,
        sender_id=operator_id,
    )


async def close_by_platform(
    session: AsyncSession, *, operator_id: int, ticket_no: str, reason: str | None
) -> None:
    await _close(
        session,
        ticket_no=ticket_no,
        reason=reason,
        close_by=SENDER_PLATFORM,
        operator_type=OperatorType.PLATFORM,
        operator_id=operator_id,
    )


# ---------------------------------------------------------------
# 三个受众共用的写路径
# ---------------------------------------------------------------
async def _reply(
    session: AsyncSession,
    redis: Redis,
    *,
    ticket_no: str,
    body: str,
    images: Sequence[str],
    sender_type: int,
    sender_id: int,
    shop_id: int | None = None,
    user_id: int | None = None,
) -> TicketMessageOut:
    await _check_rate_limit(redis, sender_id)

    # 行锁：并发回复同一会话时，last_message_at / last_sender_type 才不会互相覆盖
    ticket = await repo.get_for_update(session, ticket_no)
    if ticket is None:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    if user_id is not None and int(ticket.user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    if shop_id is not None and int(ticket.shop_id) != shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")

    now = datetime.now(UTC)
    if int(ticket.status) == TICKET_CLOSED:
        # ★ 关闭后再发消息 = **重开同一条**（不是报错、也不新建）。
        #   买家已经点进这条会话并打了字，重开是最诚实的解读；
        #   商家那边它自然重新出现在「待回复」里。
        await repo.reopen(session, ticket_no, now=now)
        await repo.insert_state_flow(
            session,
            ticket_no=ticket_no,
            from_status=TICKET_CLOSED,
            to_status=TICKET_OPEN,
            event=EVENT_REOPEN,
            operator_type=_OPERATOR_OF_SENDER[sender_type],
            operator_id=sender_id,
        )

    message = await repo.insert_message(
        session,
        ticket_no=ticket_no,
        sender_type=sender_type,
        sender_id=sender_id,
        body=body,
        images=images,
    )
    await repo.touch_after_message(session, ticket_no, sender_type=sender_type, now=now)

    if sender_type == SENDER_USER:
        await repo.set_user_read(session, ticket_no, now=now)
    else:
        await repo.set_staff_read(session, ticket_no, now=now)
        # 客服回复 → 同事务写站内信。biz_key 带上消息 id：同一条回复**永远**
        # 只产生一条站内信，连"手滑发两条一样的话"也是两条（那是两条消息）。
        await notify_service.push(
            session,
            user_id=int(ticket.user_id),
            msg_type=SiteMsgType.SUPPORT_REPLY,
            title="客服回复了你",
            body=_preview(body),
            biz_key=f"TICKET:{ticket_no}:{message.id}",
            link_type=MsgLinkType.TICKET,
            link_value=ticket_no,
        )

    return _to_message(message)


async def _close(
    session: AsyncSession,
    *,
    ticket_no: str,
    reason: str | None,
    close_by: int,
    operator_type: OperatorType,
    operator_id: int,
    shop_id: int | None = None,
    user_id: int | None = None,
) -> None:
    now = datetime.now(UTC)
    ticket = await repo.get_for_update(session, ticket_no)
    if ticket is None:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    if user_id is not None and int(ticket.user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    if shop_id is not None and int(ticket.shop_id) != shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")

    closed = await repo.cas_close(
        session,
        ticket_no,
        from_status=TICKET_OPEN,
        close_by=close_by,
        reason=reason,
        now=now,
    )
    if not closed:
        # 已经关过了：幂等，不报错（重复点两次"结束会话"不该看到错误）
        return
    await repo.insert_state_flow(
        session,
        ticket_no=ticket_no,
        from_status=TICKET_OPEN,
        to_status=TICKET_CLOSED,
        event=EVENT_CLOSE,
        operator_type=operator_type,
        operator_id=operator_id,
        remark=reason,
    )
