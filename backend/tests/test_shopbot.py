"""店小蜜（买家侧的智能客服）的集成测试：**真实 PG + 真实 Redis**，模型是脚本化的假客户端。

钉住的都是"很难靠读代码发现、但线上一定会发生"的几件事：

1. **幂等**：扫描每 3 秒一轮，同一条买家消息只能答一次。
2. **抢话**：商家在 AI 飞行的这几秒里回了话 → AI 必须闭嘴，**绝不能盖掉真人的话**。
3. **开关**：关掉之后排队中的那一轮也得闭嘴（只在扫描时看开关是不够的）。
4. **答不了要交人**：模型自己认输、或上游挂了 → 转人工；而"一个字都不说"的那种
   认输（额度/限流/熔断）**不需要**置闩锁 —— 最后一条还是买家的，商家队列自然会收。
5. **平台票不答**：平台级会话没有店铺目录可查，AI 不参与。
6. 只发图片的消息不硬答。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.errors import BizError
from app.core.redis import get_redis
from app.core.redis_keys import assistant_rate_shop_buyer
from app.modules.assistant import repository as repo
from app.modules.assistant import rules as shopbot_rules
from app.modules.assistant import service as shopbot
from app.modules.assistant import tools
from app.modules.assistant.models import (
    ACTOR_BUYER,
    BOT_DONE,
    BOT_FAILED,
    BOT_SKIPPED,
    USAGE_SCOPE_SHOP,
)
from app.modules.assistant.provider import LlmError
from tests.conftest import auth_header, make_admin, open_shop, register
from tests.test_assistant import FakeLlm, answer, tool_call
from tests.test_product import _make_category, _spu_payload
from tests.test_trade import _create_order
from tests.test_trade import _prepare as _prepare_trade

MERCHANT_PHONE = "13800139201"
BUYER_PHONE = "13800139202"


# ==================================================================
# 夹具
# ==================================================================
async def _prepare(
    client: AsyncClient, session: Any, *, ai_enabled: bool = True, with_spu: bool = True
) -> dict:
    """一个开了智能客服的店 + 一个买家 + 一条"买家说过话"的店铺会话。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")
    merchant = await register(client, phone=MERCHANT_PHONE)
    shop_id = int(await open_shop(client, merchant["accessToken"], name="店小蜜测试店"))
    merchant_headers = auth_header(merchant["accessToken"])
    resp = await client.post(
        "/api/merchant/spus", json=_spu_payload(category), headers=merchant_headers
    )
    assert resp.status_code == 200, resp.text
    spu_id = int(resp.json()["data"]["id"])
    sku_ids = [int(s["id"]) for s in resp.json()["data"]["skus"]]
    # ★ 上架：买家视角只看得到在售商品（`get_spu_detail` 的买家分支）
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=merchant_headers)
    resp = await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": True},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200, resp.text

    buyer = await register(client, phone=BUYER_PHONE)
    buyer_headers = auth_header(buyer["accessToken"])

    if ai_enabled:
        resp = await client.put(
            "/api/assistant/shop-setting",
            json={"aiEnabled": True},
            headers=merchant_headers,
        )
        assert resp.status_code == 200, resp.text

    resp = await client.post(
        "/api/support/tickets",
        json={
            "shopId": str(shop_id),
            "source": 2,
            "subject": "关于「测试商品」",
            # 商品页深链会带上它（会话据此记住买家在问哪件商品）
            **({"spuId": str(spu_id)} if with_spu else {}),
        },
        headers=buyer_headers,
    )
    assert resp.status_code == 200, resp.text
    ticket_no = resp.json()["data"]["ticketNo"]

    return {
        "admin_headers": auth_header(admin["accessToken"]),
        "merchant": merchant_headers,
        "buyer": buyer_headers,
        "buyer_id": await _user_id(BUYER_PHONE),
        "shop_id": shop_id,
        # 造出来的那件商品（下架测试要用）
        "spu_id": spu_id,
        # ★ 会话里记着的商品上下文 —— 就是 `answer_ticket` 从 detail.context 读到的那一份
        "ticket_spu_id": int(spu_id) if with_spu else None,
        "sku_ids": sku_ids,
        "ticket_no": ticket_no,
    }


