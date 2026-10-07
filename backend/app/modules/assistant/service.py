"""assistant 的业务层：提交提问、跑工具调用循环、限流与降级。见 docs/20。

一次提问的生命周期：

```
submit()  落 user 消息 + 一条 PENDING 的 assistant 消息，提交后才投递 job
   ↓
worker: run_turn()  复核身份 → 组装 prompt → 循环调模型/工具 → 落答案
   ↓
前端轮询 GET 会话详情，看到 status != PENDING 就停
```

★ **为什么不让 API 同步等**：``web-admin`` 的 axios 超时是 10 秒，而一次问答要
  2–5 轮模型调用、每轮几秒 —— 同步实现**必然**超时；何况演示机的 uvicorn 只有
  1 个 worker，把唯一的响应通道占住十几秒是架构问题而不只是体验问题。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import after_commit
from app.core.config import get_settings
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.core.redis_keys import (
    assistant_breaker,
    assistant_breaker_failures,
    assistant_rate_shop,
    assistant_rate_shop_buyer,
    assistant_rate_user,
)
from app.core.snowflake import next_id
from app.modules.account import service as account_service
from app.modules.assistant import knowledge as knowledge_mod
from app.modules.assistant import repository as repo
from app.modules.assistant import rules, tools
from app.modules.assistant.models import (
    ACTOR_BUYER,
    BOT_DONE,
    BOT_FAILED,
    BOT_PENDING,
    BOT_RUNNING,
    BOT_SKIPPED,
    MESSAGE_ASSISTANT,
    MESSAGE_DEGRADED,
    MESSAGE_DONE,
    MESSAGE_FAILED,
    MESSAGE_PENDING,
    MESSAGE_RUNNING,
    MESSAGE_USER,
    USAGE_SCOPE_SHOP,
    USAGE_SCOPE_USER,
    BotTurn,
    Conversation,
    Message,
    ShopFaq,
)
from app.modules.assistant.provider import ChatReply, LlmClient, LlmError
from app.modules.assistant.schemas import (
    ShopBotStatsOut,
    ShopFaqIn,
    ShopFaqOut,
    ShopSettingOut,
)
from app.modules.support import service as support_service
from app.modules.support.models import SENDER_USER as SUPPORT_SENDER_USER
from app.modules.support.models import SOURCE_AI_ASSISTANT
from app.modules.support.schemas import OpenTicketRequest
from app.modules.trade.order_no import build_conversation_no
from app.worker.enqueue import enqueue_now

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = get_logger(__name__)

# 投递给 worker 的任务名。worker 侧用同一个常量注册（``assistant.tasks``）
JOB_RUN_TURN = "run_assistant_turn"

# 限流的固定窗口
RATE_WINDOW_SECONDS = 60

# 取多少条历史再按字符预算裁。条数是粗筛，真正的上限由 ``rules.trim_history`` 按字符卡
HISTORY_MESSAGES = 20

# 消息停在"还在飞"超过这么久，就认定它再也不会被跑了（worker 崩 / 队列丢）
STUCK_SECONDS = 180

# 模型没给出任何文本时的兜底答复。★ 宁可说"我没答上来"，也不要给一个空泡泡
FALLBACK_ANSWER = "抱歉，我暂时没能整理出答案。可以换个说法再问一次，或者点「转人工」。"
TIMEOUT_ANSWER = "助手这次没能响应。可以重试一次，或者点「转人工」。"
BUSY_ANSWER = "助手暂时不可用（上游异常）。稍后再试，或点「转人工」。"


# ==================================================================
# 闸：限流 / 额度 / 熔断
# ==================================================================
async def _check_rate_limit(caller: rules.Caller) -> None:
    """分钟级固定窗口。按用户 + 按店铺两道。

    ★ 照 ``support.service`` 的写法（``incr`` → 首次 ``expire`` → 超阈值抛），
      不引 Lua：限流用不着原子脚本，多一次往返换来的可读性更值。
    """
    settings = get_settings()
    redis = get_redis()
    pairs = [(assistant_rate_user(caller.user_id), settings.assistant_user_rate_per_minute)]
    if caller.shop_id is not None:
        pairs.append((assistant_rate_shop(int(caller.shop_id)), settings.assistant_shop_rate_per_minute))
    for key, limit in pairs:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, RATE_WINDOW_SECONDS)
        if count > limit:
            raise BizError(ErrorCode.ASSISTANT_RATE_LIMITED)


async def _check_quota(session: AsyncSession, caller: rules.Caller, *, day: str) -> None:
    """每日 token 预算。**在入队之前查** —— 别等钱花完再拒。"""
    settings = get_settings()
    used = await repo.get_usage_tokens(
        session, day=day, scope=USAGE_SCOPE_USER, scope_id=caller.user_id
    )
    if used >= settings.assistant_daily_token_budget_user:
        raise BizError(ErrorCode.ASSISTANT_QUOTA_EXCEEDED)
    if caller.shop_id is not None:
        used_shop = await repo.get_usage_tokens(
            session, day=day, scope=USAGE_SCOPE_SHOP, scope_id=int(caller.shop_id)
        )
        if used_shop >= settings.assistant_daily_token_budget_shop:
            raise BizError(
                ErrorCode.ASSISTANT_QUOTA_EXCEEDED, "这个店铺今天的助手额度已用完，明天再来"
            )


async def _assert_breaker_closed() -> None:
    """上游连续失败时熔断。★ 不熔断的话，上游挂了之后**每个**请求都要等满超时，
    把仅有的 1 个 uvicorn worker 全占死。"""
    if await get_redis().exists(assistant_breaker()):
        raise BizError(ErrorCode.ASSISTANT_DISABLED)


async def _note_upstream_result(*, ok: bool) -> None:
    """记一次上游结果：成功清零，失败累积到阈值就打开开关。"""
    settings = get_settings()
    redis = get_redis()
    if ok:
        await redis.delete(assistant_breaker_failures())
        return
    key = assistant_breaker_failures()
    count = await redis.incr(key)
    if count == 1:
        # 计数本身也要过期，否则某天失败几次之后永远"离熔断只差一次"
        await redis.expire(key, settings.assistant_breaker_cooldown_seconds * 4)
    if count >= settings.assistant_breaker_threshold:
        await redis.set(
            assistant_breaker(), "1", ex=settings.assistant_breaker_cooldown_seconds
        )
        logger.warning("助手上游连续失败，已熔断", extra={"failures": int(count)})


# ==================================================================
# 会话与消息（API 侧）
# ==================================================================
async def _create_conversation(
    session: AsyncSession, *, caller: rules.Caller, question: str
) -> Conversation:
    """开一条新会话。

    ★ 这里**不再需要保存点**。以前要它是因为"每用户同时只有一条进行中会话"那条
      部分唯一索引：同时点两下提交会撞唯一键，得用 ``begin_nested`` 只回滚那次插入。
      现在一条用户能有多条会话，插入不可能撞 —— 那条约束已经被迁移删掉了。
    """
    now = datetime.now(UTC)
    snowflake = next_id()
    conv = Conversation(
        id=snowflake,
        conversation_no=build_conversation_no(snowflake, now=now),
        owner_user_id=caller.user_id,
        owner_role=caller.role,
        shop_id=caller.shop_id,
        title=rules.title_of(question),
    )
    await repo.insert_conversation(session, conv)
    await session.flush()
    return conv


async def _resolve_conversation(
    session: AsyncSession, *, caller: rules.Caller, conversation_no: str | None, question: str
) -> Conversation:
    """接着某条已有会话，或者开一条新的（``conversation_no`` 为空 = 新对话）。

    ★ 归属校验在这里，而且**不是自己的会话一律 404**（不是 403）——
      与项目里其他归属校验一致：不给"遍历探测别人有哪些会话"留口子。
    """
    if conversation_no is None:
        return await _create_conversation(session, caller=caller, question=question)
    conv = await repo.get_conversation(session, conversation_no)
    if conv is None or int(conv.owner_user_id) != caller.user_id:
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    return conv


async def submit_question(
    session: AsyncSession,
    *,
    caller: rules.Caller,
    question: str,
    conversation_no: str | None = None,
) -> Message:
    """提交一个问题。返回那条 **PENDING 的 assistant 消息**（前端拿它的 id 轮询）。

    ``conversation_no`` 为空 → **开一条新会话**；给了 → 追加到那一条
    （「新对话」在前端就是"把当前会话号清空"）。
    """
    settings = get_settings()
    if not settings.assistant_enabled:
        raise BizError(ErrorCode.ASSISTANT_DISABLED)
    await _assert_breaker_closed()
    await _check_rate_limit(caller)
    await _check_quota(session, caller, day=rules.day_key(datetime.now(UTC)))

    conv = await _resolve_conversation(
        session, caller=caller, conversation_no=conversation_no, question=question
    )
    if await repo.has_inflight(session, conv.conversation_no):
        # 不并答：工具调用循环共用一条历史，交错会让两轮互相看见对方半截的结果
        raise BizError(ErrorCode.ASSISTANT_BUSY)

    now = datetime.now(UTC)
    await repo.insert_message(
        session,
        Message(
            conversation_no=conv.conversation_no,
            role=MESSAGE_USER,
            status=MESSAGE_DONE,
            content=question,
            actor_user_id=caller.user_id,
            actor_role=caller.role,
            actor_shop_id=caller.shop_id,
        ),
    )
    pending = Message(
        conversation_no=conv.conversation_no,
        role=MESSAGE_ASSISTANT,
        status=MESSAGE_PENDING,
        actor_user_id=caller.user_id,
        actor_role=caller.role,
        actor_shop_id=caller.shop_id,
    )
    await repo.insert_message(session, pending)
    # 拿 Identity 主键，供投递与前端轮询
    await session.flush()
    await repo.touch_conversation(
        session, conv.conversation_no, title=None if conv.title else rules.title_of(question), now=now
    )

    # ★ 提交之后才投递。抢在提交前投递的话，worker 可能先于本事务可见就读那条消息
    #   —— 「刚写完就读不到」（docs/10 §提交后回调）。
    message_id = int(pending.id)
    after_commit.defer(session, lambda: enqueue_now(JOB_RUN_TURN, message_id))
    return pending


async def load_conversation(
    session: AsyncSession,
    *,
    caller: rules.Caller,
    conversation_no: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> tuple[Conversation | None, list[Message], bool]:
    """一条会话 + 它的消息（``before_id`` 给定时取**更早的一页**）。

    返回 ``(会话, 消息, 是否还有更早的)``。``conversation_no`` 为空 = 取**最近动过**的
    那一条（面板首次打开用，前端不必先知道会话号）。

    ★ 归属在这里校验：不是自己的会话**一律当作不存在**（路由层给 404），
      与项目里其他归属校验一致 —— 不给遍历探测留口子。
    """
    if conversation_no is None:
        conv = await repo.get_latest_conversation(session, caller.user_id)
    else:
        conv = await repo.get_conversation(session, conversation_no)
        if conv is not None and int(conv.owner_user_id) != caller.user_id:
            conv = None
    if conv is None:
        return None, [], False
    messages, has_more = await repo.list_messages(
        session, conv.conversation_no, before_id=before_id, limit=limit
    )
    return conv, messages, has_more


async def list_conversations(
    session: AsyncSession, *, caller: rules.Caller, before_id: int | None, limit: int
) -> tuple[list[Conversation], bool]:
    """会话列表（最近动过的在前）。返回 ``(列表, 是否还有更多)``。

    ★ **只列自己的** —— 条件里那个 ``owner_user_id`` 就是权限边界，不靠调用方自觉。
    """
    rows = await repo.list_conversations(
        session, caller.user_id, before_id=before_id, limit=limit
    )
    has_more = len(rows) > limit
    return rows[:limit], has_more


# ==================================================================
# 跑一轮（worker 侧）
# ==================================================================
def caller_of(message: Message) -> rules.Caller:
    """身份快照 → ``Caller``。**worker 只信这一份**，不信模型、也不信 job 参数。"""
    return rules.Caller(
        user_id=int(message.actor_user_id),
        role=str(message.actor_role),
        shop_id=int(message.actor_shop_id) if message.actor_shop_id is not None else None,
    )


async def _assert_identity_still_valid(session: AsyncSession, caller: rules.Caller) -> None:
    """提问那一刻到真正取数之间，身份可能已经变了（关店 / 改角色）。

    商家侧能复核的是**店铺还在正常营业**；不过关就直接拒，一个工具都不调。
    """
    if caller.is_admin:
        return
    shop = await account_service.get_my_shop(session, caller.user_id)
    if not account_service.shop_is_active(shop.status):
        raise BizError(ErrorCode.FORBIDDEN, "店铺当前状态不能使用助手")


async def _build_messages(
    session: AsyncSession, *, caller: rules.Caller, message: Message
) -> list[dict[str, Any]]:
    """system prompt（知识库 + 身份）+ 裁剪过的历史。"""
    shop_name: str | None = None
    if not caller.is_admin and caller.shop_id is not None:
        shop = await account_service.get_my_shop(session, caller.user_id)
        shop_name = shop.name

    history = await repo.list_history(
        session, message.conversation_no, before_id=int(message.id), limit=HISTORY_MESSAGES
    )
    turns = [
        {
            "role": "user" if row.role == MESSAGE_USER else "assistant",
            "content": row.content or "",
        }
        for row in history
    ]
    system = rules.build_system_prompt(
        caller=caller,
        shop_name=shop_name,
        # ★ 后台助手按身份取（运营多一份"只有运营能看"的口径，商家看不到）；
        #   买家那一类**绝不能**混进来 —— 见 knowledge.py 的受众说明
        knowledge=knowledge_mod.load(knowledge_mod.audiences_for_role(caller.role)),
    )
    return [{"role": "system", "content": system}, *rules.trim_history(turns)]


async def _execute_call(
    tool_ctx: tools.ToolContext, call: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑一个工具调用。**任何失败都变成一条结果回给模型**，不炸掉整轮。

    返回 ``(给模型看的结果, 审计记录)``。

    ★ 工具报错不回给模型的话，用户会看到"助手出错了"，而真正有用的是
      "我没查到这一单" —— 后者模型能用人话转述，前者只能干瞪眼。
    """
    caller = tool_ctx.caller
    started = datetime.now(UTC)
    spec = next((t for t in tools.tools_for(caller) if t.name == call.name), None)
    if spec is None:
        # 模型幻觉/注入了本身份没有的工具名。记审计，但**不执行**
        result: dict[str, Any] = {"error": f"当前身份没有这个工具：{call.name}"}
    else:
        try:
            args = rules.parse_tool_args(call.arguments, spec.parameters)
        except rules.ToolArgsError as exc:
            result = {"error": str(exc)}
        else:
            try:
                result = await tools.execute(tool_ctx, call.name, args)
            except BizError as exc:
                result = {"error": exc.message}
            except Exception:
                logger.exception("工具执行异常", extra={"tool": call.name})
                result = {"error": "查询失败"}
    elapsed = int((datetime.now(UTC) - started).total_seconds() * 1000)
    audit = {
        "name": call.name,
        "arguments": call.arguments,
        "latency_ms": elapsed,
        # 审计里留 result 的**预览**（完整结果已经在上下文里花过钱了，
        # 再存一份全量只会把这张表撑大）
        "result_preview": rules.clip_text(str(result), 500),
    }
    return result, audit


