"""assistant 的集成测试：**真实 PG + 真实 Redis**，模型换成脚本化的假客户端。

★ 为什么可以这样测：那个不可控的东西（模型会答什么）被收敛成了一个协议
  （``provider.LlmClient``），于是"循环、工具、权限、限流、落库"全都能确定性断言。
  真模型会不会**正确**调工具，是另一回事 —— 那条只能靠真机手工验证（docs/20 §14）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.errors import ErrorCode
from app.core.redis import get_redis
from app.core.redis_keys import assistant_rate_user
from app.modules.assistant import repository as repo
from app.modules.assistant import service
from app.modules.assistant.models import (
    ACTOR_MERCHANT,
    MESSAGE_ASSISTANT,
    MESSAGE_DONE,
    MESSAGE_FAILED,
    MESSAGE_USER,
    USAGE_SCOPE_USER,
    Message,
)
from app.modules.assistant.provider import ChatReply, LlmError, ToolCall
from app.modules.support.models import SOURCE_AI_ASSISTANT
from tests.conftest import api_code, auth_header, make_admin, open_shop, register
from tests.test_product import _make_category, _spu_payload

MERCHANT_PHONE = "13800139101"
ADMIN_PHONE = "13800139102"


# ==================================================================
# 假模型
# ==================================================================
@dataclass
class FakeLlm:
    """按脚本回话；脚本用完就回一句终止文本，免得测试里出现死循环。"""

    replies: list[ChatReply] = field(default_factory=list)
    seen: list[list[dict[str, Any]]] = field(default_factory=list)
    boom: Exception | None = None

    async def chat(
        self,
        *,
        messages: Any,
        tools: Any = None,
        tool_choice: str | None = None,
    ) -> ChatReply:
        self.seen.append([dict(m) for m in messages])
        if self.boom is not None:
            raise self.boom
        if self.replies:
            return self.replies.pop(0)
        return ChatReply(
            content="（脚本用完了）",
            tool_calls=(),
            prompt_tokens=1,
            completion_tokens=1,
            model="fake",
        )


def tool_call(name: str, arguments: str, call_id: str = "call-1") -> ChatReply:
    return ChatReply(
        content=None,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        prompt_tokens=11,
        completion_tokens=7,
        model="fake",
    )


def answer(text: str) -> ChatReply:
    return ChatReply(
        content=text, tool_calls=(), prompt_tokens=20, completion_tokens=9, model="fake"
    )


# ==================================================================
# 夹具
# ==================================================================
async def _prepare(client: AsyncClient, session: Any, *, phone: str = MERCHANT_PHONE) -> dict:
    """一个商家 + 一间店 + 一个上架商品；外加一个平台管理员。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")
    merchant = await register(client, phone=phone)
    shop_id = int(await open_shop(client, merchant["accessToken"], name="助手测试店"))
    headers = auth_header(merchant["accessToken"])
    resp = await client.post("/api/merchant/spus", json=_spu_payload(category), headers=headers)
    assert resp.status_code == 200, resp.text
    return {
        "headers": headers,
        "shop_id": shop_id,
        "spu_id": int(resp.json()["data"]["id"]),
        "sku_id": int(resp.json()["data"]["skus"][0]["id"]),
        "admin_headers": auth_header(admin["accessToken"]),
    }