async def _user_id(phone: str) -> int:
    """从库里取用户 id（注册接口只回 token，不带用户对象）。"""
    from app.core.crypto import phone_hash

    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT id FROM account.user WHERE phone_hash = :h"),
                {"h": phone_hash(phone)},
            )
        )


async def _say(client: AsyncClient, ctx: dict, body: str) -> int:
    """买家说一句，返回这条消息的 id（= 幂等闩锁的键）。"""
    resp = await client.post(
        f"/api/support/tickets/{ctx['ticket_no']}/messages",
        json={"body": body},
        headers=ctx["buyer"],
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["data"]["id"])


async def _sweep() -> list[int]:
    async with get_session_factory()() as s, s.begin():
        return await shopbot.sweep_ticket_turns(s)


async def _run_turn(source_message_id: int, llm: Any) -> None:
    """按 worker 的两段式跑一轮（领取 → 执行）。"""
    factory = get_session_factory()
    async with factory() as s, s.begin():
        assert await shopbot.claim_turn(s, source_message_id) is True
    async with factory() as s, s.begin():
        await shopbot.answer_ticket(s, source_message_id=source_message_id, llm=llm)


async def _turn_status(source_message_id: int) -> int | None:
    async with get_session_factory()() as s:
        turn = await repo.get_turn(s, source_message_id)
        return None if turn is None else int(turn.status)


async def _thread(client: AsyncClient, ctx: dict, headers: dict | None = None) -> list[dict]:
    """会话里的消息（默认用买家视角读）。"""
    resp = await client.get(
        f"/api/support/tickets/{ctx['ticket_no']}", headers=headers or ctx["buyer"]
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["messages"]


# ==================================================================
# 1. 幂等
# ==================================================================
async def test_sweep_is_idempotent(client: AsyncClient, session) -> None:
    """★ 扫描每 3 秒一轮：同一条买家消息**只能**排上一次活儿。"""
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "这个有货吗？")

    assert await _sweep() == [message_id]
    # 第二轮扫描：会话的最后一条还是买家（还没人答），但闩锁已经在了
    assert await _sweep() == []
    assert await _turn_status(message_id) is not None


async def test_sweep_skips_shops_with_ai_off(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session, ai_enabled=False)
    await _say(client, ctx, "在吗")
    assert await _sweep() == [], "没开的店不该排活儿"


async def test_sweep_skips_platform_tickets(client: AsyncClient, session) -> None:
    """★ 平台级会话（shop_id=0）没有店铺目录可查 —— 店小蜜不参与。"""
    ctx = await _prepare(client, session)
    resp = await client.post(
        "/api/support/tickets", json={"source": 4, "subject": "账号问题"}, headers=ctx["buyer"]
    )
    platform_no = resp.json()["data"]["ticketNo"]
    await client.post(
        f"/api/support/tickets/{platform_no}/messages",
        json={"body": "平台在吗"},
        headers=ctx["buyer"],
    )
    assert await _sweep() == []


# ==================================================================
# 2. 正常答一轮
# ==================================================================
async def test_ai_answers_in_the_ticket(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "这个多久发货？")
    assert await _sweep() == [message_id]

    await _run_turn(message_id, FakeLlm(replies=[answer("一般 48 小时内发出。")]))

    messages = await _thread(client, ctx)
    assert messages[-1]["body"] == "一般 48 小时内发出。"
    assert messages[-1]["senderTypeText"] == "智能客服"
    assert await _turn_status(message_id) == BOT_DONE

    # 球在买家手里：AI 答完就不欠商家回复了（会话离开待回复队列）
    detail = (
        await client.get(f"/api/merchant/support/tickets/{ctx['ticket_no']}", headers=ctx["merchant"])
    ).json()["data"]
    assert detail["staffOwesReply"] is False
    assert detail["needHuman"] is False