async def _run_loop(
    *,
    messages: list[dict[str, Any]],
    specs: list[tools.ToolSpec],
    tool_ctx: tools.ToolContext,
    llm: LlmClient,
) -> tuple[str, list[dict[str, Any]], int, int, str, bool]:
    """模型 ↔ 工具 的循环。**后台助手与店小蜜共用**（只是入口不同）。

    返回 ``(答案, 审计, prompt_tokens, completion_tokens, model, 是否降级)``。
    """
    settings = get_settings()
    max_rounds = max(1, settings.assistant_max_tool_rounds)
    payload = tools.openai_tools(specs)

    prompt_tokens = completion_tokens = 0
    model = ""
    audit: list[dict[str, Any]] = []
    seen: set[str] = set()
    repeated = False
    answer: str | None = None
    degraded = False

    for round_index in range(max_rounds):
        # 最后一轮、或模型开始重复调同一个工具时，强制它用已有信息作答 ——
        # 再放它一轮也只是再多花一次钱
        force = rules.should_force_summary(
            round_index=round_index, max_rounds=max_rounds, repeated=repeated
        )
        reply: ChatReply = await llm.chat(
            messages=messages, tools=payload, tool_choice="none" if force else None
        )
        prompt_tokens += reply.prompt_tokens
        completion_tokens += reply.completion_tokens
        model = reply.model or model

        if not reply.tool_calls:
            answer = reply.content
            break

        # 把"我要调这些工具"作为 assistant 消息记进上下文，工具结果必须跟在一个
        # 声明了 tool_calls 的 assistant 消息后面，否则 OpenAI 兼容层会报 400
        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in reply.tool_calls
                ],
            }
        )
        for call in reply.tool_calls:
            try:
                args = rules.parse_tool_args(
                    call.arguments, _schema_of(specs, call.name)
                )
                fingerprint = rules.canonical_args(call.name, args)
            except rules.ToolArgsError:
                fingerprint = f"{call.name}:<坏参数>"
            if fingerprint in seen:
                repeated = True
            seen.add(fingerprint)

            result, record = await _execute_call(tool_ctx, call)
            audit.append(record)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": rules.encode_tool_result(call.name, result),
                }
            )
        if repeated:
            degraded = True
    else:
        degraded = True

    if not answer:
        degraded = True
        answer = FALLBACK_ANSWER
    return answer, audit, prompt_tokens, completion_tokens, model, degraded