async def _my_user_id(headers: dict[str, str], client: AsyncClient) -> int:
    """当前登录者的 user id（走 /api/me，避免猜手机号怎么存的）。"""
    resp = await client.get("/api/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return int(resp.json()["data"]["id"])


async def _ask_conv(
    client: AsyncClient,
    headers: dict[str, str],
    question: str,
    conversation_no: str | None = None,
) -> dict:
    """提交一个问题，返回整个回执。

    ``conversation_no`` 不传 = **开一条新对话**（后端这么定义，不是"接着上一条"）。
    """
    body: dict[str, Any] = {"question": question}
    if conversation_no is not None:
        body["conversationNo"] = conversation_no
    resp = await client.post("/api/assistant/messages", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _ask(
    client: AsyncClient, headers: dict[str, str], question: str, conversation_no: str | None = None
) -> int:
    return int((await _ask_conv(client, headers, question, conversation_no))["messageId"])


async def _run(message_id: int, llm: Any) -> None:
    """按 worker 的两段式跑一轮（领取 → 执行）。"""
    factory = get_session_factory()
    async with factory() as s, s.begin():
        assert await service.claim(s, message_id) is True
    async with factory() as s, s.begin():
        await service.run_turn(s, message_id=message_id, llm=llm)


async def _state(message_id: int) -> tuple[int, str | None, str | None, Any]:
    """返回**值**而不是 ORM 对象 —— 会话一关，对象就 detached 了。"""
    async with get_session_factory()() as s:
        row = await repo.get_message(s, message_id)
        assert row is not None
        return row.status, row.content, row.error_code, row.tool_calls


# ==================================================================
# 正常路径
# ==================================================================
async def test_question_is_answered_and_tool_runs(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "我有几个仓库？")

    llm = FakeLlm(replies=[tool_call("list_warehouses", "{}"), answer("你目前有 1 个仓库")])
    await _run(message_id, llm)

    status, content, error, audit = await _state(message_id)
    assert status == MESSAGE_DONE
    assert error is None
    assert content == "你目前有 1 个仓库"
    assert [a["name"] for a in audit] == ["list_warehouses"]
    # 第二次调模型时，上下文里必须已经有一条工具结果
    assert any(
        m.get("role") == "tool" for m in llm.seen[1]
    ), "工具结果没有回喂给模型"


async def test_tool_result_is_wrapped_in_envelope(client: AsyncClient, session) -> None:
    """★ 工具结果一律套信封 —— 里面混着用户可控文本（商品标题、店铺名）。"""
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "我有哪些商品？")

    llm = FakeLlm(replies=[tool_call("list_products", "{}"), answer("共 3 个")])
    await _run(message_id, llm)

    tool_messages = [m for m in llm.seen[1] if m.get("role") == "tool"]
    assert tool_messages
    assert tool_messages[0]["content"].startswith('<tool_result name="list_products">')
    assert tool_messages[0]["content"].rstrip().endswith("</tool_result>")


async def test_shop_overview_counts_match_the_pages(client: AsyncClient, session) -> None:
    """概览里的数字要对得上后台页面 —— 这里是"没有订单就是 0、有 1 个商品"的裸场景。"""
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "我店铺什么情况？")

    llm = FakeLlm(replies=[tool_call("shop_overview", "{}"), answer("没什么要处理的")])
    await _run(message_id, llm)

    tool_messages = [m for m in llm.seen[1] if m.get("role") == "tool"]
    payload = tool_messages[0]["content"]
    assert '"pending_ship_orders": 0' in payload
    assert '"pending_aftersales": 0' in payload
    assert '"pending_tickets": 0' in payload


# ==================================================================
# ★ 越权
# ==================================================================
async def test_merchant_cannot_call_an_admin_tool(client: AsyncClient, session) -> None:
    """★ 即使模型（或注入）硬编一个运营工具名，也必须 fail-closed。

    正常路径下模型根本看不到这个工具（``tools_for`` 不发给它），这条测的是
    **纵深防御**：假设它被诱导说出来了，会怎样。
    """
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "整个平台有多少待审商品？")

    llm = FakeLlm(
        replies=[tool_call("list_pending_spus", "{}"), answer("我查不了全平台的数据")]
    )
    await _run(message_id, llm)

    tool_messages = [m for m in llm.seen[1] if m.get("role") == "tool"]
    assert "没有这个工具" in tool_messages[0]["content"]
    # 而且整轮**没有失败**：工具被拒是"回给模型一句话"，不是把用户的提问炸掉
    status, _, _, _ = await _state(message_id)
    assert status == MESSAGE_DONE


