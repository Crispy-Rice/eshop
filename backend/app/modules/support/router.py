"""support 模块的 HTTP 路由 —— 客服会话（docs/15 §2.9）。

三个受众三组守卫：

| 前缀 | 守卫 |
|---|---|
| ``/api/support/*`` | ``CurrentUserDep``（买家，按 ``user_id`` 判归属） |
| ``/api/merchant/support/*`` | ``CurrentShopIdDep``（按**店铺归属**，刻意不看 role） |
| ``/api/admin/support/*`` | ``require_role("admin")``（**只做平台级会话**：提给平台的工单） |

★ **平台不是"超级商家"。** 它原来能列出所有店铺的买家会话并以「平台客服」身份插话，
  结果是买家正在跟某家店聊，中间冒出一句平台客服的话 —— 而且平台客服台里
  混着几十家店的买家咨询，真正该看的"提给平台的工单"反而被淹了。
  店的会话归商家；商家自己处理不了，会转人工到平台。这条边界在
  ``service.list_platform`` 里用常量钉死（不提供 ``shop_id`` 参数）。

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
from app.modules.support.models import PLATFORM_SHOP_ID
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
    "/api/support/tickets/{ticket_no}/request-human",
    response_model=ApiResponse[None],
    summary="买家要求人工客服",
)
async def request_human(
    session: DbSession,
    redis: RedisDep,
    user: CurrentUserDep,
    ticket_no: str,
) -> ApiResponse[None]:
    """在这条会话上置「已转人工」+ 留一条说明消息。**不新开会话。**

    ★ 与 ``/api/assistant/handoff``（商家 → **平台**，会另开一张平台级工单）
      是两件事，别混：这条是"买家在自己店里要人工"。

    幂等：已经转过的会话重复点不会有第二条说明。
    """
    await service.request_human(session, redis, user_id=user.id, ticket_no=ticket_no)
    return ApiResponse.ok(None)


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
    status: int | None = Query(default=None, ge=10, le=30, description="按状态过滤"),
    pending_only: bool = Query(default=False, alias="pendingOnly", description="只看待回复"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
) -> ApiResponse[TicketListOut]:
    """★ ``status`` 与平台那条同构（10 进行中 / 30 已结束）—— 后台的页签靠它，
    少了它「已结束」就会查出全部（见 ``service.list_for_shop`` 的说明）。"""
    return ApiResponse.ok(
        await service.list_for_shop(
            session,
            shop_id=shop_id,
            status=status,
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
# 平台（只处理平台级会话，见 service.list_platform）
# ============================================================
@router.get(
    "/api/admin/support/tickets",
    response_model=ApiResponse[TicketListOut],
    summary="平台级会话队列（提给平台的工单）",
)
async def list_platform_tickets(
    session: DbSession,
    user: AdminDep,
    status: int | None = Query(default=None, ge=10, le=30),
    pending_only: bool = Query(default=False, alias="pendingOnly", description="只看待回复"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
) -> ApiResponse[TicketListOut]:
    return ApiResponse.ok(
        await service.list_platform(
            session,
            status=status,
            pending_only=pending_only,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get(
    "/api/admin/support/pending-count",
    response_model=ApiResponse[TicketCountOut],
    summary="待回复数（平台导航角标，只数平台级会话）",
)
async def platform_pending_count(
    session: DbSession, user: AdminDep
) -> ApiResponse[TicketCountOut]:
    """★ 口径与上面那张列表严格一致（都是平台级），否则角标会提示一堆点不进去的会话。"""
    return ApiResponse.ok(
        await service.pending_count(session, shop_id=PLATFORM_SHOP_ID)
    )


@router.get(
    "/api/admin/support/tickets/{ticket_no}",
    response_model=ApiResponse[TicketDetailOut],
    summary="会话详情（平台）",
)
async def get_platform_ticket(
    session: DbSession,
    user: AdminDep,
    ticket_no: str,
    before: int | None = Query(default=None),
) -> ApiResponse[TicketDetailOut]:
    """★ 店铺的买家会话 → **404**（不是 403）：平台对它没有身份，装作"存在但无权"等于承认存在。"""
    return ApiResponse.ok(
        await service.get_for_platform(session, ticket_no=ticket_no, before_id=before)
    )


@router.post(
    "/api/admin/support/tickets/{ticket_no}/messages",
    response_model=ApiResponse[TicketMessageOut],
    summary="平台回复提问方",
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
    summary="平台结束会话",
)
async def close_platform_ticket(
    session: DbSession,
    user: AdminDep,
    ticket_no: str,
    body: CloseTicketRequest,
) -> ApiResponse[None]:
    await service.close_by_platform(
        session, operator_id=user.id, ticket_no=ticket_no, reason=body.reason
    )
    return ApiResponse.ok(None)
