"""assistant 的 ARQ 任务。

**后台助手**两个任务，职责刻意分开：

- ``run_assistant_turn`` —— 跑一轮问答。**两个事务**：先"领取"（PENDING→RUNNING）
  并提交，再真正跑。分开的理由见 ``service.claim`` 的 docstring：合并成一个事务的话，
  模型调用一失败会把"已开始"的标记一起回滚，重试就会**再花一次钱**。
- ``reap_stuck_messages`` —— 兜底收尾。worker 崩了、队列丢了，那些消息会永远停在
  「处理中」让前端一直转圈。与 ``outbox`` 的兜底扫描是同一个思路。

**店小蜜**（买家侧）三个任务，是同一套形状的第二次使用：

- ``sweep_ticket_turns`` —— 每 3 秒扫一遍"买家刚说完话、还没人接"的会话，
  给该 AI 答的那些排一个 PENDING 台账并入队（``service.sweep_ticket_turns``）。
- ``run_bot_turn`` —— 跑一轮（同样两个事务）。
- ``reap_stuck_turns`` —— 兜底：PENDING 卡住的**重投递**、RUNNING 卡住的判失败。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.modules.assistant import service
from app.modules.assistant.provider import QwenClient
from app.worker.enqueue import enqueue_now

logger = get_logger(__name__)


async def run_assistant_turn(ctx: dict[str, Any], message_id: int) -> None:
    """把一条待答的消息跑到终态。

    ★ **不再自动重试**：消息一旦被领取就停在 RUNNING，重投递会直接返回
      （``claim`` 只认 PENDING）。LLM 调用重放会双倍花钱、还可能重复作答，
      所以"失败就重试"在这里是错的默认值。
    """
    factory = ctx["session_factory"]

    async with factory() as session, session.begin():
        if not await service.claim(session, int(message_id)):
            return

    async with factory() as session, session.begin():
        await service.run_turn(session, message_id=int(message_id), llm=QwenClient())


async def reap_stuck_messages(ctx: dict[str, Any]) -> int:
    """把卡住的消息标成失败。每分钟一次。"""
    factory = ctx["session_factory"]
    async with factory() as session, session.begin():
        return await service.reap_stuck(session)


# ==================================================================
# 店小蜜（买家侧的智能客服，docs/20 §14）
# ==================================================================
async def run_bot_turn(ctx: dict[str, Any], source_message_id: int) -> None:
    """跑一轮店小蜜。**两个事务**，与 ``run_assistant_turn`` 同一个理由：
    先"领取"并提交，模型调用失败才不会把"已开始"一起回滚（重试会再花一次钱）。"""
    factory = ctx["session_factory"]
    async with factory() as session, session.begin():
        if not await service.claim_turn(session, int(source_message_id)):
            return
    async with factory() as session, session.begin():
        await service.answer_ticket(
            session, source_message_id=int(source_message_id), llm=QwenClient()
        )


async def sweep_ticket_turns(ctx: dict[str, Any]) -> int:
    """扫一遍"在等答复"的会话，给该答的那些排上活儿再投递。每 3 秒一次。

    ★ 扫描与投递**分开两个事务**：扫描先提交，投递在事务外做。投递失败**不回滚**
      那些 PENDING 行 —— 它们留在库里，由 ``reap_stuck_turns`` 重投。
      （反过来做的话，进程崩在"投递前"就会丢掉整批活儿。）
    """
    factory = ctx["session_factory"]
    async with factory() as session, session.begin():
        pending = await service.sweep_ticket_turns(session)
    for source_message_id in pending:
        await enqueue_now(service.JOB_RUN_BOT_TURN, source_message_id)
    return len(pending)


async def reap_stuck_turns(ctx: dict[str, Any]) -> int:
    """兜底收尾：``PENDING`` 卡住的**重新投递**、``RUNNING`` 卡住的判失败。每分钟一次。"""
    factory = ctx["session_factory"]
    async with factory() as session, session.begin():
        resend, failed = await service.reap_stuck_turns(session)
    for source_message_id in resend:
        await enqueue_now(service.JOB_RUN_BOT_TURN, source_message_id)
    return len(resend) + failed