def _schema_of(specs: list[tools.ToolSpec], name: str) -> dict[str, Any]:
    """取工具的参数 schema；没有这个工具就给一个"什么都不接受"的 schema。

    这样越权的工具调用会走"参数校验失败"这条路（结果回给模型），
    而不是在这里抛出去。
    """
    for spec in specs:
        if spec.name == name:
            return spec.parameters
    return {"type": "object", "properties": {}, "additionalProperties": False}


async def claim(session: AsyncSession, message_id: int) -> bool:
    """领取一条待跑的消息：``PENDING → RUNNING``。返回是否领到。

    ★ **单独一步、单独一个事务**，是这一块最容易做错的地方。任务重投递时
      （ARQ 默认会重试），如果"标记已开始"和"跑模型"在同一个事务里，模型调用
      一失败就把标记一起回滚了 —— 重试会**再花一次钱**。把它先提交出去，
      重试就只会看到 RUNNING 然后直接返回。

    代价是：真跑挂了的消息会停在 RUNNING，靠 ``reap_stuck`` 兜底收尾。
    这比"失败就重放"划算 —— LLM 调用不适合盲目重试。
    """
    message = await repo.get_message(session, message_id)
    if message is None or message.status != MESSAGE_PENDING:
        return False
    message.status = MESSAGE_RUNNING
    message.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def run_turn(session: AsyncSession, *, message_id: int, llm: LlmClient) -> None:
    """跑一轮问答，把消息推到终态。

    **前提**：这条消息已经被 :func:`claim` 领走（状态 RUNNING）。这里不再改状态起点，
    只负责"复核身份 → 循环调模型/工具 → 落答案"。
    """
    message = await repo.get_message(session, message_id)
    if message is None or message.status != MESSAGE_RUNNING:
        return

    caller = caller_of(message)
    try:
        await _assert_identity_still_valid(session, caller)
        answer, audit, prompt_t, completion_t, model, degraded = await _run_loop(
            messages=await _build_messages(session, caller=caller, message=message),
            specs=tools.tools_for(caller),
            tool_ctx=tools.ToolContext(session=session, caller=caller),
            llm=llm,
        )
    except LlmError as exc:
        # ★ 必须把上游的原始报文记下来。用户看到的是"助手暂时无法回答"这种安全文案，
        #   而真正的原因（模型名写错 / 没开通 / 这个模型不接受 enable_thinking 这个字段）
        #   只在 exc 里。不记日志的话，排查时是瞎的。
        logger.warning("助手上游调用失败：%s", exc)
        await _note_upstream_result(ok=False)
        repo.settle_message(
            message,
            status=MESSAGE_FAILED,
            content=TIMEOUT_ANSWER if exc.timeout else BUSY_ANSWER,
            error_code=(
                ErrorCode.ASSISTANT_UPSTREAM_TIMEOUT.name
                if exc.timeout
                else ErrorCode.ASSISTANT_UPSTREAM_ERROR.name
            ),
            now=datetime.now(UTC),
        )
        return
    except BizError as exc:
        # 身份复核没过 / 会话已被换掉：都不算上游故障，不参与熔断计数
        repo.settle_message(
            message,
            status=MESSAGE_FAILED,
            content=exc.message,
            error_code=exc.code.name,
            now=datetime.now(UTC),
        )
        return

    await _note_upstream_result(ok=True)
    repo.settle_message(
        message,
        status=MESSAGE_DEGRADED if degraded else MESSAGE_DONE,
        content=answer,
        tool_calls=audit,
        llm_model=model or None,
        prompt_tokens=prompt_t,
        completion_tokens=completion_t,
        now=datetime.now(UTC),
    )

    # 记账。★ 按用户与按店铺各记一份 —— 一个店多人共用账号时，
    #   用户级那道额度不是真实成本单位
    day = rules.day_key(datetime.now(UTC))
    await repo.add_usage(
        session,
        day=day,
        scope=USAGE_SCOPE_USER,
        scope_id=caller.user_id,
        prompt_tokens=prompt_t,
        completion_tokens=completion_t,
    )
    if caller.shop_id is not None:
        await repo.add_usage(
            session,
            day=day,
            scope=USAGE_SCOPE_SHOP,
            scope_id=int(caller.shop_id),
            prompt_tokens=prompt_t,
            completion_tokens=completion_t,
        )