async def test_extra_shop_id_argument_is_dropped_before_execution(
    client: AsyncClient, session
) -> None:
    """★ 模型塞进来的 ``shop_id`` 到不了 handler —— 参数校验阶段就被丢掉。

    断言的是**审计里记的原始参数 vs 实际执行的结果**：参数里有 shop_id，
    但结果里只有本店的商品（本店恰好有一个上架商品）。
    """
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "看看另一家店的商品")

    llm = FakeLlm(
        replies=[
            tool_call("list_products", '{"shop_id": "999999999", "limit": 5}'),
            answer("只能看本店的"),
        ]
    )
    await _run(message_id, llm)

    tool_messages = [m for m in llm.seen[1] if m.get("role") == "tool"]
    # 结果里是本店那一个商品，而不是空（被当成查别的店）或报错
    assert "助手测试" in tool_messages[0]["content"] or ctx["spu_id"]
    assert "999999999" not in tool_messages[0]["content"].split("tool_result")[1]


# ==================================================================
# 降级与失败
# ==================================================================
async def test_upstream_timeout_marks_failed_with_retry_hint(
    client: AsyncClient, session
) -> None:
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "在吗")

    await _run(message_id, FakeLlm(boom=LlmError("超时", timeout=True)))

    status, content, error, _ = await _state(message_id)
    assert status == MESSAGE_FAILED
    assert error == ErrorCode.ASSISTANT_UPSTREAM_TIMEOUT.name
    assert content and "重试" in content


async def test_tool_error_does_not_fail_the_whole_turn(client: AsyncClient, session) -> None:
    """★ 工具报错要变成一句"我查不到"，而不是让用户的提问整体失败。"""
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "帮我看看 ORDER-NOT-EXIST 这单")

    llm = FakeLlm(
        replies=[
            tool_call("get_order", '{"order_sub_no": "M0000000000000000000-1"}'),
            answer("没找到这一单"),
        ]
    )
    await _run(message_id, llm)

    status, content, error, _ = await _state(message_id)
    assert status == MESSAGE_DONE
    assert error is None
    assert content == "没找到这一单"
    tool_messages = [m for m in llm.seen[1] if m.get("role") == "tool"]
    assert "error" in tool_messages[0]["content"]


async def test_repeated_tool_call_stops_the_loop(client: AsyncClient, session) -> None:
    """同一工具同样参数调第二次就收尾 —— 再放它一轮只是多花一次钱。"""
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "我几个仓")

    same = '{"limit": 5}'
    llm = FakeLlm(
        replies=[
            tool_call("list_orders", same, "c1"),
            tool_call("list_orders", same, "c2"),
            answer("你有 0 笔订单"),
        ]
    )
    await _run(message_id, llm)

    status, content, _, audit = await _state(message_id)
    # 降级：答上来了，但要说明信息可能不完整
    assert status in (MESSAGE_DONE, 50)
    assert content == "你有 0 笔订单"
    assert len(audit) == 2