async def test_answer_is_clamped_to_a_chat_length(client: AsyncClient, session) -> None:
    """★ ``ticket_message`` 没有长度 CHECK，前端按短文本设计 —— 所以代码里硬夹。"""
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "介绍一下你们店")
    await _sweep()
    await _run_turn(message_id, FakeLlm(replies=[answer("很" * 2000)]))

    body = (await _thread(client, ctx))[-1]["body"]
    assert len(body) <= shopbot_rules.SHOPBOT_MAX_CHARS + 40  # clip_text 会加一句"已截断"
    assert "已截断" in body


# ==================================================================
# 3. ★ 抢话守卫：真人已经答了，AI 必须闭嘴
# ==================================================================
async def test_ai_does_not_overwrite_a_human_reply(client: AsyncClient, session) -> None:
    """模型跑了好几秒，这期间商家完全可能已经回了话。

    ★ 守卫漏掉的后果不是"多一句话"，而是 **AI 的话盖掉真人的话、并把会话重新挤出
      商家的「待回复」队列** —— 所以必须在写之前重读一次。
    """
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "这个有货吗？")
    await _sweep()

    # 商家在 AI 飞行途中回了话
    resp = await client.post(
        f"/api/merchant/support/tickets/{ctx['ticket_no']}/messages",
        json={"body": "在的，现货"},
        headers=ctx["merchant"],
    )
    assert resp.status_code == 200, resp.text

    factory = get_session_factory()
    async with factory() as s, s.begin():
        assert await shopbot.claim_turn(s, message_id) is True
    async with factory() as s, s.begin():
        await shopbot.answer_ticket(
            s, source_message_id=message_id, llm=FakeLlm(replies=[answer("我查一下啊")])
        )

    messages = await _thread(client, ctx)
    assert messages[-1]["body"] == "在的，现货"
    assert all(m["body"] != "我查一下啊" for m in messages), "AI 不该在真人之后插嘴"
    assert await _turn_status(message_id) == BOT_SKIPPED


async def test_ai_stays_out_when_the_switch_is_flipped_off(client: AsyncClient, session) -> None:
    """★ 只在扫描时看开关是不够的 —— 关掉之后排在队里的那一轮也得闭嘴。"""
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "这个有货吗？")
    await _sweep()

    resp = await client.put(
        "/api/assistant/shop-setting", json={"aiEnabled": False}, headers=ctx["merchant"]
    )
    assert resp.status_code == 200, resp.text

    await _run_turn(message_id, FakeLlm(replies=[answer("有货的")]))

    assert all(m["senderTypeText"] != "智能客服" for m in await _thread(client, ctx))
    assert await _turn_status(message_id) == BOT_SKIPPED


async def test_ai_stays_out_after_the_buyer_asked_for_a_human(
    client: AsyncClient, session
) -> None:
    """已转人工的会话不该再由机器人说话（买家已经明确要人了）。"""
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "这个有货吗？")
    await _sweep()
    resp = await client.post(
        f"/api/support/tickets/{ctx['ticket_no']}/request-human", headers=ctx["buyer"]
    )
    assert resp.status_code == 200, resp.text

    await _run_turn(message_id, FakeLlm(replies=[answer("有货的")]))
    assert all(m["senderTypeText"] != "智能客服" for m in await _thread(client, ctx))
    assert await _turn_status(message_id) == BOT_SKIPPED


