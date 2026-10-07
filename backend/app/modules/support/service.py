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
    SENDER_AI,
    SENDER_MERCHANT,
    SENDER_PLATFORM,
    SENDER_SYSTEM,
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
    AwaitingTicketOut,
    OpenTicketRequest,
    TicketContextOut,
    TicketCountOut,
    TicketDetailOut,
    TicketListItemOut,
    TicketListOut,
    TicketMessageOut,
    TicketStateOut,
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
#
# ★ ``SENDER_SYSTEM`` 与 ``SENDER_AI`` 都记成 ``SYSTEM``（自动化，不是人）：
#   状态流水要回答的是"谁重开的"，而这两个都没有 operator_id。
#   哪天需要区分"系统重开"与"AI 重开"，再给 OperatorType 加一个值。
_OPERATOR_OF_SENDER = {
    SENDER_USER: OperatorType.USER,
    SENDER_MERCHANT: OperatorType.MERCHANT,
    SENDER_PLATFORM: OperatorType.PLATFORM,
    SENDER_SYSTEM: OperatorType.SYSTEM,
    SENDER_AI: OperatorType.SYSTEM,
}

# 「人」的发送方：只有它们吃按发送者的发消息限流。
# AI 与系统不占这个键（见 _reply 的说明）。
_HUMAN_SENDERS = frozenset({SENDER_USER, SENDER_MERCHANT, SENDER_PLATFORM})


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
        staff_owes_reply=staff_owes_reply(
            int(ticket.status), last_sender, ticket.need_human_at is not None
        ),
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
            spu_id=ticket.spu_id,
        ),
        last_message_at=ticket.last_message_at,
        staff_owes_reply=staff_owes_reply(
            int(ticket.status), int(ticket.last_sender_type), ticket.need_human_at is not None
        ),
        need_human=ticket.need_human_at is not None,
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
        spu_id=int(req.spu_id) if req.spu_id is not None else None,
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
    else:
        # ★ 复用已有会话时，**刷新上下文**（后一次点击为准）。
        #   不刷新的话会答错：买家上一轮从商品 A 点进来、这一轮从商品 B 点进来，
        #   会话里记的还是 A —— 店小蜜会拿 A 的价格去答 B 的问题。
        #   只更新请求里**真的带了**的那几项（None 不动），所以从订单页点进来
        #   不会把商品上下文抹掉。
        await repo.refresh_context(
            session,
            ticket_no=ticket.ticket_no,
            spu_id=int(req.spu_id) if req.spu_id is not None else None,
            order_main_no=req.order_main_no,
            order_sub_no=req.order_sub_no,
            refund_no=req.refund_no,
            now=now,
        )
        ticket = await repo.get_active(session, user_id=user_id, shop_id=shop_id)
        assert ticket is not None
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


