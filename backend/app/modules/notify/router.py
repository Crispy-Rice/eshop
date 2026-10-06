"""notify 模块的 HTTP 路由 —— 站内信（docs/15 §2.9）。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, DbSession
from app.core.response import ApiResponse
from app.modules.notify import service
from app.modules.notify.schemas import SiteMessageListOut, UnreadCountOut

router = APIRouter()


@router.get(
    "/api/notifications",
    response_model=ApiResponse[SiteMessageListOut],
    summary="我的站内信",
)
async def list_notifications(
    session: DbSession,
    user: CurrentUserDep,
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
) -> ApiResponse[SiteMessageListOut]:
    return ApiResponse.ok(
        await service.list_messages(
            session,
            user_id=user.id,
            unread_only=unread_only,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get(
    "/api/notifications/unread-count",
    response_model=ApiResponse[UnreadCountOut],
    summary="未读数（导航角标）",
)
async def unread_count(session: DbSession, user: CurrentUserDep) -> ApiResponse[UnreadCountOut]:
    return ApiResponse.ok(await service.unread_count(session, user_id=user.id))


@router.post(
    "/api/notifications/{msg_id:int}/read",
    response_model=ApiResponse[None],
    summary="标记一条已读",
)
async def mark_read(
    session: DbSession, user: CurrentUserDep, msg_id: int
) -> ApiResponse[None]:
    """别人的消息返回 404（不泄露它存在与否）。重复标记同一条**不报错**。"""
    await service.mark_read(session, user_id=user.id, msg_id=msg_id)
    return ApiResponse.ok(None)


@router.post(
    "/api/notifications/read-all",
    response_model=ApiResponse[None],
    summary="全部标记已读",
)
async def mark_all_read(session: DbSession, user: CurrentUserDep) -> ApiResponse[None]:
    await service.mark_all_read(session, user_id=user.id)
    return ApiResponse.ok(None)