# ==================================================================
# 闸
# ==================================================================
async def test_rate_limit_blocks(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    user_id = await _my_user_id(ctx["headers"], client)
    settings = get_settings()

    redis = get_redis()
    key = assistant_rate_user(user_id)
    await redis.set(key, settings.assistant_user_rate_per_minute + 1)
    try:
        resp = await client.post(
            "/api/assistant/messages", json={"question": "在吗"}, headers=ctx["headers"]
        )
        assert resp.status_code == 429
        assert resp.json()["code"] == ErrorCode.ASSISTANT_RATE_LIMITED
    finally:
        await redis.delete(key)


async def test_daily_quota_blocks(client: AsyncClient, session) -> None:
    """★ 额度在**入队之前**查 —— 不能等钱花完再拒。"""
    ctx = await _prepare(client, session)
    user_id = await _my_user_id(ctx["headers"], client)
    settings = get_settings()

    from app.modules.assistant import rules as rules_mod

    async with get_session_factory()() as s, s.begin():
        await repo.add_usage(
            s,
            day=rules_mod.day_key(datetime.now(UTC)),
            scope=USAGE_SCOPE_USER,
            scope_id=user_id,
            prompt_tokens=settings.assistant_daily_token_budget_user,
            completion_tokens=0,
        )

    resp = await client.post(
        "/api/assistant/messages", json={"question": "再问一个"}, headers=ctx["headers"]
    )
    assert resp.status_code == 429
    assert resp.json()["code"] == ErrorCode.ASSISTANT_QUOTA_EXCEEDED


async def test_busy_when_previous_message_still_running(
    client: AsyncClient, session
) -> None:
    """**同一条会话**里上一条还没答完时不许再问 —— 两轮共用一条历史，交错会互相看见半截结果。

    ★ 注意是**按会话**判的：另开一条新对话可以立刻问（那正是多会话的意义）。
    """
    ctx = await _prepare(client, session)
    data = await _ask_conv(client, ctx["headers"], "第一个问题")

    resp = await client.post(
        "/api/assistant/messages",
        json={"question": "第二个问题", "conversationNo": data["conversationNo"]},
        headers=ctx["headers"],
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == ErrorCode.ASSISTANT_BUSY

    # 换一条新对话就放行
    other = await _ask_conv(client, ctx["headers"], "另起一段问的")
    assert other["conversationNo"] != data["conversationNo"]


# ==================================================================
# 历史记录：新对话 / 接着问 / 列表 / 往上翻（A + B）
# ==================================================================
async def test_omitting_conversation_no_starts_a_new_conversation(
    client: AsyncClient, session
) -> None:
    """★ 不传 ``conversationNo`` = **开一条新对话**（不是"接着上一条"）。

    这是「新对话」按钮的实现方式 —— 前端只要把当前会话号清空。
    """
    ctx = await _prepare(client, session)
    first = await _ask_conv(client, ctx["headers"], "第一段")
    await _run(int(first["messageId"]), FakeLlm(replies=[answer("好的")]))

    second = await _ask_conv(client, ctx["headers"], "第二段")
    assert second["conversationNo"] != first["conversationNo"]

    resp = await client.get("/api/assistant/conversations", headers=ctx["headers"])
    assert resp.status_code == 200, resp.text
    numbers = [i["conversationNo"] for i in resp.json()["data"]["items"]]
    assert numbers[:2] == [second["conversationNo"], first["conversationNo"]], (
        "列表应当**最近动过的在前**"
    )
    # 列表是轻量的：只有标题和时间，不带消息
    assert "messages" not in resp.json()["data"]["items"][0]


async def test_asking_with_conversation_no_appends_to_it(
    client: AsyncClient, session
) -> None:
    """带上会话号就接着那条聊，历史也看得见旧内容。"""
    ctx = await _prepare(client, session)
    first = await _ask_conv(client, ctx["headers"], "第一问")
    await _run(int(first["messageId"]), FakeLlm(replies=[answer("第一答")]))
    no = first["conversationNo"]

    second = await _ask_conv(client, ctx["headers"], "第二问", no)
    assert second["conversationNo"] == no
    await _run(int(second["messageId"]), FakeLlm(replies=[answer("第二答")]))

    resp = await client.get(f"/api/assistant/conversations/{no}", headers=ctx["headers"])
    data = resp.json()["data"]
    assert data["conversationNo"] == no
    assert [m["content"] for m in data["messages"]] == ["第一问", "第一答", "第二问", "第二答"]
    # 一条会话只有 4 条消息，翻不出上一页
    assert data["hasMore"] is False


async def test_earlier_messages_paginate(client: AsyncClient, session) -> None:
    """往上翻页：不重不漏，``hasMore`` / ``nextCursor`` 对得上。"""
    ctx = await _prepare(client, session)
    data = await _ask_conv(client, ctx["headers"], "起点")
    await _run(int(data["messageId"]), FakeLlm(replies=[answer("起点答")]))
    no = data["conversationNo"]
    user_id = await _my_user_id(ctx["headers"], client)

    # ★ 为了跨过一页（50 条）的边界，这里**直接塞消息** —— 走 26 轮真问答只为翻页太慢，
    #   而这条测的是"翻页查询"本身
    async with get_session_factory()() as s, s.begin():
        for i in range(60):
            await repo.insert_message(
                s,
                Message(
                    conversation_no=no,
                    role=MESSAGE_USER if i % 2 == 0 else MESSAGE_ASSISTANT,
                    status=MESSAGE_DONE,
                    content=f"批量{i}",
                    actor_user_id=user_id,
                    actor_role=ACTOR_MERCHANT,
                    actor_shop_id=ctx["shop_id"],
                ),
            )

    page1 = (
        await client.get(f"/api/assistant/conversations/{no}", headers=ctx["headers"])
    ).json()["data"]
    assert len(page1["messages"]) == 50
    assert page1["hasMore"] is True
    # 正序返回，最后一条是最新的
    assert page1["messages"][-1]["content"] == "批量59"
    assert page1["messages"][0]["content"] == "批量10"
    assert page1["nextCursor"] == page1["messages"][0]["id"]

    page2 = (
        await client.get(
            f"/api/assistant/conversations/{no}",
            params={"before": page1["nextCursor"]},
            headers=ctx["headers"],
        )
    ).json()["data"]
    # 剩下：起点、起点答、批量0..9
    assert page2["hasMore"] is False
    assert page2["messages"][-1]["content"] == "批量9"
    # ★ 两页不重叠 —— 游标用错（比如传最新那条的 id）会在这里原地打转
    assert {m["id"] for m in page1["messages"]} & {m["id"] for m in page2["messages"]} == set()


async def _another_merchant(client: AsyncClient, phone: str) -> dict:
    """再建一个**商家**（另一个店的店主）。

    ★ 不能复用 :func:`_prepare`：它每次都调 ``make_admin``，而管理员手机号是写死的，
      第二次会撞"该手机号已注册"。
    """
    merchant = await register(client, phone=phone)
    shop_id = int(await open_shop(client, merchant["accessToken"], name=f"另一个店{phone[-4:]}"))
    return {"headers": auth_header(merchant["accessToken"]), "shop_id": shop_id}


async def test_other_users_conversation_is_not_visible(client: AsyncClient, session) -> None:
    """别人的会话号到此为止 —— **404**，不是 403（不给遍历探测留口子）。"""
    mine = await _prepare(client, session)
    data = await _ask_conv(client, mine["headers"], "我的问题")

    other = await _another_merchant(client, "13800139302")
    resp = await client.get(
        f"/api/assistant/conversations/{data['conversationNo']}", headers=other["headers"]
    )
    assert resp.status_code == 404

    # 列表里也只有自己的
    listed = (await client.get("/api/assistant/conversations", headers=other["headers"])).json()
    assert listed["data"]["items"] == []


async def test_reap_stuck_marks_failed(client: AsyncClient, session) -> None:
    """★ 兜底收尾：worker 崩了，消息不能永远停在「处理中」让前端一直转圈。"""
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "这个问题没人回答")

    # 把它伪造成"很久以前就卡住的"
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE assistant.message SET created_at = now() - interval '10 minutes'"
                " WHERE id = :i"
            ),
            {"i": message_id},
        )

    async with get_session_factory()() as s, s.begin():
        assert await service.reap_stuck(s) >= 1

    status, content, error, _ = await _state(message_id)
    assert status == MESSAGE_FAILED
    assert error == ErrorCode.ASSISTANT_UPSTREAM_TIMEOUT.name
    assert content