async def request_human(
    session: AsyncSession, redis: Redis, *, user_id: int, ticket_no: str
) -> None:
    """买家主动要人工客服。

    ★ 它只做两件事：置 ``need_human_at``（让这条会话**留在/回到**商家的「待回复」
      队列里 —— 否则 AI 说过话之后，买家再喊一句"我要人工"是掉进队列外面的），
      外加一条给买家看的系统消息。**不新开会话、不改归属**。

    ★ 与 ``assistant.service.handoff_to_human``（商家 → **平台**，会另开一张
      **平台级**工单）是两件事：那条是"商家以平台使用者的身份提问"，
      这条是"买家在自己店里要人工"。两者的票据、队列、参与方都不同。

    幂等：已经转过的会话不再重复插消息（买家连点两下只有一条说明）。
    """
    now = datetime.now(UTC)
    ticket = await repo.get_for_update(session, ticket_no)
    if ticket is None or int(ticket.user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    if ticket.need_human_at is not None:
        return

    await repo.set_need_human(session, ticket_no, now=now)
    # 走 _reply 而不是自己插消息：它顺带处理"会话已关则重开"、行锁与
    # last_message_at/last_sender_type 的维护（系统消息不动任何读游标、不推站内信）
    await _reply(
        session,
        redis,
        ticket_no=ticket_no,
        body="已收到，正在为你转接人工客服，商家会尽快回复。",
        images=(),
        sender_type=SENDER_SYSTEM,
        sender_id=None,
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
    status: int | None,
    pending_only: bool,
    cursor: str | None,
    limit: int,
) -> TicketListOut:
    """本店队列。

    ★ ``status`` 不是可选项：后台的「进行中 / 已结束 / 全部」三个页签全靠它。
      原来这里只认 ``pending_only``（那只是「待回复」**一个**页签），于是
      「已结束」查出来的还是全部 —— 点下去看着就像页签坏了。
      它与 ``pending_only`` 是 AND 关系：后者本身还带一个「进行中」
      （见 ``models.OWES_REPLY_WHERE``），两个一起传只会更窄，不会打架。
    """
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


# ---------------------------------------------------------------
# 店铺的智能客服（AI 以店铺身份说话）
# ---------------------------------------------------------------
async def reply_as_ai(
    session: AsyncSession,
    redis: Redis,
    *,
    shop_id: int,
    ticket_no: str,
    body: str,
) -> TicketMessageOut:
    """店铺的智能客服在会话里说一句。

    ★ 归属还是商家那一套（``shop_id`` 必须等于会话的店铺）—— AI 不是特权身份，
      它就是这家店的一个发言人。调用方（assistant 模块）负责在那之前复核
      "真人有没有抢答 / 开关是否还开着"。
    ★ **不推站内信、不动商家读游标**（``_reply`` 里按发送方白名单分派），
      于是 AI 说完话之后，商家的未读与「待回复」都还是准的。
    """
    return await _reply(
        session,
        redis,
        ticket_no=ticket_no,
        body=body,
        images=(),
        sender_type=SENDER_AI,
        sender_id=None,
        shop_id=shop_id,
    )


async def flag_need_human(session: AsyncSession, *, shop_id: int, ticket_no: str) -> None:
    """把会话标成「需要人工」——**智能客服答不了时**调用。

    ★ 与 ``request_human`` 的分工：那个是**买家**要人工（会留一条说明消息给买家看），
      这个是**店铺的机器人**自己认输（它刚说过"我答不了"，不必再插一条）。
      两者共用 ``need_human_at`` 这一个闩锁 —— 谁先说都算数，商家回复时一起解开。

    ★ 归属照商家的规矩校验：AI 只能动自己店的会话。
    """
    ticket = await repo.get_for_update(session, ticket_no)
    if ticket is None or int(ticket.shop_id) != shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    # 已转过 / 已关闭：无事可做（关闭的会话连闩锁都不该再置）
    if ticket.need_human_at is not None or int(ticket.status) == TICKET_CLOSED:
        return
    await repo.set_need_human(session, ticket_no, now=datetime.now(UTC))


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
# 平台（**只**处理平台级会话 —— 商家 / 买家提给平台的工单）
# ---------------------------------------------------------------
async def list_platform(
    session: AsyncSession,
    *,
    status: int | None,
    pending_only: bool,
    cursor: str | None,
    limit: int,
) -> TicketListOut:
    """平台队列。**恒定过滤 ``shop_id = 平台哨兵``。**

    ★ 这里**没有 ``shop_id`` 参数是故意的**。它原来接受 ``None``（= 不限店铺），
      于是平台客服台会列出所有店铺的买家会话，平台还能以「平台客服」的身份插进去
      —— 买家正在跟某家店聊，中间冒出一句平台客服的话，**谁在跟谁说话都分不清**。

      平台在这套系统里的职责是"回答提给平台的工单"，店的会话归商家（商家要是处理不了，
      会自己转人工给平台）。写成常量而不是参数，将来没人能靠传参把范围放开。
    """
    capped = _clamp(limit)
    rows = await repo.list_tickets(
        session,
        shop_id=PLATFORM_SHOP_ID,
        status=status,
        owes_reply_only=pending_only,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=capped,
    )
    return await _to_list(session, rows, as_staff=True, limit=capped)


async def get_for_platform(
    session: AsyncSession, *, ticket_no: str, before_id: int | None
) -> TicketDetailOut:
    ticket = await repo.get_by_no(session, ticket_no)
    # 归属不对一律 NOT_FOUND（与商家侧同一条规矩：不给遍历探测留口子）
    if ticket is None or int(ticket.shop_id) != PLATFORM_SHOP_ID:
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
    """★ 复用 ``_reply`` 的**店铺归属校验**：传 ``shop_id=平台哨兵``，
    店里的会话会被那条等式挡成 404 —— 平台只能落在平台级会话里。"""
    return await _reply(
        session,
        redis,
        ticket_no=ticket_no,
        body=body,
        images=images,
        sender_type=SENDER_PLATFORM,
        sender_id=operator_id,
        shop_id=PLATFORM_SHOP_ID,
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
        shop_id=PLATFORM_SHOP_ID,
    )


# ---------------------------------------------------------------
# 给别的模块的只读投影（跨模块的唯一入口）
# ---------------------------------------------------------------
async def list_awaiting_tickets(
    session: AsyncSession, *, since: datetime, limit: int = 50
) -> list[AwaitingTicketOut]:
    """买家刚说完话、**还没有人接**的会话（店铺会话，按最近发言排序）。

    ★ **泛型**：语义只有"买家发言了、还没有人回"，**不含任何 AI 概念** ——
      support 不知道谁会用这个列表（见 docs/19 §2.3 与 docs/20 §14）。
    ★ 比「商家欠回复」窄一项：**已转人工的不算**，那种已经交给人了。
    ★ ``since`` 是新鲜度窗口，调用方给（越旧的会话越不该被翻出来自动回）。
    """
    rows = await repo.list_awaiting(session, since=since, limit=_clamp(limit))
    return [
        AwaitingTicketOut(
            ticket_no=ticket.ticket_no,
            shop_id=ticket.shop_id,
            user_id=ticket.user_id,
            last_message_id=last_id,
            last_message_at=ticket.last_message_at,
            source=int(ticket.source),
            subject=ticket.subject,
            order_main_no=ticket.order_main_no,
            order_sub_no=ticket.order_sub_no,
            refund_no=ticket.refund_no,
        )
        for ticket, last_id in rows
    ]


async def get_ticket_state(
    session: AsyncSession, *, shop_id: int, ticket_no: str, lock: bool = False
) -> TicketStateOut | None:
    """一条会话的当前状态（给别的模块做**写前复核**）。

    ★ ``lock=True`` 会拿行锁。调用方要的是"**重读 → 判断 → 写**"这一串不被打断：
      不锁的话，商家可能在你读完、还没写完之间回了一条，你的写就盖掉了他的话。
      （写路径 ``_reply`` 自己也会拿行锁，但"判断"必须在锁内做才有意义。）
    ★ 归属不对 / 不存在 → ``None``：与全模块一致，不区分"没有"和"不是你的"。
    """
    ticket = (
        await repo.get_for_update(session, ticket_no)
        if lock
        else await repo.get_by_no(session, ticket_no)
    )
    if ticket is None or int(ticket.shop_id) != shop_id:
        return None
    return TicketStateOut(
        ticket_no=ticket.ticket_no,
        shop_id=ticket.shop_id,
        status=int(ticket.status),
        last_sender_type=int(ticket.last_sender_type),
        need_human=ticket.need_human_at is not None,
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
    sender_id: int | None,
    shop_id: int | None = None,
    user_id: int | None = None,
) -> TicketMessageOut:
    """所有发送方共用的写路径。

    ★ **限流只对"人"生效**（买家 / 商家 / 平台）。AI 与系统没有 ``sender_id``：
      拿店铺 id 去套那个键会和商家自己的回复抢同一个计数器（20 条/分钟），
      而 AI 的花费由 assistant 自己的额度闸（按店铺日 token）管。
    """
    if sender_type in _HUMAN_SENDERS:
        assert sender_id is not None  # 人类发送方一定带 id
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
    elif sender_type in (SENDER_MERCHANT, SENDER_PLATFORM):
        # ★ 白名单，不是 `else`。原来这里是 else，于是**任何**新发送方都会掉进来
        #   干两件错事（SENDER_AI 正是这样被坑到的）：
        #   ① `set_staff_read(now)` 把商家的读游标推到现在 —— 买家刚问的问题会被
        #      机器人自己标成"商家已读"，`unread_of` 算出的商家未读直接归零；
        #   ② 推一条「客服回复了你」的站内信 —— 说话的根本不是人（误导），
        #      而且会计入买家的未读角标。
        #   AI 的话买家在会话页本来就看得到，不需要额外推一条。
        #   将来再加发送方，默认是"什么都不做"，而不是"当成客服"。
        await repo.set_staff_read(session, ticket_no, now=now)
        # 人已经回过了，这一笔不再欠谁（AI 转人工留下的闩锁在这里解开）
        await repo.clear_need_human(session, ticket_no, now=now)
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
