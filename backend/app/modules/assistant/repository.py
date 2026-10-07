"""assistant 模块的数据库访问。只碰 ``assistant`` schema 自己的表。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.assistant.models import (
    BOT_PENDING,
    BOT_RUNNING,
    INFLIGHT_STATUSES,
    MESSAGE_ASSISTANT,
    MESSAGE_DONE,
    MESSAGE_USER,
    BotTurn,
    Conversation,
    Message,
    ShopFaq,
    ShopSetting,
    UsageDaily,
)


# ------------------------------------------------------------------
# 会话
# ------------------------------------------------------------------
async def get_latest_conversation(session: AsyncSession, user_id: int) -> Conversation | None:
    """这个用户**最近动过**的那条会话（没有则 None）。

    ★ 排序用 ``(updated_at DESC, id DESC)``：``updated_at`` 只在提问时被推进，
      同一毫秒撞车时用雪花 id 兜底 —— 少了第二个键，同毫秒的两条谁在前是不确定的。
    """
    return await session.scalar(
        select(Conversation)
        .where(Conversation.owner_user_id == user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(1)
    )


async def list_conversations(
    session: AsyncSession, user_id: int, *, before_id: int | None, limit: int
) -> list[Conversation]:
    """会话列表，**最近动过的在前**。多取一条给调用方判 ``has_more``。

    ★ 游标用 ``id`` 而不是 ``updated_at``：列表是"最近动过的在前"，而
      ``updated_at`` 会随着继续聊天而变 —— 用它会**翻页时漏掉/重复**正在聊的那条。
      ``id`` 是雪花的、单调，做游标稳定。于是排序键与游标键**不是同一个**，
      这条是有意的取舍（代价：同一毫秒内新建的两条顺序可能和 id 不一致，
      而这在列表页无感）。
    """
    stmt = select(Conversation).where(Conversation.owner_user_id == user_id)
    if before_id is not None:
        stmt = stmt.where(Conversation.id < before_id)
    stmt = stmt.order_by(
        Conversation.updated_at.desc(), Conversation.id.desc()
    ).limit(limit + 1)
    return list(await session.scalars(stmt))


async def get_conversation(session: AsyncSession, conversation_no: str) -> Conversation | None:
    return await session.scalar(
        select(Conversation).where(Conversation.conversation_no == conversation_no)
    )


async def insert_conversation(session: AsyncSession, conv: Conversation) -> None:
    session.add(conv)


async def touch_conversation(
    session: AsyncSession, conversation_no: str, *, title: str | None, now: datetime
) -> None:
    """更新会话的"最近活跃"；``title`` 只在需要时（首次提问）写。"""
    values: dict[str, Any] = {"updated_at": now}
    if title is not None:
        values["title"] = title
    await session.execute(
        update(Conversation).where(Conversation.conversation_no == conversation_no).values(**values)
    )


# ------------------------------------------------------------------
# 消息
# ------------------------------------------------------------------
async def insert_message(session: AsyncSession, message: Message) -> None:
    session.add(message)


async def get_message(session: AsyncSession, message_id: int) -> Message | None:
    return await session.get(Message, message_id)


async def list_messages(
    session: AsyncSession,
    conversation_no: str,
    *,
    before_id: int | None = None,
    limit: int = 50,
) -> tuple[list[Message], bool]:
    """会话里**最新**的 N 条消息（``before_id`` 给定时则是它**之前**的 N 条）。

    返回 ``(消息, 是否还有更早的)``，消息**按时间正序**。

    ★ 取最新的 N 条再翻回正序 —— 直接正序取前 N 条在长会话里会永远给出最早的那几条。
    ★ **多取一条**判 ``has_more``：不然就得再发一次 COUNT，而这里只是翻页。
    """
    stmt = select(Message).where(Message.conversation_no == conversation_no)
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    rows = list(await session.scalars(stmt.order_by(Message.id.desc()).limit(limit + 1)))

    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, has_more


async def list_history(
    session: AsyncSession, conversation_no: str, *, before_id: int, limit: int
) -> list[Message]:
    """本轮提问之前的历史（只取 user / assistant 的**已完成**消息）。

    给模型当上下文用。``role`` 与 ``status`` 的过滤在这里：
    - 只要 user / assistant —— 工具结果不是历史（见 models 的模块 docstring）；
    - 助手消息必须 DONE，否则会把一条"正在处理中"的空答案当上下文喂进去。
    """
    rows = list(
        await session.scalars(
            select(Message)
            .where(
                Message.conversation_no == conversation_no,
                Message.id < before_id,
                Message.role.in_((MESSAGE_USER, MESSAGE_ASSISTANT)),
                Message.status == MESSAGE_DONE,
            )
            .order_by(Message.id.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    return rows


async def has_inflight(session: AsyncSession, conversation_no: str) -> bool:
    """这条会话上还有没有没答完的消息。``ASSISTANT_BUSY`` 判据。"""
    return bool(
        await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_no == conversation_no,
                Message.status.in_(INFLIGHT_STATUSES),
            )
        )
    )


async def list_stuck_messages(
    session: AsyncSession, *, created_before: datetime, limit: int = 100
) -> list[Message]:
    """卡住的消息（worker 崩了 / 队列丢了），给 reaper 兜底用。"""
    return list(
        await session.scalars(
            select(Message)
            .where(
                Message.status.in_(INFLIGHT_STATUSES),
                Message.created_at < created_before,
            )
            .order_by(Message.id)
            .limit(limit)
        )
    )


def settle_message(
    message: Message,
    *,
    status: int,
    content: str | None = None,
    error_code: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    llm_model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    now: datetime,
) -> None:
    """把一轮的落库字段一次写齐（成功 / 失败 / 降级共用）。

    ★ 一个函数而不是三处分别赋值：这三个结局写的是**同一批字段**，
      分开写早晚会漏一个（典型的是漏掉 ``updated_at``，reaper 就会把
      已经答完的消息又当成卡住的）。
    """
    message.status = status
    message.content = content
    message.error_code = error_code
    message.tool_calls = tool_calls
    message.llm_model = llm_model
    message.prompt_tokens = prompt_tokens
    message.completion_tokens = completion_tokens
    message.updated_at = now


# ------------------------------------------------------------------
# 记账
# ------------------------------------------------------------------
async def add_usage(
    session: AsyncSession,
    *,
    day: str,
    scope: int,
    scope_id: int,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """累加当日用量。

    ★ 用 ``ON CONFLICT DO UPDATE`` 而不是"先查再写"：同一个店的多个人可能同时提问，
      读-改-写会丢更新，而这一步记的是**钱**。
    """
    total = prompt_tokens + completion_tokens
    stmt = pg_insert(UsageDaily).values(
        day=day,
        scope=scope,
        scope_id=scope_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
        request_count=1,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uk_assistant_usage_daily",
            set_={
                "prompt_tokens": UsageDaily.prompt_tokens + stmt.excluded.prompt_tokens,
                "completion_tokens": (
                    UsageDaily.completion_tokens + stmt.excluded.completion_tokens
                ),
                "total_tokens": UsageDaily.total_tokens + stmt.excluded.total_tokens,
                "request_count": UsageDaily.request_count + 1,
                "updated_at": func.now(),
            },
        )
    )


async def get_usage_tokens(session: AsyncSession, *, day: str, scope: int, scope_id: int) -> int:
    """当日已用的 token 数（没有记录就是 0）。"""
    total = await session.scalar(
        select(UsageDaily.total_tokens).where(
            UsageDaily.day == day,
            UsageDaily.scope == scope,
            UsageDaily.scope_id == scope_id,
        )
    )
    return int(total or 0)


# ------------------------------------------------------------------
# 店小蜜：店铺开关 / 商户问答 / 执行台账
# ------------------------------------------------------------------
async def get_shop_setting(session: AsyncSession, shop_id: int) -> ShopSetting | None:
    return await session.scalar(select(ShopSetting).where(ShopSetting.shop_id == shop_id))


async def is_ai_enabled(session: AsyncSession, shop_id: int) -> bool:
    """这个店开了智能客服没有。**没有那一行 = 没开过 = 关**。"""
    return bool(
        await session.scalar(
            select(ShopSetting.ai_enabled).where(ShopSetting.shop_id == shop_id)
        )
    )


async def upsert_shop_setting(
    session: AsyncSession, *, shop_id: int, ai_enabled: bool, now: datetime
) -> None:
    """开/关。用 ``ON CONFLICT DO UPDATE`` —— 一店一行，第一次写就是插入。"""
    stmt = pg_insert(ShopSetting).values(
        shop_id=shop_id, ai_enabled=ai_enabled, created_at=now, updated_at=now
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ShopSetting.shop_id],
            set_={"ai_enabled": stmt.excluded.ai_enabled, "updated_at": now},
        )
    )


async def list_enabled_shop_ids(
    session: AsyncSession, shop_ids: Sequence[int]
) -> set[int]:
    """这批店里哪些开了智能客服（一次查完，别在循环里查）。"""
    wanted = list({int(s) for s in shop_ids})
    if not wanted:
        return set()
    rows = await session.scalars(
        select(ShopSetting.shop_id).where(
            ShopSetting.shop_id.in_(wanted), ShopSetting.ai_enabled.is_(True)
        )
    )
    return {int(s) for s in rows}


async def list_faq(
    session: AsyncSession, shop_id: int, *, enabled_only: bool = False
) -> list[ShopFaq]:
    stmt = select(ShopFaq).where(ShopFaq.shop_id == shop_id)
    if enabled_only:
        stmt = stmt.where(ShopFaq.enabled.is_(True))
    return list((await session.scalars(stmt.order_by(ShopFaq.id))).all())


async def get_faq(session: AsyncSession, *, shop_id: int, faq_id: int) -> ShopFaq | None:
    """取一条问答。**带 shop_id 条件** —— 归属不对就是"不存在"（与全项目一致）。"""
    return await session.scalar(
        select(ShopFaq).where(ShopFaq.id == faq_id, ShopFaq.shop_id == shop_id)
    )


async def insert_faq(session: AsyncSession, faq: ShopFaq) -> None:
    session.add(faq)


async def delete_faq(session: AsyncSession, faq: ShopFaq) -> None:
    await session.delete(faq)


async def insert_turn_ignore_conflict(session: AsyncSession, turn: BotTurn) -> bool:
    """排一轮活儿。返回**是否真的插进去了**（撞 ``source_message_id`` 唯一键就返回 False）。

    ★ 这个返回值就是幂等的实现：扫描每 3 秒一轮，同一条买家消息只会插进去一次。
    """
    stmt = (
        pg_insert(BotTurn)
        .values(
            id=turn.id,
            source_message_id=turn.source_message_id,
            ticket_no=turn.ticket_no,
            shop_id=turn.shop_id,
            buyer_user_id=turn.buyer_user_id,
            status=BOT_PENDING,
        )
        .on_conflict_do_nothing(constraint="uk_assistant_bot_turn_source")
        .returning(BotTurn.id)
    )
    return await session.scalar(stmt) is not None


async def get_turn(session: AsyncSession, source_message_id: int) -> BotTurn | None:
    return await session.scalar(
        select(BotTurn).where(BotTurn.source_message_id == source_message_id)
    )


async def list_stuck_turns(
    session: AsyncSession, *, pending_before: datetime, running_before: datetime
) -> list[BotTurn]:
    """还在飞、且已经老得不正常的轮次。

    ★ 两种卡法用**两个时间**分开筛：``PENDING`` 卡住（入队没成）重投递就够，
      ``RUNNING`` 卡住（模型跑了）不能重跑 —— 见 ``service.reap_stuck_turns``。
    """
    stmt = (
        select(BotTurn)
        .where(
            (BotTurn.status == BOT_PENDING) & (BotTurn.created_at < pending_before)
            | (BotTurn.status == BOT_RUNNING) & (BotTurn.created_at < running_before)
        )
        .order_by(BotTurn.id)
        .limit(100)
    )
    return list((await session.scalars(stmt)).all())


async def count_turns_by_status(
    session: AsyncSession, *, shop_id: int, since: datetime
) -> dict[int, int]:
    """店小蜜这段时间各状态的条数（商户页面上的"今天答了多少、转了多少"）。"""
    rows = await session.execute(
        select(BotTurn.status, func.count())
        .where(BotTurn.shop_id == shop_id, BotTurn.created_at >= since)
        .group_by(BotTurn.status)
    )
    return {int(status): int(count) for status, count in rows.all()}