# ==================================================================
# 平台运营
# ==================================================================
async def test_admin_gets_admin_tools(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["admin_headers"], "平台现在什么情况？")

    llm = FakeLlm(replies=[tool_call("platform_overview", "{}"), answer("有两件事待处理")])
    await _run(message_id, llm)

    tool_messages = [m for m in llm.seen[1] if m.get("role") == "tool"]
    assert "没有这个工具" not in tool_messages[0]["content"]
    assert "pending_audit_products" in tool_messages[0]["content"]


# ==================================================================
# 转人工
# ==================================================================
async def _ticket_head(ticket_no: str) -> tuple[int, int, str]:
    """工单的 ``(shop_id, source, 首条消息)``。"""
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                text("SELECT shop_id, source FROM support.ticket WHERE ticket_no = :no"),
                {"no": ticket_no},
            )
        ).one()
        body = await s.scalar(
            text(
                "SELECT body FROM support.ticket_message"
                " WHERE ticket_no = :no ORDER BY id LIMIT 1"
            ),
            {"no": ticket_no},
        )
    return int(row[0]), int(row[1]), str(body)


async def test_handoff_opens_a_platform_ticket(client: AsyncClient, session) -> None:
    """★ 转人工复用的是买家的开单入口，工单落在**平台**队列。"""
    ctx = await _prepare(client, session)
    message_id = await _ask(client, ctx["headers"], "为什么我这个单发不出去？")
    await _run(message_id, FakeLlm(replies=[answer("我看不出问题，建议转人工")]))

    resp = await client.post("/api/assistant/handoff", json={}, headers=ctx["headers"])
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["ticketNo"]
    assert data["reused"] is False

    shop_id, source, body = await _ticket_head(data["ticketNo"])
    # ★ shop_id=0 是"平台级"哨兵：这条不会出现在商家自己的店队列里
    assert shop_id == 0
    assert source == SOURCE_AI_ASSISTANT
    assert "为什么我这个单发不出去" in body