# ==================================================================
# 4. 答不了 → 交给人
# ==================================================================
async def test_model_escalation_hands_the_ticket_to_a_human(
    client: AsyncClient, session
) -> None:
    """★ 模型调 ``request_human``：话照说，但**球交给真人**（置 need_human_at）。

    模型自己没有写状态的能力（它是信号，不是动作）—— 真正的置位由服务层做。
    """
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "能不能给我便宜 100？")
    await _sweep()

    await _run_turn(
        message_id,
        FakeLlm(
            replies=[
                tool_call("request_human", '{"reason": "买家要改价"}'),
                answer("这个我拿不准，已经叫了商家本人来回复你。"),
            ]
        ),
    )

    messages = await _thread(client, ctx)
    assert messages[-1]["senderTypeText"] == "智能客服"
    detail = (
        await client.get(f"/api/merchant/support/tickets/{ctx['ticket_no']}", headers=ctx["merchant"])
    ).json()["data"]
    assert detail["needHuman"] is True
    # 而它**留在**商家的待回复队列里（这正是 need_human_at 存在的理由）
    pending = await client.get(
        "/api/merchant/support/tickets", params={"pendingOnly": "true"}, headers=ctx["merchant"]
    )
    assert [i["ticketNo"] for i in pending.json()["data"]["items"]] == [ctx["ticket_no"]]


async def test_upstream_failure_says_something_and_hands_off(
    client: AsyncClient, session
) -> None:
    """上游挂了：**必须留一句话** —— 买家已经在等"客服"了，沉默比答错更糟。"""
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "有货吗")
    await _sweep()

    await _run_turn(message_id, FakeLlm(boom=LlmError("boom", timeout=True)))

    messages = await _thread(client, ctx)
    assert messages[-1]["body"] == shopbot_rules.SHOPBOT_FAILED_ANSWER
    assert await _turn_status(message_id) == BOT_DONE
    detail = (
        await client.get(f"/api/merchant/support/tickets/{ctx['ticket_no']}", headers=ctx["merchant"])
    ).json()["data"]
    assert detail["needHuman"] is True


async def test_quota_exhausted_hands_off_silently(client: AsyncClient, session) -> None:
    """★ 额度用完：**一个字都不说**，而这恰恰等于交给人了。

    AI 没说话 → 最后一条还是买家的 → 商家的「待回复」自然会收它。
    比"写一句我答不了"更好：不给买家噪音，也不白占商家的注意力。
    """
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "有货吗")
    await _sweep()

    settings = get_settings()
    async with get_session_factory()() as s, s.begin():
        await repo.add_usage(
            s,
            day=shopbot_rules.day_key(datetime.now(UTC)),
            scope=USAGE_SCOPE_SHOP,
            scope_id=int(ctx["shop_id"]),
            prompt_tokens=settings.assistant_daily_token_budget_shop,
            completion_tokens=0,
        )

    await _run_turn(message_id, FakeLlm(replies=[answer("有货的")]))

    assert all(m["senderTypeText"] != "智能客服" for m in await _thread(client, ctx))
    assert await _turn_status(message_id) == BOT_SKIPPED
    pending = await client.get(
        "/api/merchant/support/tickets", params={"pendingOnly": "true"}, headers=ctx["merchant"]
    )
    assert [i["ticketNo"] for i in pending.json()["data"]["items"]] == [ctx["ticket_no"]]


async def test_rate_limit_is_per_buyer_and_shop(client: AsyncClient, session) -> None:
    """按 (店铺, 买家) 限流：一个买家刷爆的是"他在这家店"的那道闸。"""
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, "有货吗")
    await _sweep()

    settings = get_settings()
    redis = get_redis()
    async with get_session_factory()() as s:
        buyer_id = int(await s.scalar(text("SELECT user_id FROM support.ticket LIMIT 1")))
    key = assistant_rate_shop_buyer(int(ctx["shop_id"]), buyer_id)
    await redis.set(key, settings.shopbot_buyer_rate_per_minute + 1)
    try:
        await _run_turn(message_id, FakeLlm(replies=[answer("有货的")]))
        assert all(m["senderTypeText"] != "智能客服" for m in await _thread(client, ctx))
        assert await _turn_status(message_id) == BOT_SKIPPED
    finally:
        await redis.delete(key)


