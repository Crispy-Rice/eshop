"""assistant 的 HTTP 路由，见 docs/20 §8。

★ **只挂一个路径族 ``/api/assistant/*``，不拆成 ``/merchant`` 与 ``/admin``。**
  这个功能是共用的，角色决定的是**能看到哪些工具**、而不是能不能用助手
  （见 ``tools.tools_for``）。拆两套只会得到八个字面相同的 handler，
  将来改一处忘一处。角色一律取自 token，**绝不接受请求体里的身份**。

事务由 ``get_session`` 依赖统一管理，路由里不写 ``begin()``。
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Query

from app.core.deps import CurrentUserDep, DbSession, RedisDep
from app.core.errors import BizError, ErrorCode
from app.core.response import ApiResponse
from app.modules.account import service as account_service
from app.modules.assistant import rules, service
from app.modules.assistant.models import ACTOR_ADMIN, ACTOR_MERCHANT, Message
from app.modules.assistant.schemas import (
    CONVERSATION_PAGE_SIZE,
    MESSAGE_PAGE_SIZE,
    AskOut,
    AskRequest,
    ConversationListItemOut,
    ConversationListOut,
    ConversationOut,
    HandoffOut,
    HandoffRequest,
    MessageOut,
    ShopBotStatsOut,
    ShopFaqIn,
    ShopFaqOut,
    ShopSettingIn,
    ShopSettingOut,
)

# ★ **不按 role 拦，也不按路径分商家/运营两套。** 这个项目的"商家"不是 token 里的
#   角色 —— 开店不会改 token 里的 role（`account/deps.py` 自己写着"用户开店后拿的
#   还是旧 token"），商家身份由**数据库里有没有店铺**决定。所以这里放行所有登录用户，
#   由 `_caller` 去查店铺：有店铺 = 商家作用域，admin = 运营作用域，两者都不是
#   （纯买家）= 用不了助手。
router = APIRouter()


async def _caller(user: CurrentUserDep, session: DbSession) -> rules.Caller | None:
    """身份 → ``Caller``。**这是整个模块唯一的身份来源。**

    ★ 店铺 ID 走 ``account.service.get_shop_id``（**查数据库**，不读 JWT 里的
      ``shop_id``）—— 与 ``CurrentShopIdDep`` 同一套判断。

    运营没有店铺，返回 ``shop_id=None``；还没开店的买家返回 ``None``
    （调用方决定是拒掉还是只把 ``available`` 标成 false）。
    """
    # ★ ``is_admin`` 是**方法**（``CurrentUser.is_admin()``），不是属性 —— 写成
    #   ``if user.is_admin:`` 会恒为真（bound method 对象永远 truthy），后果是
    #   **每个商家都拿到平台运营的工具集**。``rules.Caller.is_admin`` 是属性，
    #   两边不一致，改这里时看清楚。
    if user.is_admin():
        return rules.Caller(user_id=user.id, role=ACTOR_ADMIN, shop_id=None)
    shop_id = await account_service.get_shop_id(session, user.id)
    if shop_id is None:
        return None
    # ★ 作用域用 ACTOR_MERCHANT 而不是 ``user.role``：token 里那个值对店主来说
    #   还是 ``buyer``，拿它去查工具清单会得到**空清单**（一个工具都用不了）
    return rules.Caller(user_id=user.id, role=ACTOR_MERCHANT, shop_id=int(shop_id))


async def _require_caller(user: CurrentUserDep, session: DbSession) -> rules.Caller:
    caller = await _caller(user, session)
    if caller is None:
        raise BizError(ErrorCode.FORBIDDEN, "请先开通店铺")
    return caller


def _page(
    messages: Sequence[Message], has_more: bool
) -> tuple[list[MessageOut], bool, str | None]:
    """消息 → 出参 + 上一页游标（**当前最早那条**的 id）。

    ★ 游标是"最早那条"而不是"最新那条"：下一页要的是**更早**的，
      传最新那条的 id 会原地打转。
    """
    out = [MessageOut.of(row) for row in messages]
    cursor = str(messages[0].id) if (has_more and messages) else None
    return out, has_more, cursor


@router.get(
    "/api/assistant/conversation",
    response_model=ApiResponse[ConversationOut],
    summary="最近一条会话（面板首次打开 / 轮询用）",
)
async def get_latest_conversation(
    session: DbSession, user: CurrentUserDep
) -> ApiResponse[ConversationOut]:
    """**最近动过的那条**会话 + 它最新的一页消息。

    面板首次打开用它 —— 前端不必先知道会话号，后端替它挑"上次聊的那条"。

    ★ ``available=false`` 表示这个账号还用不了助手（商家还没开店）。
      **不抛 403**：前端要据此把入口藏起来，而不是弹一个错误。
    """
    caller = await _caller(user, session)
    if caller is None:
        return ApiResponse.ok(ConversationOut(available=False))
    conv, messages, has_more = await service.load_conversation(
        session, caller=caller, limit=MESSAGE_PAGE_SIZE
    )
    out, more, cursor = _page(messages, has_more)
    return ApiResponse.ok(
        ConversationOut(
            conversation_no=conv.conversation_no if conv else None,
            title=conv.title if conv else None,
            messages=out,
            has_more=more,
            next_cursor=cursor,
        )
    )


@router.get(
    "/api/assistant/conversations",
    response_model=ApiResponse[ConversationListOut],
    summary="会话列表（「历史对话」）",
)
async def list_conversations(
    session: DbSession,
    user: CurrentUserDep,
    cursor: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=CONVERSATION_PAGE_SIZE, ge=1, le=50),
) -> ApiResponse[ConversationListOut]:
    """自己的会话，**最近动过的在前**。"""
    caller = await _require_caller(user, session)
    rows, has_more = await service.list_conversations(
        session,
        caller=caller,
        before_id=int(cursor) if cursor else None,
        limit=limit,
    )
    return ApiResponse.ok(
        ConversationListOut(
            items=[
                ConversationListItemOut(
                    conversation_no=row.conversation_no,
                    title=row.title,
                    updated_at=row.updated_at,
                )
                for row in rows
            ],
            has_more=has_more,
            next_cursor=str(rows[-1].id) if (has_more and rows) else None,
        )
    )


@router.get(
    "/api/assistant/conversations/{conversation_no}",
    response_model=ApiResponse[ConversationOut],
    summary="某一条会话（切过去 / 往上翻更早的）",
)
async def get_conversation_detail(
    session: DbSession,
    user: CurrentUserDep,
    conversation_no: str,
    before: str | None = Query(default=None, max_length=32, description="取更早一页的游标"),
) -> ApiResponse[ConversationOut]:
    """某一条会话 + 它的一页消息。

    ``before`` 给定时取**更早的一页**（面板顶部的「加载更早」）。
    ★ 不是自己的会话 → **404**（不是 403），不给遍历探测留口子。
    """
    caller = await _require_caller(user, session)
    conv, messages, has_more = await service.load_conversation(
        session,
        caller=caller,
        conversation_no=conversation_no,
        before_id=int(before) if before else None,
        limit=MESSAGE_PAGE_SIZE,
    )
    if conv is None:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    out, more, cursor = _page(messages, has_more)
    return ApiResponse.ok(
        ConversationOut(
            conversation_no=conv.conversation_no,
            title=conv.title,
            messages=out,
            has_more=more,
            next_cursor=cursor,
        )
    )


@router.post(
    "/api/assistant/messages",
    response_model=ApiResponse[AskOut],
    summary="提问（异步：返回 messageId，前端轮询会话详情）",
)
async def ask(
    session: DbSession, user: CurrentUserDep, req: AskRequest
) -> ApiResponse[AskOut]:
    """提一个问题。``conversationNo`` 不传 = **开一条新对话**。"""
    caller = await _require_caller(user, session)
    message = await service.submit_question(
        session,
        caller=caller,
        question=req.question,
        conversation_no=req.conversation_no,
    )
    return ApiResponse.ok(
        AskOut(conversation_no=message.conversation_no, message_id=message.id)
    )


@router.post(
    "/api/assistant/handoff",
    response_model=ApiResponse[HandoffOut],
    summary="转人工（把对话摘要交给平台客服）",
)
async def handoff(
    session: DbSession,
    redis: RedisDep,
    user: CurrentUserDep,
    req: HandoffRequest | None = None,
) -> ApiResponse[HandoffOut]:
    """``conversationNo`` 给定时转的是**那一条**（不是"最近动过的"），
    ``note`` 是可选的一句补充说明 —— 两者都没有才拒绝（见 service 的说明）。"""
    caller = await _require_caller(user, session)
    ticket_no, reused = await service.handoff_to_human(
        session,
        redis,
        caller=caller,
        conversation_no=req.conversation_no if req else None,
        note=req.note if req else None,
    )
    return ApiResponse.ok(HandoffOut(ticket_no=ticket_no, reused=reused))


# ============================================================
# 店小蜜（买家侧）：商户自己维护的开关与问答（docs/20 §14）
#
# ★ 全是**本店**范围：``caller.require_shop()``（参数里没有 shop_id），
#   所以运营（admin，没有店铺）调这些会拿到 403 —— AI 对买家说不说话，
#   是商户自己的事，平台不替他决定。
# ★ 路径仍在这一个 ``/api/assistant/*`` 家族里（见 §0 的那条理由）。
# ============================================================
@router.get(
    "/api/assistant/shop-setting",
    response_model=ApiResponse[ShopSettingOut],
    summary="本店的智能客服开关",
)
async def get_shop_setting(
    session: DbSession, user: CurrentUserDep
) -> ApiResponse[ShopSettingOut]:
    caller = await _require_caller(user, session)
    return ApiResponse.ok(await service.get_shop_setting(session, caller=caller))


@router.put(
    "/api/assistant/shop-setting",
    response_model=ApiResponse[ShopSettingOut],
    summary="开/关本店的智能客服",
)
async def set_shop_setting(
    session: DbSession, user: CurrentUserDep, req: ShopSettingIn
) -> ApiResponse[ShopSettingOut]:
    caller = await _require_caller(user, session)
    return ApiResponse.ok(
        await service.set_shop_setting(session, caller=caller, ai_enabled=req.ai_enabled)
    )


@router.get(
    "/api/assistant/shop-faq",
    response_model=ApiResponse[list[ShopFaqOut]],
    summary="本店的问答列表（店小蜜的知识库）",
)
async def list_faq(
    session: DbSession, user: CurrentUserDep
) -> ApiResponse[list[ShopFaqOut]]:
    caller = await _require_caller(user, session)
    return ApiResponse.ok(await service.list_faq(session, caller=caller))


@router.post(
    "/api/assistant/shop-faq",
    response_model=ApiResponse[ShopFaqOut],
    summary="加一条问答",
)
async def create_faq(
    session: DbSession, user: CurrentUserDep, req: ShopFaqIn
) -> ApiResponse[ShopFaqOut]:
    caller = await _require_caller(user, session)
    return ApiResponse.ok(await service.create_faq(session, caller=caller, req=req))


@router.put(
    "/api/assistant/shop-faq/{faq_id}",
    response_model=ApiResponse[ShopFaqOut],
    summary="改一条问答",
)
async def update_faq(
    session: DbSession, user: CurrentUserDep, faq_id: int, req: ShopFaqIn
) -> ApiResponse[ShopFaqOut]:
    """★ 不是本店的问答 → **404**（不是 403）：与全项目的归属校验一致。"""
    caller = await _require_caller(user, session)
    return ApiResponse.ok(
        await service.update_faq(session, caller=caller, faq_id=faq_id, req=req)
    )


@router.delete(
    "/api/assistant/shop-faq/{faq_id}",
    response_model=ApiResponse[None],
    summary="删一条问答",
)
async def delete_faq(
    session: DbSession, user: CurrentUserDep, faq_id: int
) -> ApiResponse[None]:
    caller = await _require_caller(user, session)
    await service.delete_faq(session, caller=caller, faq_id=faq_id)
    return ApiResponse.ok(None)


@router.get(
    "/api/assistant/shop-bot/stats",
    response_model=ApiResponse[ShopBotStatsOut],
    summary="智能客服今日计数与 token 用量",
)
async def shopbot_stats(
    session: DbSession, user: CurrentUserDep
) -> ApiResponse[ShopBotStatsOut]:
    caller = await _require_caller(user, session)
    return ApiResponse.ok(await service.shopbot_stats(session, caller=caller))
