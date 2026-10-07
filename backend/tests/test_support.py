"""客服会话的集成测试（真 PG + Redis）。

钉住的七条不变量：

1. 一个用户对同一个对象同时只有**一条**进行中会话（部分唯一索引），关闭后可再开
2. 平台级会话用哨兵 ``shop_id=0``，与真实店铺会话互不干扰
3. 商家/平台回复 → **同事务**给买家写站内信（所以角标立刻准，不靠 worker）
4. 归属不对一律 **404**（不是 403，不给遍历探测留口子）
5. 关闭后再发消息 = **重开同一条**（不是报错、也不新建）
6. 「待回复」= 进行中 + 最后一条是买家（SQL 与规则函数同一判定）
7. 打开详情推进读游标 → 未读归零
8. 平台**只**读/回平台级会话（店里的买家会话 → 404，也不进平台角标）
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import get_session_factory
from app.core.redis import get_redis
from app.modules.support import service as support_service
from tests.conftest import (
    api_code,
    auth_header,
    make_admin,
    open_shop,
    register,
)

BUYER_PHONE = "13800131001"
MERCHANT_PHONE = "13800131002"
MERCHANT2_PHONE = "13800131003"
ADMIN_PHONE = "13900131004"


@pytest.fixture(autouse=True)
async def clean_support_keys() -> AsyncIterator[None]:
    """限流计数在 Redis 里，不随 PG 的 TRUNCATE 清掉 —— 不清会串到下一个用例。"""
    redis = get_redis()
    for key in await redis.keys("support:ratelimit:*"):
        await redis.delete(key)
    yield
    for key in await redis.keys("support:ratelimit:*"):
        await redis.delete(key)


async def _me(client: AsyncClient, headers: dict[str, str]) -> int:
    resp = await client.get("/api/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return int(resp.json()["data"]["id"])


async def _setup(client: AsyncClient, session) -> dict:
    buyer = await register(client, phone=BUYER_PHONE)
    merchant = await register(client, phone=MERCHANT_PHONE)
    merchant2 = await register(client, phone=MERCHANT2_PHONE)
    admin = await make_admin(client, session, phone=ADMIN_PHONE)
    buyer_headers = auth_header(buyer["accessToken"])
    return {
        "buyer": buyer_headers,
        "buyer_id": await _me(client, buyer_headers),
        "buyer2": auth_header((await register(client, phone="13800131005"))["accessToken"]),
        "merchant": auth_header(merchant["accessToken"]),
        "merchant2": auth_header(merchant2["accessToken"]),
        "admin": auth_header(admin["accessToken"]),
        "shop_id": await open_shop(client, merchant["accessToken"], name="客服一号店"),
        "shop2_id": await open_shop(client, merchant2["accessToken"], name="客服二号店"),
    }


async def _open(client: AsyncClient, ctx: dict, *, headers: dict | None = None, **extra) -> dict:
    resp = await client.post(
        "/api/support/tickets",
        json={"source": 2, **extra},
        headers=headers or ctx["buyer"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _send(
    client: AsyncClient, headers: dict, ticket_no: str, body: str = "你好，在吗？"
) -> dict:
    resp = await client.post(
        f"/api/support/tickets/{ticket_no}/messages",
        json={"body": body},
        headers=headers,
    )
    return resp


# ============================================================
# 1 / 5：唯一性与重开
# ============================================================
async def test_open_ticket_is_reused_for_same_shop(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    first = await _open(client, ctx, shopId=ctx["shop_id"])
    second = await _open(client, ctx, shopId=ctx["shop_id"])
    assert first["ticketNo"] == second["ticketNo"], "同一对象不该开出第二条进行中会话"
    assert first["status"] == 10
    assert first["messages"] == [], "开会话不带首条消息 —— 那走 /messages"


async def test_second_active_ticket_blocked_by_partial_unique_index(
    client: AsyncClient, session
) -> None:
    """唯一性由**数据库**兜底，不只是应用层的"先查再插"。"""
    ctx = await _setup(client, session)
    await _open(client, ctx, shopId=ctx["shop_id"])
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO support.ticket"
                " (id, ticket_no, user_id, shop_id, subject, source, status)"
                " VALUES (999999999999, 'TDUP0000000000000000', :uid, :sid, 'x', 5, 10)"
            ),
            {"uid": ctx["buyer_id"], "sid": int(ctx["shop_id"])},
        )
        await session.flush()
    await session.rollback()


async def test_closed_ticket_frees_the_slot(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    first = await _open(client, ctx, shopId=ctx["shop_id"])
    resp = await client.post(
        f"/api/support/tickets/{first['ticketNo']}/close", json={}, headers=ctx["buyer"]
    )
    assert resp.status_code == 200, resp.text
    second = await _open(client, ctx, shopId=ctx["shop_id"])
    assert second["ticketNo"] != first["ticketNo"], "关闭之后应该能再开一条"


async def test_reply_reopens_closed_ticket(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]
    await client.post(f"/api/support/tickets/{no}/close", json={"reason": "问完了"}, headers=ctx["buyer"])

    resp = await _send(client, ctx["buyer"], no, "又想起来一个问题")
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"/api/support/tickets/{no}", headers=ctx["buyer"])
    data = resp.json()["data"]
    assert data["status"] == 10, "关闭后再发消息应该重开同一条"
    assert data["closeByText"] is None
    assert data["closeTime"] is None
    assert len(data["messages"]) == 1


async def test_close_is_idempotent(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]
    for _ in range(2):
        resp = await client.post(
            f"/api/support/tickets/{no}/close", json={}, headers=ctx["buyer"]
        )
        assert resp.status_code == 200, "重复点「结束会话」不该报错"


# ============================================================
# 2：平台级哨兵
# ============================================================
async def test_platform_ticket_is_separate_from_shop_ticket(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    shop_ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    platform_ticket = await _open(client, ctx, source=4)  # 不传 shopId = 平台级

    assert shop_ticket["ticketNo"] != platform_ticket["ticketNo"]
    assert platform_ticket["shopId"] == "0"
    assert platform_ticket["shopName"] == "平台客服"

    resp = await client.get("/api/support/tickets", headers=ctx["buyer"])
    assert len(resp.json()["data"]["items"]) == 2, "同一用户的两条会话都要出现"


# ============================================================
# 3：回复 → 同事务站内信
# ============================================================
async def test_merchant_reply_writes_buyer_site_message(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]
    assert (await _send(client, ctx["buyer"], no)).status_code == 200

    resp = await client.post(
        f"/api/merchant/support/tickets/{no}/messages",
        json={"body": "在的，请问有什么可以帮您"},
        headers=ctx["merchant"],
    )
    assert resp.status_code == 200, resp.text

    # ★ 没有跑 worker、没有跑投递循环 —— 站内信**已经**在了。
    #   这就是"同事务直写"与"走 outbox"的区别，也是这条用例要钉住的东西。
    resp = await client.get("/api/notifications/unread-count", headers=ctx["buyer"])
    assert resp.json()["data"]["count"] == 1

    resp = await client.get("/api/notifications", headers=ctx["buyer"])
    item = resp.json()["data"]["items"][0]
    assert item["msgType"] == "SUPPORT_REPLY"
    assert item["msgTypeText"] == "客服回复"
    assert item["linkType"] == "TICKET"
    assert item["linkValue"] == no
    assert item["isRead"] is False


async def test_buyer_reply_does_not_notify_the_buyer(client: AsyncClient, session) -> None:
    """买家自己发的消息不该给自己发站内信。"""
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    assert (await _send(client, ctx["buyer"], ticket["ticketNo"])).status_code == 200
    resp = await client.get("/api/notifications/unread-count", headers=ctx["buyer"])
    assert resp.json()["data"]["count"] == 0


# ============================================================
# 4：越权一律 404
# ============================================================
async def test_merchant_cannot_read_another_shops_ticket(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]

    resp = await client.get(f"/api/merchant/support/tickets/{no}", headers=ctx["merchant2"])
    assert resp.status_code == 404, "别人的会话应该 404（不是 403）"

    resp = await client.post(
        f"/api/merchant/support/tickets/{no}/messages",
        json={"body": "我不该能回"},
        headers=ctx["merchant2"],
    )
    assert resp.status_code == 404
    assert api_code(resp.json()) == "NOT_FOUND"

    resp = await client.post(
        f"/api/merchant/support/tickets/{no}/close", json={}, headers=ctx["merchant2"]
    )
    assert resp.status_code == 404


async def test_buyer_cannot_read_another_buyers_ticket(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]

    resp = await client.get(f"/api/support/tickets/{no}", headers=ctx["buyer2"])
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/support/tickets/{no}/messages", json={"body": "偷看"}, headers=ctx["buyer2"]
    )
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/support/tickets/{no}/close", json={}, headers=ctx["buyer2"]
    )
    assert resp.status_code == 404


async def test_merchant_cannot_see_platform_ticket(client: AsyncClient, session) -> None:
    """平台级会话对商家不可见 —— 靠 ``ticket.shop_id == shop_id`` 这条等式挡住。"""
    ctx = await _setup(client, session)
    platform_ticket = await _open(client, ctx, source=4)
    resp = await client.get(
        f"/api/merchant/support/tickets/{platform_ticket['ticketNo']}", headers=ctx["merchant"]
    )
    assert resp.status_code == 404


async def test_merchant_without_shop_is_rejected(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    resp = await client.get("/api/merchant/support/tickets", headers=ctx["buyer"])
    assert resp.status_code == 403


# ============================================================
# 平台：只做平台级会话（提给平台的工单）
# ============================================================
async def test_platform_only_handles_platform_tickets(client: AsyncClient, session) -> None:
    """★ 平台**看不到也插不进**店里的买家会话。

    这条是一次行为上的**收紧**：原来平台能列出所有店铺的会话，还能以「平台客服」
    的身份发言 —— 买家正跟某家店聊，中间冒出一句平台客服的话，谁跟谁说话都分不清
    （见 ``service.list_platform``）。
    """
    ctx = await _setup(client, session)
    shop_ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    platform_ticket = await _open(client, ctx, source=4)
    await _send(client, ctx["buyer"], shop_ticket["ticketNo"])
    await _send(client, ctx["buyer"], platform_ticket["ticketNo"])

    resp = await client.get("/api/admin/support/tickets", headers=ctx["admin"])
    assert resp.status_code == 200, resp.text
    nos = {i["ticketNo"] for i in resp.json()["data"]["items"]}
    assert platform_ticket["ticketNo"] in nos
    assert shop_ticket["ticketNo"] not in nos, "店里买家的会话不进平台队列"

    # 详情与发言：店会话一律 **404**（不是 403 —— 不给遍历探测留口子）
    resp = await client.get(
        f"/api/admin/support/tickets/{shop_ticket['ticketNo']}", headers=ctx["admin"]
    )
    assert resp.status_code == 404

    resp = await client.post(
        f"/api/admin/support/tickets/{shop_ticket['ticketNo']}/messages",
        json={"body": "平台客服已介入"},
        headers=ctx["admin"],
    )
    assert resp.status_code == 404, "平台不能在店里的会话中发言"

    resp = await client.post(
        f"/api/admin/support/tickets/{shop_ticket['ticketNo']}/close",
        json={},
        headers=ctx["admin"],
    )
    assert resp.status_code == 404

    # 平台级那条：能读（手机号打码，docs/19 §4）、能回，提问方收得到
    resp = await client.get(
        f"/api/admin/support/tickets/{platform_ticket['ticketNo']}", headers=ctx["admin"]
    )
    assert resp.status_code == 200, resp.text
    assert "****" in resp.json()["data"]["buyerPhone"]

    resp = await client.post(
        f"/api/admin/support/tickets/{platform_ticket['ticketNo']}/messages",
        json={"body": "平台已收到，正在处理"},
        headers=ctx["admin"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["senderTypeText"] == "平台客服"

    resp = await client.get(
        f"/api/support/tickets/{platform_ticket['ticketNo']}", headers=ctx["buyer"]
    )
    assert resp.json()["data"]["messages"][-1]["body"] == "平台已收到，正在处理"


async def test_admin_list_is_rejected_for_merchant(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    resp = await client.get("/api/admin/support/tickets", headers=ctx["merchant"])
    assert resp.status_code == 403


# ============================================================
# 智能客服（SENDER_AI）与「转人工」
# ============================================================
async def _ai_reply(ctx: dict, ticket_no: str, body: str = "有货的，今天就能发。") -> None:
    """直接调服务层的 AI 回复入口（不经过 HTTP —— 买家侧没有这个端点）。"""
    async with get_session_factory()() as s, s.begin():
        await support_service.reply_as_ai(
            s, get_redis(), shop_id=int(ctx["shop_id"]), ticket_no=ticket_no, body=body
        )


async def test_ai_reply_leaves_staff_cursor_and_notify_alone(
    client: AsyncClient, session
) -> None:
    """★ AI 说一句，**不许**动商家的读游标、也不许推站内信。

    这两件事原来是 ``_reply`` 的 ``else`` 分支干的（"不是买家 = 客服回复"），
    ``SENDER_AI`` 会天然掉进去，于是：
    ① ``set_staff_read(now)`` 把商家的读游标推到现在 —— 买家刚问的问题被机器人
       自己标成"商家已读"，**商家未读角标归零**；
    ② 推一条「客服回复了你」—— 说话的根本不是人（误导），还点亮买家的角标。
    """
    ctx = await _setup(client, session)
    no = (await _open(client, ctx, shopId=ctx["shop_id"]))["ticketNo"]
    assert (await _send(client, ctx["buyer"], no)).status_code == 200

    await _ai_reply(ctx, no)

    resp = await client.get("/api/merchant/support/tickets", headers=ctx["merchant"])
    row = next(i for i in resp.json()["data"]["items"] if i["ticketNo"] == no)
    assert row["lastSenderText"] == "智能客服"
    assert row["unread"] == 1, "AI 回复不能替商家把买家的问题标成已读"
    # 球在买家手里：AI 答完了，商家不欠回复
    assert row["staffOwesReply"] is False

    # 买家一条站内信都不该收到（AI 的话在会话页里看得见）
    resp = await client.get("/api/notifications", headers=ctx["buyer"])
    assert resp.json()["data"]["items"] == []


async def test_ai_escalation_keeps_the_ticket_in_the_queue(client: AsyncClient, session) -> None:
    """★ AI 答不了 → 标「需要人工」→ 会话**必须留在**商家的待回复队列里。

    这正是 ``need_human_at`` 存在的理由：AI 一说话，"最后一条是买家"就不成立，
    光靠 ``last_sender_type`` 会把它挤出队列。
    """
    ctx = await _setup(client, session)
    no = (await _open(client, ctx, shopId=ctx["shop_id"]))["ticketNo"]
    await _send(client, ctx["buyer"], no)
    await _ai_reply(ctx, no, "这个我拿不准，已经叫商家了。")

    async with get_session_factory()() as s, s.begin():
        await support_service.flag_need_human(s, shop_id=int(ctx["shop_id"]), ticket_no=no)

    resp = await client.get(
        "/api/merchant/support/tickets", params={"pendingOnly": "true"}, headers=ctx["merchant"]
    )
    items = resp.json()["data"]["items"]
    assert [i["ticketNo"] for i in items] == [no]
    assert items[0]["staffOwesReply"] is True
    assert items[0]["lastSenderText"] == "智能客服", "最后一条是 AI，但队列仍看得见"

    # ★ 三处一致：列表（上面）、角标（这里）、纯函数（test_support_units）
    count = await client.get("/api/merchant/support/pending-count", headers=ctx["merchant"])
    assert count.json()["data"]["count"] == 1


async def test_buyer_can_request_human(client: AsyncClient, session) -> None:
    """买家点「转人工」：置闩锁 + 一条说明消息，**不新开会话**，且幂等。"""
    ctx = await _setup(client, session)
    no = (await _open(client, ctx, shopId=ctx["shop_id"]))["ticketNo"]
    await _send(client, ctx["buyer"], no)

    resp = await client.post(f"/api/support/tickets/{no}/request-human", headers=ctx["buyer"])
    assert resp.status_code == 200, resp.text

    detail = (await client.get(f"/api/support/tickets/{no}", headers=ctx["buyer"])).json()["data"]
    assert detail["needHuman"] is True
    assert detail["messages"][-1]["senderTypeText"] == "系统"
    assert "转接人工" in detail["messages"][-1]["body"]

    # 最后一条是系统说的，但它仍然在商家的「待回复」里（need_human_at 兜住了）
    count = await client.get("/api/merchant/support/pending-count", headers=ctx["merchant"])
    assert count.json()["data"]["count"] == 1

    # 幂等：重复点只有第一条说明
    await client.post(f"/api/support/tickets/{no}/request-human", headers=ctx["buyer"])
    detail = (await client.get(f"/api/support/tickets/{no}", headers=ctx["buyer"])).json()["data"]
    assert sum("转接人工" in m["body"] for m in detail["messages"]) == 1

    # 商家回话 → 闩锁解开、离开队列
    await client.post(
        f"/api/merchant/support/tickets/{no}/messages",
        json={"body": "在的"},
        headers=ctx["merchant"],
    )
    detail = (await client.get(f"/api/merchant/support/tickets/{no}", headers=ctx["merchant"])).json()[
        "data"
    ]
    assert detail["needHuman"] is False
    assert detail["staffOwesReply"] is False
    count = await client.get("/api/merchant/support/pending-count", headers=ctx["merchant"])
    assert count.json()["data"]["count"] == 0


async def test_request_human_needs_ownership(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    no = (await _open(client, ctx, shopId=ctx["shop_id"]))["ticketNo"]
    resp = await client.post(
        f"/api/support/tickets/{no}/request-human", headers=ctx["buyer2"]
    )
    assert resp.status_code == 404, "别人的会话一律当作不存在"


# ============================================================
# 6：待回复筛选
# ============================================================
async def test_pending_only_lists_tickets_awaiting_staff(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]
    assert (await _send(client, ctx["buyer"], no)).status_code == 200

    resp = await client.get(
        "/api/merchant/support/tickets", params={"pendingOnly": "true"}, headers=ctx["merchant"]
    )
    items = resp.json()["data"]["items"]
    assert [i["ticketNo"] for i in items] == [no]
    assert items[0]["staffOwesReply"] is True
    assert items[0]["unread"] == 1, "买家发了一条，商家侧应显示一条未读"

    await client.post(
        f"/api/merchant/support/tickets/{no}/messages", json={"body": "好的"}, headers=ctx["merchant"]
    )
    resp = await client.get(
        "/api/merchant/support/tickets", params={"pendingOnly": "true"}, headers=ctx["merchant"]
    )
    assert resp.json()["data"]["items"] == [], "回复之后不再「待回复」"

    resp = await client.get("/api/merchant/support/tickets", headers=ctx["merchant"])
    assert len(resp.json()["data"]["items"]) == 1, "不带筛选仍然看得到"


# ============================================================
# 7：读详情推进游标
# ============================================================
async def test_reading_detail_clears_unread(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]
    await _send(client, ctx["buyer"], no)
    await client.post(
        f"/api/merchant/support/tickets/{no}/messages", json={"body": "在的"}, headers=ctx["merchant"]
    )

    resp = await client.get("/api/support/tickets", headers=ctx["buyer"])
    assert resp.json()["data"]["items"][0]["unread"] == 1

    assert (
        await client.get(f"/api/support/tickets/{no}", headers=ctx["buyer"])
    ).status_code == 200
    resp = await client.get("/api/support/tickets", headers=ctx["buyer"])
    assert resp.json()["data"]["items"][0]["unread"] == 0, "打开详情即已读"


# ============================================================
# 分页 + 限流
# ============================================================
async def test_ticket_list_paginates_by_cursor(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    for shop_id in (ctx["shop_id"], ctx["shop2_id"], None):
        payload = {"source": 5} if shop_id is None else {"source": 5, "shopId": shop_id}
        resp = await client.post("/api/support/tickets", json=payload, headers=ctx["buyer"])
        assert resp.status_code == 200, resp.text

    resp = await client.get("/api/support/tickets", params={"limit": 2}, headers=ctx["buyer"])
    page = resp.json()["data"]
    assert len(page["items"]) == 2 and page["hasMore"] is True

    resp = await client.get(
        "/api/support/tickets",
        params={"limit": 2, "cursor": page["nextCursor"]},
        headers=ctx["buyer"],
    )
    rest = resp.json()["data"]
    assert len(rest["items"]) == 1 and rest["hasMore"] is False
    assert not ({i["ticketNo"] for i in rest["items"]} & {i["ticketNo"] for i in page["items"]})


async def test_send_message_is_rate_limited(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]

    for index in range(20):
        resp = await _send(client, ctx["buyer"], no, f"第 {index} 条")
        assert resp.status_code == 200, resp.text

    resp = await _send(client, ctx["buyer"], no, "第 21 条")
    assert resp.status_code == 429
    assert api_code(resp.json()) == "SUPPORT_RATE_LIMITED"


# ============================================================
# 站内信：标记已读
# ============================================================
async def test_mark_read_and_read_all(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]
    await _send(client, ctx["buyer"], no)
    await client.post(
        f"/api/merchant/support/tickets/{no}/messages", json={"body": "回复一"}, headers=ctx["merchant"]
    )
    await client.post(
        f"/api/merchant/support/tickets/{no}/messages", json={"body": "回复二"}, headers=ctx["merchant"]
    )

    resp = await client.get("/api/notifications", headers=ctx["buyer"])
    items = resp.json()["data"]["items"]
    assert len(items) == 2

    resp = await client.post(
        f"/api/notifications/{items[0]['id']}/read", headers=ctx["buyer"]
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/notifications/unread-count", headers=ctx["buyer"])
    assert resp.json()["data"]["count"] == 1

    resp = await client.post("/api/notifications/read-all", headers=ctx["buyer"])
    assert resp.status_code == 200
    resp = await client.get("/api/notifications/unread-count", headers=ctx["buyer"])
    assert resp.json()["data"]["count"] == 0


async def test_cannot_mark_another_users_message_read(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]
    await _send(client, ctx["buyer"], no)
    await client.post(
        f"/api/merchant/support/tickets/{no}/messages", json={"body": "回复"}, headers=ctx["merchant"]
    )
    resp = await client.get("/api/notifications", headers=ctx["buyer"])
    msg_id = resp.json()["data"]["items"][0]["id"]

    resp = await client.post(f"/api/notifications/{msg_id}/read", headers=ctx["buyer2"])
    assert resp.status_code == 404


async def test_unread_only_filter(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]
    await _send(client, ctx["buyer"], no)
    await client.post(
        f"/api/merchant/support/tickets/{no}/messages", json={"body": "回复"}, headers=ctx["merchant"]
    )
    await client.post("/api/notifications/read-all", headers=ctx["buyer"])

    resp = await client.get(
        "/api/notifications", params={"unreadOnly": "true"}, headers=ctx["buyer"]
    )
    assert resp.json()["data"]["items"] == []
    resp = await client.get("/api/notifications", headers=ctx["buyer"])
    assert len(resp.json()["data"]["items"]) == 1


# ============================================================
# 待回复角标
# ============================================================
async def test_pending_count_endpoints(client: AsyncClient, session) -> None:
    """后台导航角标的数据源。**刚开出来的空会话不算待回复** —— 买家只说"我要咨询"
    就走了的话，不该在商家队列里挂一条永远没人回的空单。"""
    ctx = await _setup(client, session)
    ticket = await _open(client, ctx, shopId=ctx["shop_id"])
    no = ticket["ticketNo"]

    resp = await client.get("/api/merchant/support/pending-count", headers=ctx["merchant"])
    assert resp.json()["data"]["count"] == 0, "还没有人说话，不欠谁回复"

    await _send(client, ctx["buyer"], no)
    resp = await client.get("/api/merchant/support/pending-count", headers=ctx["merchant"])
    assert resp.json()["data"]["count"] == 1

    resp = await client.get("/api/merchant/support/pending-count", headers=ctx["merchant2"])
    assert resp.json()["data"]["count"] == 0, "别的店看不到"

    # ★ 平台角标只数**平台级**：店里买家的咨询不归平台（见 service.list_platform）
    resp = await client.get("/api/admin/support/pending-count", headers=ctx["admin"])
    assert resp.json()["data"]["count"] == 0, "店里买家的咨询不计入平台角标"

    platform = await _open(client, ctx, source=4)
    await _send(client, ctx["buyer"], platform["ticketNo"])
    resp = await client.get("/api/admin/support/pending-count", headers=ctx["admin"])
    assert resp.json()["data"]["count"] == 1, "提给平台的工单才算平台的待回复"

    await client.post(
        f"/api/merchant/support/tickets/{no}/messages", json={"body": "在的"}, headers=ctx["merchant"]
    )
    resp = await client.get("/api/merchant/support/pending-count", headers=ctx["merchant"])
    assert resp.json()["data"]["count"] == 0, "回复之后不再计入"

    resp = await client.get("/api/admin/support/pending-count", headers=ctx["buyer"])
    assert resp.status_code == 403


async def test_reopening_refreshes_the_context(client: AsyncClient, session) -> None:
    """★ 复用已有会话时，上下文**跟着最后一次点击走**。

    不刷新就会答错：买家上一轮从商品 A 点进来、这一轮从商品 B 点进来，会话里记的
    还是 A —— 店小蜜会拿 A 的价格去答 B 的问题（见 ``repo.refresh_context``）。
    """
    ctx = await _setup(client, session)
    first = await _open(client, ctx, shopId=ctx["shop_id"], spuId="1001", orderMainNo="M1")
    again = await _open(client, ctx, shopId=ctx["shop_id"], spuId="1002")

    assert again["ticketNo"] == first["ticketNo"], "同一对象仍然是那一条"
    assert again["context"]["spuId"] == "1002", "后一次点击为准"
    assert again["context"]["orderMainNo"] == "M1", "没带的那几项不该被抹掉"
