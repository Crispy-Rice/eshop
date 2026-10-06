"""notify 模块的领域逻辑 —— **站内信**，也是其他模块能调到的唯一入口。

方向只有两个：

- **写**：``push`` —— 别的模块（``support``）在**自己的事务里**调它。
  站内信与业务**同库**，能随事务一起回滚，所以**不必走 outbox**；
  outbox 解决的是 Redis / 短信这类**回滚不了**的副作用（docs/19 §3）。
  另一半来源 —— trade / payment / aftersale 的 6 个既有 topic —— 走的是 outbox，
  由 ``worker/outbox_delivery.py`` 分派到 ``handlers.py``，最终也落到 ``push``。
- **读**：列表 / 未读数 / 标记已读，只服务 HTTP 路由。

★ 幂等钉在 ``repository.insert_ignore_dup`` 的 ``ON CONFLICT DO NOTHING`` 上：
  上面两条写路径都可能重复投递同一条，``biz_key`` 是唯一的拦阻。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgLinkType, SiteMsgType
from app.core.errors import BizError, ErrorCode
from app.modules.notify import repository as repo
from app.modules.notify.models import SITE_MSG_TYPE_TEXT, SiteMessage
from app.modules.notify.schemas import (
    SiteMessageListOut,
    SiteMessageOut,
    UnreadCountOut,
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _text_of(msg_type: str) -> str:
    return SITE_MSG_TYPE_TEXT.get(msg_type, msg_type)


def _to_out(row: SiteMessage) -> SiteMessageOut:
    return SiteMessageOut(
        id=row.id,
        msg_type=row.msg_type,
        msg_type_text=_text_of(row.msg_type),
        title=row.title,
        body=row.body,
        link_type=row.link_type,
        link_value=row.link_value,
        is_read=row.is_read,
        created_at=row.created_at,
    )


async def push(
    session: AsyncSession,
    *,
    user_id: int,
    msg_type: SiteMsgType,
    title: str,
    biz_key: str,
    body: str = "",
    link_type: MsgLinkType = MsgLinkType.NONE,
    link_value: str | None = None,
) -> bool:
    """写一条站内信。**必须在调用方的事务里调，自己不 commit。**

    返回是否真的写入了（``False`` = ``biz_key`` 已存在，重复投递被静默吸收）。
    调用方通常不关心返回值 —— 幂等命中不是错误。
    """
    return await repo.insert_ignore_dup(
        session,
        user_id=user_id,
        msg_type=str(msg_type),
        title=title,
        body=body,
        biz_key=biz_key,
        link_type=str(link_type),
        link_value=link_value,
    )


def _parse_cursor(cursor: str | None) -> int | None:
    """站内信的游标就是一个 ``id``，所以只是一次数字解析。"""
    if cursor is None:
        return None
    if not cursor.isdigit():
        raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新加载")
    return int(cursor)


async def list_messages(
    session: AsyncSession,
    *,
    user_id: int,
    unread_only: bool = False,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SiteMessageListOut:
    limit = max(1, min(limit, MAX_LIMIT))
    rows = await repo.list_for_user(
        session, user_id, unread_only=unread_only, cursor=_parse_cursor(cursor), limit=limit
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return SiteMessageListOut(
        items=[_to_out(r) for r in items],
        next_cursor=str(items[-1].id) if has_more and items else None,
        has_more=has_more,
    )


async def unread_count(session: AsyncSession, *, user_id: int) -> UnreadCountOut:
    return UnreadCountOut(count=await repo.count_unread(session, user_id))


async def mark_read(session: AsyncSession, *, user_id: int, msg_id: int) -> None:
    """标记一条已读。**不是自己的消息一律"不存在"**（不泄露它存在与否）。"""
    if not await repo.mark_read(session, user_id, msg_id):
        raise BizError(ErrorCode.NOT_FOUND, "消息不存在")


async def mark_all_read(session: AsyncSession, *, user_id: int) -> int:
    return await repo.mark_all_read(session, user_id)