# ==================================================================
# 5. 纯图片 / 空正文
# ==================================================================
async def test_image_only_message_is_not_answered(client: AsyncClient, session) -> None:
    """★ 店小蜜看不了图。商城的图片消息正文就是 ``[图片]`` 这个占位串。"""
    ctx = await _prepare(client, session)
    message_id = await _say(client, ctx, shopbot_rules.IMAGE_ONLY_BODY)
    assert await _sweep() == [message_id]

    await _run_turn(message_id, FakeLlm(replies=[answer("这个颜色有货")]))
    assert all(m["senderTypeText"] != "智能客服" for m in await _thread(client, ctx))
    assert await _turn_status(message_id) == BOT_SKIPPED


# ==================================================================
# 6. 兜底收尾
# ==================================================================
async def test_reaper_resends_pending_and_fails_running(client: AsyncClient, session) -> None:
    """★ 两种卡法处理方式不同：PENDING（没跑过模型）重投，RUNNING（跑过）判失败。"""
    ctx = await _prepare(client, session)
    first = await _say(client, ctx, "第一句")
    await _sweep()
    # 人为把它推成一个"很久以前就卡住"的 PENDING
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE assistant.bot_turn SET created_at = now() - interval '10 minutes'"
                " WHERE source_message_id = :sid"
            ),
            {"sid": first},
        )

    async with get_session_factory()() as s, s.begin():
        resend, failed = await shopbot.reap_stuck_turns(s)
    assert resend == [first]
    assert failed == 0
    # ★ 重投递**不改状态**：worker 拿到它照样能 claim（这正是重投的意义）
    assert await _turn_status(first) is not None

    second = await _say(client, ctx, "第二句")
    await _sweep()
    async with get_session_factory()() as s, s.begin():
        assert await shopbot.claim_turn(s, second) is True
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE assistant.bot_turn SET created_at = now() - interval '10 minutes'"
                " WHERE source_message_id = :sid"
            ),
            {"sid": second},
        )
    async with get_session_factory()() as s, s.begin():
        resend, failed = await shopbot.reap_stuck_turns(s)
    # ``first`` 还停在 PENDING（用例里没人 claim 它），所以它会被**再投一次** ——
    # ★ 这是对的：worker 拿到它照样能 claim，重投是安全的，直到真跑起来为止
    assert resend == [first]
    assert failed == 1
    assert await _turn_status(second) == BOT_FAILED


