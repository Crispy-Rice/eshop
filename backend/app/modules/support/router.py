"""support 模块的 HTTP 路由 —— 客服会话（docs/15 §2.9）。

三个受众三组守卫：

| 前缀 | 守卫 |
|---|---|
| ``/api/support/*`` | ``CurrentUserDep``（买家，按 ``user_id`` 判归属） |
| ``/api/merchant/support/*`` | ``CurrentShopIdDep``（按**店铺归属**，刻意不看 role） |
| ``/api/admin/support/*`` | ``require_role("admin")``（平台可介入任何会话） |

★ **发消息不要求 ``Idempotency-Key``** —— 与 cart 的加购同类：聊天消息的语义就是
  "又发了一条"，没有资金动作需要护住，真重复了也只是多一条同样的话。该做的是
  前端在请求在飞时禁用发送按钮。反过来"开会话"**天然幂等**（已有进行中的会话
  就把那条还给你），也不需要键。

★ **没有 ``/read`` 端点**：读游标由"打开详情"和"发消息"推进（见 service 的说明），
  少一个端点就少一处前端要记得调的地方。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.context import CurrentUser
from app.core.deps import CurrentUserDep, DbSession, RedisDep, require_role
from app.core.response import ApiResponse
from app.modules.account.deps import CurrentShopIdDep
from app.modules.support import service
from app.modules.support.schemas import (
    CloseTicketRequest,
    OpenTicketRequest,
    SendMessageRequest,
    TicketCountOut,
    TicketDetailOut,
    TicketListOut,
    TicketMessageOut,
)

router = APIRouter()

AdminDep = Annotated[CurrentUser, Depends(require_role("admin"))]


# ============================================================
# 买家
# ============================================================
@router.post(
    "/api/support/tickets",
    response_model=ApiResponse[TicketDetailOut],
    summary="开会话（已有进行中的会话就返回那一条）",
)
async def open_ticket(
    session: DbSession, body: OpenTicketRequest, user: CurrentUserDep
) -> ApiResponse[TicketDetailOut]:
    """``shopId`` 不传 = 平台级会话。**首条消息另发**（POST …/messages）。"""
    return ApiResponse.ok(await service.open_ticket(session, user_id=user.id, req=body))


@router.get(
    "/api/support/tickets", response_model=ApiResponse[TicketListOut], summary="我的会话"
)
async def list_my_tickets(
    session: DbSession,
    user: CurrentUserDep,
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
) -> ApiResponse[TicketListOut]:
    return ApiResponse.ok(
        await service.list_mine(session, user_id=user.id, cursor=cursor, limit=limit)
    )


@router.get(
    "/api/support/tickets/{ticket_no}",
    response_model=ApiResponse[TicketDetailOut],
    summary="会话详情（带最近一页消息）",
)
async def get_ticket(
    session: DbSession,
    user: CurrentUserDep,
    ticket_no: str,
    before: int | None = Query(default=None, description="往前翻：带上当前最早那条消息的 id"),
) -> ApiResponse[TicketDetailOut]:
    return ApiResponse.ok(
        await service.get_for_user(
            session, user_id=user.id, ticket_no=ticket_no, before_id=before
        )
    )


@router.post(
    "/api/support/tickets/{ticket_no}/messages",
    response_model=ApiResponse[TicketMessageOut],
    summary="买家发言",
)
async def send_message(
    session: DbSession,
    redis: RedisDep,
    user: CurrentUserDep,
    ticket_no: str,
    body: SendMessageRequest,
) -> ApiResponse[TicketMessageOut]:
    """会话已关闭时**重开同一条**（不是报错，也不新建）。"""
    return ApiResponse.ok(
        await service.reply_as_user(
            session,
            redis,
            user_id=user.id,
            ticket_no=ticket_no,
            body=body.body,
            images=body.images,
        )
    )


@router.post(
    "/api/support/tickets/{ticket_no}/close",
    response_model=ApiResponse[None],
    summary="买家结束会话",
)
async def close_ticket(
    session: DbSession,
    user: CurrentUserDep,
    ticket_no: str,
    body: CloseTicketRequest,
) -> ApiResponse[None]:
    """重复关闭不报错（幂等）。"""
    await service.close_by_user(
        session, user_id=user.id, ticket_no=ticket_no, reason=body.reason
    )
    return ApiResponse.ok(None)


# ============================================================
# 商家
# ============================================================
@router.get(
    "/api/merchant/support/tickets",
    response_model=ApiResponse[TicketListOut],
    summary="本店会话队列",
)
async def list_shop_tickets(
    session: DbSession,
    shop_id: CurrentShopIdDep,
    pending_only: bool = Query(default=False, alias="pendingOnly", description="只看待回复"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
) -> ApiResponse[TicketListOut]:
    return ApiResponse.ok(
        await service.list_for_shop(
            session,
            shop_id=shop_id,
            pending_only=pending_only,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get(
    "/api/merchant/support/pending-count",
    response_model=ApiResponse[TicketCountOut],
    summary="待回复数（商家导航角标）",
)
async def shop_pending_count(
    session: DbSession, shop_id: CurrentShopIdDep
) -> ApiResponse[TicketCountOut]:
    """★ 单开一个 count 而不是让前端数列表第一页：列表是游标分页的，
    数一页会**少报** —— 而"积压很多"恰恰是角标最该说话的时候。"""
    return ApiResponse.ok(await service.pending_count(session, shop_id=shop_id))


@router.get(
    "/api/merchant/support/tickets/{ticket_no}",
    response_model=ApiResponse[TicketDetailOut],
    summary="会话详情（商家视角）",
)
async def get_shop_ticket(
    session: DbSession,
    shop_id: CurrentShopIdDep,
    ticket_no: str,
    before: int | None = Query(default=None),
) -> ApiResponse[TicketDetailOut]:
    return ApiResponse.ok(
        await service.get_for_shop(
            session, shop_id=shop_id, ticket_no=ticket_no, before_id=before
        )
    )


@router.post(
    "/api/merchant/support/tickets/{ticket_no}/messages",
    response_model=ApiResponse[TicketMessageOut],
    summary="商家回复",
)
async def reply_as_merchant(
    session: DbSession,
    redis: RedisDep,
    shop_id: CurrentShopIdDep,
    user: CurrentUserDep,
    ticket_no: str,
    body: SendMessageRequest,
) -> ApiResponse[TicketMessageOut]:
    """回复会**同事务**给买家写一条站内信 —— 所以消息中心的角标立刻是准的。"""
    return ApiResponse.ok(
        await service.reply_as_merchant(
            session,
            redis,
            shop_id=shop_id,
            operator_id=user.id,
            ticket_no=ticket_no,
            body=body.body,
            images=body.images,
        )
    )


@router.post(
    "/api/merchant/support/tickets/{ticket_no}/close",
    response_model=ApiResponse[None],
    summary="商家结束会话",
)
async def close_shop_ticket(
    session: DbSession,
    shop_id: CurrentShopIdDep,
    user: CurrentUserDep,
    ticket_no: str,
    body: CloseTicketRequest,
) -> ApiResponse[None]:
    await service.close_by_merchant(
        session,
        shop_id=shop_id,
        operator_id=user.id,
        ticket_no=ticket_no,
        reason=body.reason,
    )
    return ApiResponse.ok(None)


# ============================================================
# 平台（可介入任何会话，含平台级）
# ============================================================
@router.get(
    "/api/admin/support/tickets",
    response_model=ApiResponse[TicketListOut],
    summary="全部会话（平台）",
)
async def list_all_tickets(
    session: DbSession,
    user: AdminDep,
    shop_id: int | None = Query(default=None, alias="shopId", description="按店铺筛；0 = 平台级"),
    status: int | None = Query(default=None, ge=10, le=30),
    pending_only: bool = Query(default=False, alias="pendingOnly", description="只看待回复"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
) -> ApiResponse[TicketListOut]:
    return ApiResponse.ok(
        await service.list_all(
            session,
            shop_id=shop_id,
            status=status,
            pending_only=pending_only,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get(
    "/api/admin/support/pending-count",
    response_model=ApiResponse[TicketCountOut],
    summary="待回复数（平台导航角标，含全部店铺与平台级）",
)
async def admin_pending_count(
    session: DbSession, user: AdminDep
) -> ApiResponse[TicketCountOut]:
    return ApiResponse.ok(await service.pending_count(session, shop_id=None))


@router.get(
    "/api/admin/support/tickets/{ticket_no}",
    response_model=ApiResponse[TicketDetailOut],
    summary="会话详情（平台）",
)
async def get_any_ticket(
    session: DbSession,
    user: AdminDep,
    ticket_no: str,
    before: int | None = Query(default=None),
) -> ApiResponse[TicketDetailOut]:
    return ApiResponse.ok(
        await service.get_any(session, ticket_no=ticket_no, before_id=before)
    )


@router.post(
    "/api/admin/support/tickets/{ticket_no}/messages",
    response_model=ApiResponse[TicketMessageOut],
    summary="平台介入发言",
)
async def reply_as_platform(
    session: DbSession,
    redis: RedisDep,
    user: AdminDep,
    ticket_no: str,
    body: SendMessageRequest,
) -> ApiResponse[TicketMessageOut]:
    return ApiResponse.ok(
        await service.reply_as_platform(
            session,
            redis,
            operator_id=user.id,
            ticket_no=ticket_no,
            body=body.body,
            images=body.images,
        )
    )


@router.post(
    "/api/admin/support/tickets/{ticket_no}/close",
    response_model=ApiResponse[None],
    summary="平台关闭会话",
)
async def close_any_ticket(
    session: DbSession,
    user: AdminDep,
    ticket_no: str,
    body: CloseTicketRequest,
) -> ApiResponse[None]:
    await service.close_by_platform(
        session, operator_id=user.id, ticket_no=ticket_no, reason=body.reason
    )
    return ApiResponse.ok(None)