async def reap_stuck(session: AsyncSession) -> int:
    """兜底：把卡住的消息标成失败。

    worker 崩了、Redis 重启丢了队列 —— 那些消息会永远停在「处理中」，
    前端一直转圈。这里给它们一个结局。

    ``error_code`` 复用 ``ASSISTANT_UPSTREAM_TIMEOUT``：对用户来说
    "没响应，可以重试"就是实情，不必为"为什么没响应"再分一个码。
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=STUCK_SECONDS)
    rows = await repo.list_stuck_messages(session, created_before=cutoff)
    for row in rows:
        repo.settle_message(
            row,
            status=MESSAGE_FAILED,
            content=TIMEOUT_ANSWER,
            error_code=ErrorCode.ASSISTANT_UPSTREAM_TIMEOUT.name,
            now=datetime.now(UTC),
        )
    if rows:
        logger.warning("助手里有卡住的消息被收尾", extra={"count": len(rows)})
    return len(rows)


# ==================================================================
# 转人工
# ==================================================================
# 带过去的对话轮数（一问一答算一轮）。太多会把工单首条撑得很长，
# 而客服真正要看的是"用户卡在哪"，最近三轮足够
HANDOFF_ROUNDS = 3
HANDOFF_SUMMARY_CHARS = 1000


def _handoff_summary(rows: list[Message], note: str | None) -> str:
    """把最近几轮对话压成工单首条消息（没有对话时就是那句补充说明）。"""
    lines = ["【由 AI 助手转来】"]
    for row in rows:
        if row.role == MESSAGE_USER:
            lines.append(f"用户问：{row.content or ''}")
        elif row.content:
            lines.append(f"助手答：{row.content}")
    if note:
        lines.append(f"补充说明：{note}")
    if rows:
        # 这句只在**真带了助手原话**时才有意义
        lines.append("（上面是助手的原话，供参考；具体请以系统里的数据为准）")
    return rules.clip_text("\n".join(lines), HANDOFF_SUMMARY_CHARS)


async def handoff_to_human(
    session: AsyncSession,
    redis: Redis,
    *,
    caller: rules.Caller,
    conversation_no: str | None = None,
    note: str | None = None,
) -> tuple[str, bool]:
    """把对话交给人工。返回 ``(工单号, 是否复用了已有会话)``。

    ``conversation_no`` 是**面板当前正看着的那一条**。★ 不能省成"取最近动过的那条"：
    用户可以在「历史对话」里翻回旧会话再点转人工，取最近一条等于把**另一条**的对话
    摘要交上去（转人工的内容和用户看到的完全对不上）。传了就按它校验归属。

    ``note`` 是可选的一句补充说明。**它也能独立成立** —— 面板上还一句话都没问就点
    「转人工」是合理诉求（"我直接想找客服"），这时工单首条就是这句说明，不必先逼用户
    跟助手绕一圈；真的一句都没有才拒绝。

    ★ **复用买家的开单入口**，不新写"商家开单"函数。核实过三处代码：
      ``open_ticket`` 与 ``reply_as_user`` 都**不检查角色**；不传 ``shop_id``
      时工单落在 **平台**队列（``PLATFORM_SHOP_ID``），所以它
      **不会出现在商家自己的店队列里**（那才是"自己跟自己说话"）；
      商家能用 ``list_mine`` 读回平台的回复。语义正好是"商家作为平台使用者提问"。

    ★ 但 **web-admin 目前没有「我提交的」入口** —— 商家开了单看不到回复，
      这正是这个项目里反复出现过的那类毛病（只写不读）。前端要补一个页签，
      见 docs/20 §7。
    """
    conv, rows, _ = await load_conversation(
        session,
        caller=caller,
        conversation_no=conversation_no,
        limit=HANDOFF_ROUNDS * 2,
    )
    if conversation_no is not None and conv is None:
        # 传了会话号却查不到（不存在 / 不是自己的）—— 当作不存在，别静默降级成"只转说明"
        raise BizError(ErrorCode.NOT_FOUND, "会话不存在")
    if conv is None and not note:
        raise BizError(
            ErrorCode.VALIDATION_ERROR,
            "先写一句想让客服帮你什么，或先问助手一个问题",
        )

    if conv is not None and conv.title:
        subject = f"助手转人工：{conv.title}"
    elif note:
        subject = f"转人工：{note}"
    else:
        subject = "助手转人工"

    detail = await support_service.open_ticket(
        session,
        user_id=caller.user_id,
        req=OpenTicketRequest(
            shop_id=None,
            source=SOURCE_AI_ASSISTANT,
            subject=subject[:120],
        ),
    )
    # 已有进行中的工单就复用它（``open_ticket`` 的语义），但**这一轮的诉求照样要送过去**：
    # 那条工单可能已经躺了几天，而这次点转人工问的是另一件事。
    # ★ 唯一的例外是"内容和工单最后一条一模一样"—— 那说明这次点击没带来任何新信息
    #   （连点两次、说明照抄），再贴一遍才是刷屏。
    # ★ 旧实现是**复用就一个字都不写**，于是"已有对话 + 不填说明"再转一次，平台那边
    #   什么也收不到，而用户看到的是"已把问题交给客服"—— 静默丢内容，最坏的一种。
    reused = bool(detail.messages)
    body = _handoff_summary(rows, note)
    if not detail.messages or detail.messages[-1].body != body:
        await support_service.reply_as_user(
            session,
            redis,
            user_id=caller.user_id,
            ticket_no=detail.ticket_no,
            body=body,
            images=(),
        )
    return detail.ticket_no, reused


# ==================================================================
# 店小蜜：买家在商城端问，AI 以**这家店**的身份答（docs/20 §14）
# ==================================================================
# 投递给 worker 的任务名（worker 侧用同一个常量注册）
JOB_RUN_BOT_TURN = "run_bot_turn"

# 一轮扫多少条。扫到的都会入队，真跑是 worker 的事
SWEEP_LIMIT = 50

# ``bot_turn`` 停在 PENDING 超过这么久就**重新投递**：入队那一步在事务之外，
# 可能没投出去（进程崩、Redis 抖）。★ PENDING 意味着模型**从没被调用过**，
# 所以重投递不会重复花钱 —— 这与 RUNNING 的处理方式必须分开（见 reap_stuck_turns）。
TURN_PENDING_RESEND_SECONDS = 60


async def sweep_ticket_turns(
    session: AsyncSession, *, now: datetime | None = None
) -> list[int]:
    """扫一遍"在等答复"的会话，给**每一条需要 AI 回答的买家消息**排一个 PENDING 台账。

    返回**新排上的** ``source_message_id``（入队是调用方的事）。

    ★ 为什么是**扫描**而不是"买家一发消息就直接入队"：后者要求 support 知道 AI
      存在（``support → assistant``）—— 反向依赖，铁律禁止。outbox 也不行：它是
      "通知"语义（由 notify 消费成站内信），要么让 support 写通知 topic 再让 notify
      转一道手，要么把 AI 编排塞进 notify。
      扫描则是"assistant 看自己的活儿"：support 只提供一个**泛型**的
      "哪些会话在等答复"（``list_awaiting_tickets``），全程不知道 AI 存在。
    ★ 幂等三道：① 已答过的会话最后一条是 AI 的，下一轮根本进不了候选；
      ② 撞 ``bot_turn.source_message_id`` 唯一键；③ ARQ 按"函数名 + 参数"去重 job。
    """
    settings = get_settings()
    if not (settings.assistant_enabled and settings.shopbot_enabled):
        return []
    since = (now or datetime.now(UTC)) - timedelta(
        minutes=settings.shopbot_sweep_window_minutes
    )
    awaiting = await support_service.list_awaiting_tickets(
        session, since=since, limit=SWEEP_LIMIT
    )
    if not awaiting:
        return []
    enabled = await repo.list_enabled_shop_ids(
        session, [int(item.shop_id) for item in awaiting]
    )
    created: list[int] = []
    for item in awaiting:
        shop_id = int(item.shop_id)
        if shop_id not in enabled:
            continue
        if await repo.insert_turn_ignore_conflict(
            session,
            BotTurn(
                id=next_id(),
                source_message_id=int(item.last_message_id),
                ticket_no=item.ticket_no,
                shop_id=shop_id,
                buyer_user_id=int(item.user_id),
                status=BOT_PENDING,
            ),
        ):
            created.append(int(item.last_message_id))
    return created


async def claim_turn(session: AsyncSession, source_message_id: int) -> bool:
    """领取一轮：``PENDING → RUNNING``。返回是否领到。

    ★ 与 ``claim`` 同一条纪律：**单独一步、单独一个事务**。重投递时若把"已开始"
      和"跑模型"放同一个事务，模型一失败就把标记一起回滚了 —— 会**再花一次钱**。
    """
    turn = await repo.get_turn(session, source_message_id)
    if turn is None or turn.status != BOT_PENDING:
        return False
    turn.status = BOT_RUNNING
    turn.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def answer_ticket(
    session: AsyncSession, *, source_message_id: int, llm: LlmClient
) -> None:
    """跑一轮店小蜜：复核 → 组装 → 循环 → **写之前再复核一次** → 落消息。

    前提：这一轮已被 :func:`claim_turn` 领走（状态 RUNNING）。
    """
    turn = await repo.get_turn(session, source_message_id)
    if turn is None or turn.status != BOT_RUNNING:
        return
    now = datetime.now(UTC)
    shop_id = int(turn.shop_id)
    ticket_no = turn.ticket_no

    # 用**买家视角**读会话：既拿到历史与单据上下文，也顺带证明"这条会话确实是他的"
    try:
        detail = await support_service.get_for_user(
            session,
            user_id=int(turn.buyer_user_id),
            ticket_no=ticket_no,
            before_id=None,
        )
    except BizError:
        await _settle_turn(session, turn, status=BOT_SKIPPED, error_code="TICKET_GONE", now=now)
        return

    if not any(
        m.sender_type == SUPPORT_SENDER_USER and rules.has_meaningful_text(m.body)
        for m in detail.messages
    ):
        # 只有图片 / 空正文：让 AI 说"我看不懂图"很蠢，而图像理解不在本期范围 ——
        # 不说话就等于交给人工（见下面 _assert_shopbot_allowed 的注释）
        await _settle_turn(session, turn, status=BOT_SKIPPED, error_code="NO_TEXT", now=now)
        return

    ticket = rules.ShopbotTicket(
        ticket_no=ticket_no,
        shop_id=shop_id,
        buyer_user_id=int(turn.buyer_user_id),
        subject=detail.subject,
        order_main_no=detail.context.order_main_no,
        order_sub_no=detail.context.order_sub_no,
        refund_no=detail.context.refund_no,
        spu_id=int(detail.context.spu_id) if detail.context.spu_id is not None else None,
    )

    try:
        await _assert_shopbot_allowed(
            session, shop_id=shop_id, buyer_user_id=int(turn.buyer_user_id)
        )
    except BizError as exc:
        # ★ 限流 / 额度用完 / 熔断 / 总开关关掉：**一个字都不说**，直接认输 ——
        #   而这恰恰等于交给人了：AI 没说话，最后一条还是买家的，商家队列
        #   （``OWES_REPLY_WHERE``）自然会收它。比"写一句我答不了"更好：
        #   不给买家噪音，也不白占商家的注意力。
        await _settle_turn(session, turn, status=BOT_SKIPPED, error_code=exc.code.name, now=now)
        return

    caller = rules.Caller(
        user_id=int(turn.buyer_user_id), role=ACTOR_BUYER, shop_id=shop_id
    )
    escalated = False
    try:
        answer, audit, prompt_t, completion_t, model, _degraded = await _run_loop(
            messages=await _shopbot_messages(session, detail=detail, ticket=ticket),
            specs=tools.tools_for(caller),
            tool_ctx=tools.ToolContext(session=session, caller=caller, ticket=ticket),
            llm=llm,
        )
        escalated = tools.was_escalated(audit)
    except LlmError as exc:
        # 上游的原始报文必须记下来（模型名写错 / 没开通 / 不接受 enable_thinking）
        logger.warning("店小蜜上游调用失败：%s", exc)
        await _note_upstream_result(ok=False)
        # ★ 这里**必须留一句话**：买家已经在等"客服"了，沉默比答错更糟。
        #   这句话由我们代说（模型没答上来），并顺手交给人工。
        answer, audit, prompt_t, completion_t, model = rules.SHOPBOT_FAILED_ANSWER, [], 0, 0, ""
        escalated = True

    await _note_upstream_result(ok=True)
    await _write_turn(
        session,
        turn=turn,
        ticket=ticket,
        answer=answer,
        audit=audit,
        escalated=escalated,
        prompt_tokens=prompt_t,
        completion_tokens=completion_t,
        model=model,
        now=now,
    )


async def _assert_shopbot_allowed(
    session: AsyncSession, *, shop_id: int, buyer_user_id: int
) -> None:
    """店小蜜的三道闸：总开关 → 熔断 → 限流（按 买家×店铺）→ 额度（按店铺）。

    ★ 限流**不是**按买家全站，而是按 ``(店铺, 买家)``：一个买家刷爆的应该是
      "他在这家店"的额度，而不是把他在别的店里的提问也一起掐掉。
    ★ 额度按**店铺**：买家的钱包不掏钱，成本单位是店铺（``usage_daily`` 的 SHOP 维度）。
    """
    settings = get_settings()
    if not (settings.assistant_enabled and settings.shopbot_enabled):
        raise BizError(ErrorCode.ASSISTANT_DISABLED)
    await _assert_breaker_closed()

    redis = get_redis()
    key = assistant_rate_shop_buyer(shop_id, buyer_user_id)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, RATE_WINDOW_SECONDS)
    if count > settings.shopbot_buyer_rate_per_minute:
        raise BizError(ErrorCode.ASSISTANT_RATE_LIMITED)

    used = await repo.get_usage_tokens(
        session,
        day=rules.day_key(datetime.now(UTC)),
        scope=USAGE_SCOPE_SHOP,
        scope_id=shop_id,
    )
    if used >= settings.assistant_daily_token_budget_shop:
        raise BizError(
            ErrorCode.ASSISTANT_QUOTA_EXCEEDED, "这家店今天的智能客服额度已用完"
        )


async def _shopbot_messages(
    session: AsyncSession, *, detail: Any, ticket: rules.ShopbotTicket
) -> list[dict[str, Any]]:
    """店小蜜的上下文：system（身份 + **这家店自己的话** + 平台买家规则 + 本会话）+ 历史。

    ★ 知识库取的是**买家受众**那一类。后台那几篇（怎么建仓、怎么配模板）**绝不能**
      混进来 —— 讲给买家听既听不懂也不该听到（见 knowledge.py 的受众说明）。
    """
    shop = await account_service.get_public_shop(session, ticket.shop_id)
    faq_rows = await repo.list_faq(session, ticket.shop_id, enabled_only=True)
    faq = "\n".join(f"Q：{row.question}\nA：{row.answer}" for row in faq_rows)
    system = rules.build_shopbot_prompt(
        shop_name=shop.name,
        faq=faq,
        knowledge=knowledge_mod.load(knowledge_mod.BUYER_AUDIENCES),
        ticket=ticket,
    )
    turns = [rules.shopbot_turn(int(m.sender_type), m.body or "") for m in detail.messages]
    return [{"role": "system", "content": system}, *rules.trim_history(turns)]


async def _write_turn(
    session: AsyncSession,
    *,
    turn: BotTurn,
    ticket: rules.ShopbotTicket,
    answer: str,
    audit: list[dict[str, Any]],
    escalated: bool,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    now: datetime,
) -> None:
    """**写之前**重读复核，然后才开口 —— 抢话守卫就在这里。

    ★ 顺序必须是"**锁住行 → 重新判断 → 写**"，而且三件事在同一个事务里：模型跑了好几秒，
      期间商家完全可能已经回了话。守卫漏掉的后果不是"多一句话"，是
      **AI 的话盖掉真人的话、并把会话重新挤出商家的「待回复」队列**。
      （只看扫描时的状态是不够的，所以这里再读一次、并且连开关也再确认一次。）
    """
    live = await support_service.get_ticket_state(
        session, shop_id=ticket.shop_id, ticket_no=ticket.ticket_no, lock=True
    )
    ai_enabled = await repo.is_ai_enabled(session, ticket.shop_id)
    if live is None or not rules.shopbot_should_answer(
        status=live.status,
        last_sender_type=live.last_sender_type,
        need_human=live.need_human,
        ai_enabled=ai_enabled,
    ):
        await _settle_turn(
            session, turn, status=BOT_SKIPPED, error_code="STALE", now=now
        )
        return

    body = rules.clip_text(answer, rules.SHOPBOT_MAX_CHARS)
    await support_service.reply_as_ai(
        session,
        get_redis(),
        shop_id=ticket.shop_id,
        ticket_no=ticket.ticket_no,
        body=body,
    )
    if escalated:
        # 交给真人：与买家点「转人工」共用同一个闩锁（谁先说都算数，客服回复时一起解开）
        await support_service.flag_need_human(
            session, shop_id=ticket.shop_id, ticket_no=ticket.ticket_no
        )
    await _settle_turn(
        session,
        turn,
        status=BOT_DONE,
        content=body,
        audit=audit,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
        now=now,
    )
    # 记账：**只按店铺**。买家的钱包不掏钱，成本单位是店铺
    await repo.add_usage(
        session,
        day=rules.day_key(now),
        scope=USAGE_SCOPE_SHOP,
        scope_id=ticket.shop_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


async def _settle_turn(
    session: AsyncSession,
    turn: BotTurn,
    *,
    status: int,
    now: datetime,
    content: str | None = None,
    error_code: str | None = None,
    audit: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    model: str = "",
) -> None:
    """把台账写成终态。**所有出口都过这里** —— 否则会出现"永远停在 RUNNING"。"""
    turn.status = status
    turn.content = content
    turn.error_code = error_code
    turn.tool_calls = audit or None
    turn.llm_model = model or None
    turn.prompt_tokens = prompt_tokens
    turn.completion_tokens = completion_tokens
    turn.updated_at = now
    await session.flush()


async def reap_stuck_turns(session: AsyncSession) -> tuple[list[int], int]:
    """兜底收尾。返回 ``(要重新投递的 source_message_id, 判失败的条数)``。

    ★ 两种卡法**处理方式刻意不同**：
      - ``PENDING`` 卡住 = 入队那一步没成（进程崩 / Redis 抖），**模型从没被调用过**
        → 重新投递是安全的，也是唯一正确的（否则买家的消息永远没人答）；
      - ``RUNNING`` 卡住 = 模型可能已经花过钱、甚至答了一半 → **判失败并交给人工**，
        绝不重跑（与后台助手的 ``reap_stuck`` 同一条理由）。
        而"交给人工"在这里**不需要额外动作**：AI 一个字都没落库，最后一条还是买家的，
        商家队列自然会收它。
    """
    now = datetime.now(UTC)
    stuck = await repo.list_stuck_turns(
        session,
        pending_before=now - timedelta(seconds=TURN_PENDING_RESEND_SECONDS),
        running_before=now - timedelta(seconds=STUCK_SECONDS),
    )
    resend: list[int] = []
    failed = 0
    for turn in stuck:
        if turn.status == BOT_PENDING:
            # ★ 不改状态、也不删行：重投递之后 worker 拿到它照样能 claim
            resend.append(int(turn.source_message_id))
            continue
        await _settle_turn(session, turn, status=BOT_FAILED, error_code="STUCK", now=now)
        failed += 1
    if stuck:
        logger.warning(
            "店小蜜有卡住的轮次被收尾", extra={"resend": len(resend), "failed": failed}
        )
    return resend, failed


# ------------------------------------------------------------------
# 商户侧：开关 / 问答 / 今日计数（都是**本店**范围，shop_id 取自 caller）
# ------------------------------------------------------------------
async def get_shop_setting(session: AsyncSession, *, caller: rules.Caller) -> ShopSettingOut:
    shop_id = caller.require_shop()
    setting = await repo.get_shop_setting(session, shop_id)
    # 没有那一行 = 没开过 = 关（不预先建行：默认值属于代码，不属于数据）
    return ShopSettingOut(ai_enabled=bool(setting.ai_enabled) if setting else False)


async def set_shop_setting(
    session: AsyncSession, *, caller: rules.Caller, ai_enabled: bool
) -> ShopSettingOut:
    """开/关本店的智能客服。★ **商户自己开** —— 平台不替他们决定让 AI 对买家说话。"""
    shop_id = caller.require_shop()
    await repo.upsert_shop_setting(
        session, shop_id=shop_id, ai_enabled=ai_enabled, now=datetime.now(UTC)
    )
    return ShopSettingOut(ai_enabled=ai_enabled)


async def list_faq(session: AsyncSession, *, caller: rules.Caller) -> list[ShopFaqOut]:
    rows = await repo.list_faq(session, caller.require_shop())
    return [ShopFaqOut.of(row) for row in rows]


async def create_faq(
    session: AsyncSession, *, caller: rules.Caller, req: ShopFaqIn
) -> ShopFaqOut:
    shop_id = caller.require_shop()
    now = datetime.now(UTC)
    faq = ShopFaq(
        id=next_id(),
        shop_id=shop_id,
        question=req.question.strip(),
        answer=req.answer.strip(),
        enabled=req.enabled,
        created_at=now,
        updated_at=now,
    )
    await repo.insert_faq(session, faq)
    await session.flush()
    return ShopFaqOut.of(faq)


async def update_faq(
    session: AsyncSession, *, caller: rules.Caller, faq_id: int, req: ShopFaqIn
) -> ShopFaqOut:
    """改一条。**归属不对就是"不存在"**（404）—— 与全项目的归属校验一致。"""
    faq = await repo.get_faq(session, shop_id=caller.require_shop(), faq_id=faq_id)
    if faq is None:
        raise BizError(ErrorCode.NOT_FOUND, "这条问答不存在")
    faq.question = req.question.strip()
    faq.answer = req.answer.strip()
    faq.enabled = req.enabled
    faq.updated_at = datetime.now(UTC)
    await session.flush()
    return ShopFaqOut.of(faq)


async def delete_faq(session: AsyncSession, *, caller: rules.Caller, faq_id: int) -> None:
    faq = await repo.get_faq(session, shop_id=caller.require_shop(), faq_id=faq_id)
    if faq is None:
        raise BizError(ErrorCode.NOT_FOUND, "这条问答不存在")
    await repo.delete_faq(session, faq)


async def shopbot_stats(session: AsyncSession, *, caller: rules.Caller) -> ShopBotStatsOut:
    """今日：AI 答了几条、没答几条、花了多少 token。

    ★ 给商户一个"它在替我说话、花我的钱"的可见处 —— 没有这个页面，
      开关打开之后就是黑箱（而成本是按**店铺**记的）。
    """
    shop_id = caller.require_shop()
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = await repo.count_turns_by_status(session, shop_id=shop_id, since=day_start)
    answered = counts.get(BOT_DONE, 0)
    not_answered = counts.get(BOT_SKIPPED, 0) + counts.get(BOT_FAILED, 0)
    return ShopBotStatsOut(
        answered=answered,
        not_answered=not_answered,
        pending=counts.get(BOT_PENDING, 0) + counts.get(BOT_RUNNING, 0),
        tokens_today=await repo.get_usage_tokens(
            session,
            day=rules.day_key(datetime.now(UTC)),
            scope=USAGE_SCOPE_SHOP,
            scope_id=shop_id,
        ),
    )