# ==================================================================
# 7. 商户侧的开关 / 问答 / 计数
# ==================================================================
async def test_shop_admin_endpoints_are_shop_scoped(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)

    # 开关
    resp = await client.get("/api/assistant/shop-setting", headers=ctx["merchant"])
    assert resp.json()["data"]["aiEnabled"] is True

    # 问答：加一条、改一条、删一条
    resp = await client.post(
        "/api/assistant/shop-faq",
        json={"question": "什么时候发货？", "answer": "48 小时内。"},
        headers=ctx["merchant"],
    )
    assert resp.status_code == 200, resp.text
    faq_id = resp.json()["data"]["id"]

    resp = await client.put(
        f"/api/assistant/shop-faq/{faq_id}",
        json={"question": "多久发货？", "answer": "48 小时内发出。", "enabled": False},
        headers=ctx["merchant"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["enabled"] is False

    # ★ 停用的不进提示词
    async with get_session_factory()() as s:
        enabled_rows = await repo.list_faq(s, int(ctx["shop_id"]), enabled_only=True)
    assert enabled_rows == []

    # ★ 运营（没有店铺）不该能碰某个商户的开关 —— 这是商户自己的事
    resp = await client.put(
        "/api/assistant/shop-setting", json={"aiEnabled": True}, headers=ctx["admin_headers"]
    )
    assert resp.status_code == 403

    # 统计：还没答过
    resp = await client.get("/api/assistant/shop-bot/stats", headers=ctx["merchant"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["answered"] == 0

    resp = await client.delete(
        f"/api/assistant/shop-faq/{faq_id}", headers=ctx["merchant"]
    )
    assert resp.status_code == 200, resp.text


async def test_another_merchant_cannot_touch_the_faq(client: AsyncClient, session) -> None:
    """★ 归属不对 → 404（不是 403），与全项目一致。"""
    ctx = await _prepare(client, session)
    resp = await client.post(
        "/api/assistant/shop-faq",
        json={"question": "包邮吗？", "answer": "满 99 包邮。"},
        headers=ctx["merchant"],
    )
    faq_id = resp.json()["data"]["id"]

    other = await register(client, phone="13800139203")
    await open_shop(client, other["accessToken"], name="别人家的店")
    other_headers = auth_header(other["accessToken"])

    resp = await client.put(
        f"/api/assistant/shop-faq/{faq_id}",
        json={"question": "x", "answer": "y"},
        headers=other_headers,
    )
    assert resp.status_code == 404
    resp = await client.delete(f"/api/assistant/shop-faq/{faq_id}", headers=other_headers)
    assert resp.status_code == 404


# ==================================================================
# 8. 买家侧的数据工具（block 3）
# ==================================================================
async def _exec(
    session: Any, ticket: shopbot_rules.ShopbotTicket, name: str, **args: Any
) -> dict[str, Any]:
    """直接跑一个买家工具（不经过模型）。"""
    tool_ctx = tools.ToolContext(
        session=session,
        caller=shopbot_rules.Caller(
            user_id=ticket.buyer_user_id, role=ACTOR_BUYER, shop_id=ticket.shop_id
        ),
        ticket=ticket,
    )
    return await tools.execute(tool_ctx, name, args)


def _ticket_of(ctx: dict, **extra: Any) -> shopbot_rules.ShopbotTicket:
    return shopbot_rules.ShopbotTicket(
        ticket_no=ctx["ticket_no"],
        shop_id=ctx["shop_id"],
        buyer_user_id=ctx["buyer_id"],
        # ★ 与 worker 一样：从**会话**里读商品上下文（不是从测试参数里传）
        spu_id=ctx["ticket_spu_id"],
        **extra,
    )


async def test_ticket_product_comes_from_the_conversation(client: AsyncClient, session) -> None:
    """★ 那件商品来自**会话**（服务端注入的 spu_id），模型没有任何 id 参数。"""
    ctx = await _prepare(client, session)
    assert (await client.get(f"/api/support/tickets/{ctx['ticket_no']}", headers=ctx["buyer"])).json()[
        "data"
    ]["context"]["spuId"] == str(ctx["spu_id"])

    out = await _exec(session, _ticket_of(ctx), "ticket_product")
    assert out["title"]
    assert out["price_min_cent"] > 0
    # 每个规格都带**档位文案**（不是真实件数）
    assert out["skus"] and all("stock_text" in sku for sku in out["skus"])
    assert all(sku["price_cent"] > 0 for sku in out["skus"])


async def test_ticket_product_without_context_says_so(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session, with_spu=False)
    out = await _exec(session, _ticket_of(ctx), "ticket_product")
    assert "hint" in out, "没有商品上下文时照实说，别猜"


async def test_off_shelf_product_is_not_visible(client: AsyncClient, session) -> None:
    """★ 买家视角只看得到在售商品 —— 下架之后 AI 也只能说"看不到了"。"""
    ctx = await _prepare(client, session)
    resp = await client.post(
        f"/api/merchant/spus/{ctx['spu_id']}/off-shelf", headers=ctx["merchant"]
    )
    assert resp.status_code == 200, resp.text

    out = await _exec(session, _ticket_of(ctx), "ticket_product")
    assert out["visible"] is False


async def test_ticket_order_is_scoped_to_the_buyer(client: AsyncClient, session) -> None:
    """★ 归属不对就是"查不到" —— **即使单号被塞进会话上下文里**。

    会话上下文只是"展示与取数"用的（见 support 的注释），归属永远由被查的那个
    模块自己判：``get_my_order_detail(user_id=…)``。
    """
    trade = await _prepare_trade(client, session)
    order = await _create_order(client, trade, idem="shopbot-scope")
    order_no = order["orderMainNo"]

    stranger = await register(client, phone="13800139209")
    assert stranger  # 只为了拿一个**别人的** user_id 去构造伪造的上下文
    forged = shopbot_rules.ShopbotTicket(
        ticket_no="T-forged",
        shop_id=trade["shop_id"],
        buyer_user_id=await _user_id("13800139209"),
        order_main_no=order_no,
    )
    assert await _exec(session, forged, "ticket_order") == {"hint": "这个订单查不到"}


async def test_ticket_order_result_carries_no_address(client: AsyncClient, session) -> None:
    """★ 白名单在**真实数据**上也成立：收货人/手机号/地址一律不进上下文。"""
    trade = await _prepare_trade(client, session)
    order = await _create_order(client, trade, idem="shopbot-proj")
    ticket = shopbot_rules.ShopbotTicket(
        ticket_no="T-proj",
        shop_id=trade["shop_id"],
        buyer_user_id=trade["buyer_id"],
        order_main_no=order["orderMainNo"],
    )
    out = await _exec(session, ticket, "ticket_order")
    dumped = json.dumps(out, ensure_ascii=False)

    # 地址是 `_make_address` 造的那一份（张三 / 13800138000 / 上海市 / 某某路）
    for secret in ("张三", "13800138000", "上海市", "浦东", "某某路", "receiver"):
        assert secret not in dumped, f"{secret} 不该进模型上下文"
    # 该有的都在
    assert order["orderMainNo"] in dumped
    assert out["subs"] and out["subs"][0]["items"]


async def test_my_orders_in_shop_is_limited_to_that_shop(client: AsyncClient, session) -> None:
    """★ ``shop_id`` 由服务端注入：问"我买过什么"，只能看到**这家店**的那部分。"""
    trade = await _prepare_trade(client, session)
    order = await _create_order(client, trade, idem="shopbot-list")
    mine = shopbot_rules.ShopbotTicket(
        ticket_no="T-list",
        shop_id=trade["shop_id"],
        buyer_user_id=trade["buyer_id"],
    )
    out = await _exec(session, mine, "my_orders_in_shop")
    assert [o["order_main_no"] for o in out["orders"]] == [order["orderMainNo"]]

    # 换成**别的店**的上下文：这单不属于它
    other = shopbot_rules.ShopbotTicket(
        ticket_no="T-list2",
        shop_id=int(trade["shop_id"]) + 1,
        buyer_user_id=trade["buyer_id"],
    )
    assert (await _exec(session, other, "my_orders_in_shop"))["orders"] == []


async def test_ticket_refund_without_context_says_so(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    assert "hint" in await _exec(session, _ticket_of(ctx), "ticket_refund")
    assert "hint" in await _exec(session, _ticket_of(ctx), "ticket_order")


async def test_buyer_cannot_reach_staff_tools(client: AsyncClient, session) -> None:
    """★ 纵深防御：买家身份调商家/运营的工具 —— 已注册的拒（ToolDenied），
    压根没注册的直接"没有这个工具"。两条路都 fail-closed。"""
    ctx = await _prepare(client, session)
    tool_ctx = tools.ToolContext(
        session=session,
        caller=shopbot_rules.Caller(
            user_id=ctx["buyer_id"], role=ACTOR_BUYER, shop_id=ctx["shop_id"]
        ),
        ticket=_ticket_of(ctx),
    )
    with pytest.raises(tools.ToolDenied):
        await tools.execute(tool_ctx, "list_orders", {})
    with pytest.raises(BizError):
        # 禁止清单里那个（裸查询、无归属校验）—— 从来没注册过
        await tools.execute(tool_ctx, "get_shop_order", {})