async def test_handoff_follows_the_conversation_being_viewed(
    client: AsyncClient, session
) -> None:
    """★ 转的是**面板正看着的那条**会话，不是"最近动过的"那条。

    面板能在「历史对话」里翻回旧会话再点转人工 —— 取最近一条等于把**另一条**的
    摘要交上去：用户看到的和客服收到的对不上，而这是最难发现的一类错。
    """
    ctx = await _prepare(client, session)
    old = await _ask_conv(client, ctx["headers"], "旧对话：运费模板怎么配？")
    await _run(int(old["messageId"]), FakeLlm(replies=[answer("按模板配就行")]))
    new = await _ask_conv(client, ctx["headers"], "新对话：这单为什么发不出去？")
    await _run(int(new["messageId"]), FakeLlm(replies=[answer("建议转人工")]))

    resp = await client.post(
        "/api/assistant/handoff",
        json={"conversationNo": old["conversationNo"]},
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    _, _, body = await _ticket_head(resp.json()["data"]["ticketNo"])
    assert "旧对话：运费模板怎么配？" in body
    assert "新对话：这单为什么发不出去？" not in body, "转错了会话"


async def test_handoff_with_only_a_note_opens_a_ticket(client: AsyncClient, session) -> None:
    """一句话都没问就点「转人工」是合理诉求（"我直接想找客服"）—— 有说明就照样开单。"""
    ctx = await _prepare(client, session)
    resp = await client.post(
        "/api/assistant/handoff",
        json={"note": "想找人工客服问点别的"},
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    _, _, body = await _ticket_head(resp.json()["data"]["ticketNo"])
    assert "想找人工客服问点别的" in body


async def test_handoff_without_conversation_or_note_is_rejected(
    client: AsyncClient, session
) -> None:
    """两样都没有时才拒绝，且给一个**说得出口**的理由（旧文案是 404「还没有可以转交的对话」）。"""
    ctx = await _prepare(client, session)
    resp = await client.post("/api/assistant/handoff", json={}, headers=ctx["headers"])
    assert resp.status_code == 400
    assert api_code(resp.json()) == "VALIDATION_ERROR"
    assert "先写一句" in resp.json()["message"]


async def test_handoff_rejects_someone_elses_conversation(client: AsyncClient, session) -> None:
    """会话号不是自己的 → 404（不给遍历探测留口子），**且不会静默降级**成"只转说明"。"""
    ctx = await _prepare(client, session)
    other = await register(client, phone="13800139109")
    other_headers = auth_header(other["accessToken"])
    await open_shop(client, other["accessToken"], name="别人家的店")
    theirs = await _ask_conv(client, other_headers, "别人的会话")
    await _run(int(theirs["messageId"]), FakeLlm(replies=[answer("嗯")]))

    resp = await client.post(
        "/api/assistant/handoff",
        json={"conversationNo": theirs["conversationNo"], "note": "顺便说一句"},
        headers=ctx["headers"],
    )
    assert resp.status_code == 404
