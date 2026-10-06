"""review 模块的集成测试。连真实 PostgreSQL 与 Redis。

评价的精髓是**"购后限制"与"唯一性"**（docs/01 §2 给这个模块标的两条关键难点），
所以这里重点验证四件事：

1. **购后限制**：没买过、没收到、退光了、售后中，都不能评
2. **唯一性**：每个订单项只能一条首评，一条首评只能一次追评（含并发）
3. **统计与状态成对**：发布 +1、屏蔽 −1、解除 +1、驳回不动
4. **★ 部分退款后剩余商品仍可评价** —— 这条最容易做错，因为部分退款会把子单
   推到 70「已退款」，用 `status == FINISHED` 兜就会误杀
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.db import get_session_factory
from app.core.enums import ReviewStatus
from app.core.redis import get_redis
from app.modules.review import service as review_service
from app.modules.review.models import DAILY_REVIEW_LIMIT
from tests.conftest import auth_header, make_admin, register
from tests.test_aftersale import _finished_order, _item_id, _unshipped_order
from tests.test_trade import _create_order, _prepare

BUYER2_PHONE = "13900139051"
# _prepare() 内部已经建过一个管理员（默认号 13900139001），
# 这里换一个号，否则会撞"手机号已注册"
ADMIN_PHONE = "13900139099"


async def _admin(client: AsyncClient, session) -> dict:
    return await make_admin(client, session, phone=ADMIN_PHONE)


@pytest.fixture(autouse=True)
async def clean_redis(app_runtime: None) -> AsyncIterator[None]:
    """清库存、券与评价限流的 Redis key（DB 由 conftest 的 clean_tables 清）。"""
    redis = get_redis()
    keys: list[str] = []
    for pattern in ("stock:*", "lock:stock_init:*", "coupon:*", "review:*"):
        async for key in redis.scan_iter(match=pattern):
            keys.append(key)
    if keys:
        await redis.delete(*keys)
    yield


# ============================================================
# 造数据与读取
# ============================================================
async def _spu_id_of(sku_id: int) -> int:
    async with get_session_factory()() as s:
        return int(await s.scalar(text("SELECT spu_id FROM product.sku WHERE id = :k"), {"k": sku_id}))


async def _spu_stats(spu_id: int) -> tuple[int, int, int, float | None]:
    """直接读 product.spu 的四个统计字段（绕过接口，验证落库值）。"""
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT review_count, review_score_sum, good_review_count, avg_score"
                    " FROM product.spu WHERE id = :i"
                ),
                {"i": spu_id},
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2]), (float(row[3]) if row[3] is not None else None)


async def _review_status(review_id: int) -> int:
    async with get_session_factory()() as s:
        return int(await s.scalar(text("SELECT status FROM review.review WHERE id = :i"), {"i": review_id}))


async def _submit(
    client: AsyncClient,
    ctx: dict,
    *,
    item_id: str,
    score: int = 5,
    content: str | None = "质量很好，物流也快",
    images: list[str] | None = None,
    anonymous: bool = False,
    idem: str = "rev-1",
    expect: int = 200,
) -> dict:
    resp = await client.post(
        "/api/reviews",
        json={
            "orderItemId": item_id,
            "score": score,
            "content": content,
            "images": images or [],
            "anonymous": anonymous,
        },
        headers={**ctx["buyer_headers"], "Idempotency-Key": idem},
    )
    assert resp.status_code == expect, resp.text
    return resp.json()["data"] if expect == 200 else {"code": resp.json()["code"]}


async def _finished_item(client: AsyncClient, ctx: dict, *, num: int = 1, idem: str = "f-1") -> tuple[dict, str]:
    """造一笔已完成的订单，返回 (订单, 订单项 id)。"""
    order = await _finished_order(client, ctx, num=num, idem=idem)
    return order, await _item_id(order)


# ============================================================
# ① 完整链路
# ============================================================
async def test_submit_full_chain(client: AsyncClient, session) -> None:
    """已完成的订单 → 待评价里出现 → 提交 → 统计 +1 → 商品详情页能看到。"""
    ctx = await _prepare(client, session)
    _order, item_id = await _finished_item(client, ctx, idem="chain-1")
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))

    # ① 待评价列表里有它
    resp = await client.get("/api/reviews/pending", headers=ctx["buyer_headers"])
    assert resp.status_code == 200, resp.text
    pending = resp.json()["data"]["items"]
    assert [i["orderItemId"] for i in pending] == [item_id]

    # ② 资格预检通过，并带上商品快照
    resp = await client.post(
        "/api/reviews/eligibility",
        json={"orderItemId": item_id},
        headers=ctx["buyer_headers"],
    )
    elig = resp.json()["data"]
    assert elig["eligible"] is True
    assert elig["title"]
    assert elig["num"] == 1

    # ③ 提交
    review = await _submit(client, ctx, item_id=item_id, idem="chain-2")
    assert review["score"] == 5
    assert review["status"] == 1, "机审没命中敏感词，应当直接发布"
    assert review["statusText"] == "已发布"
    assert review["nickname"].endswith("***"), "昵称要脱敏"

    # ④ 统计 +1（同一事务内更新的，不该有延迟）
    count, score_sum, good, avg = await _spu_stats(spu_id)
    assert (count, score_sum, good, avg) == (1, 5, 1, 5.0)

    # ⑤ 已评过 → 从待评价里消失
    resp = await client.get("/api/reviews/pending", headers=ctx["buyer_headers"])
    assert resp.json()["data"]["items"] == []

    # ⑥ 商品详情页的评价列表（**公开接口，不带 token**）
    resp = await client.get(f"/api/spus/{spu_id}/reviews")
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["reviewId"] == review["reviewId"]

    # ⑦ 评分汇总
    resp = await client.get(f"/api/spus/{spu_id}/review-stats")
    stats = resp.json()["data"]
    assert stats["reviewCount"] == 1
    assert stats["avgScore"] == 5.0
    assert stats["goodRate"] == 1.0
    assert stats["scoreDistribution"]["5"] == 1


async def test_follow_up_appears_under_first_review(client: AsyncClient, session) -> None:
    """追评：首评后 30 天内一次，展示上紧随首评。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="fu-1")
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))
    review = await _submit(client, ctx, item_id=item_id, idem="fu-2")

    resp = await client.post(
        f"/api/reviews/{review['reviewId']}/follow-up",
        json={"content": "用了两周，依然很好"},
        headers=ctx["buyer_headers"],
    )
    assert resp.status_code == 200, resp.text
    follow = resp.json()["data"]
    assert follow["isFollowUp"] is True

    # ★ 追评**不计入统计**（统计口径是"已发布的首评"）
    count, score_sum, _good, _avg = await _spu_stats(spu_id)
    assert (count, score_sum) == (1, 5)

    # 列表里追评挂在首评下面
    resp = await client.get(f"/api/spus/{spu_id}/reviews")
    items = resp.json()["data"]["items"]
    assert len(items) == 1, "追评不该单独占一行"
    assert items[0]["followUp"]["content"] == "用了两周，依然很好"


async def test_follow_up_twice_rejected(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="fu3-1")
    review = await _submit(client, ctx, item_id=item_id, idem="fu3-2")

    first = await client.post(
        f"/api/reviews/{review['reviewId']}/follow-up",
        json={"content": "第一次追评"},
        headers=ctx["buyer_headers"],
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/reviews/{review['reviewId']}/follow-up",
        json={"content": "第二次追评"},
        headers=ctx["buyer_headers"],
    )
    assert second.status_code == 422
    assert second.json()["code"] == "ALREADY_FOLLOWED_UP"


# ============================================================
# ② 购后限制
# ============================================================
async def test_cannot_review_others_order(client: AsyncClient, session) -> None:
    """★ 别人的订单项：返回"不存在"，不区分"不存在"与"不是你的"（防探测）。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="oth-1")

    other = await register(client, phone=BUYER2_PHONE)
    resp = await client.post(
        "/api/reviews/eligibility",
        json={"orderItemId": item_id},
        headers=auth_header(other["accessToken"]),
    )
    data = resp.json()["data"]
    assert data["eligible"] is False
    assert data["reason"] == "ORDER_ITEM_NOT_FOUND"


async def test_cannot_review_unpaid_order(client: AsyncClient, session) -> None:
    """待付款：订单还没成交。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx, idem="unp-1")
    item_id = await _item_id(order)

    resp = await client.post(
        "/api/reviews/eligibility",
        json={"orderItemId": item_id},
        headers=ctx["buyer_headers"],
    )
    assert resp.json()["data"]["reason"] in ("ORDER_NOT_FINISHED", "ORDER_CLOSED")


async def test_cannot_review_before_receiving(client: AsyncClient, session) -> None:
    """★ 待收货不能评价 —— 还没拿到货，谈不上体验。"""
    ctx = await _prepare(client, session)
    order = await _unshipped_order(client, ctx, idem="wtr-1")
    sub_no = order["subs"][0]["orderSubNo"]
    await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": "SF-WTR"},
        headers=ctx["merchant_headers"],
    )
    item_id = await _item_id(order)

    resp = await client.post(
        "/api/reviews/eligibility",
        json={"orderItemId": item_id},
        headers=ctx["buyer_headers"],
    )
    assert resp.json()["data"]["reason"] == "NOT_RECEIVED"


async def test_cannot_review_after_window(client: AsyncClient, session) -> None:
    """签收超过 30 天：窗口已过。"""
    ctx = await _prepare(client, session)
    order, item_id = await _finished_item(client, ctx, idem="win-1")

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE trade.order_sub SET receive_time = now() - interval '40 days'"
                " WHERE order_sub_no = :n"
            ),
            {"n": order["subs"][0]["orderSubNo"]},
        )

    resp = await client.post(
        "/api/reviews/eligibility",
        json={"orderItemId": item_id},
        headers=ctx["buyer_headers"],
    )
    assert resp.json()["data"]["reason"] == "REVIEW_EXPIRED"


async def test_cannot_review_fully_refunded_item(client: AsyncClient, session) -> None:
    """整件退掉了：商品已经不是他的了。"""
    ctx = await _prepare(client, session)
    _order, item_id = await _finished_item(client, ctx, idem="ref-1")

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE trade.order_item SET refunded_num = num WHERE id = :i"
            ),
            {"i": int(item_id)},
        )

    resp = await client.post(
        "/api/reviews/eligibility",
        json={"orderItemId": item_id},
        headers=ctx["buyer_headers"],
    )
    assert resp.json()["data"]["reason"] == "ITEM_REFUNDED"


async def test_cannot_review_item_in_aftersale(client: AsyncClient, session) -> None:
    """该订单项正在售后中：等售后有结论再说。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="inaf-1")

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE trade.order_item SET refunding_num = 1 WHERE id = :i"),
            {"i": int(item_id)},
        )

    resp = await client.post(
        "/api/reviews/eligibility",
        json={"orderItemId": item_id},
        headers=ctx["buyer_headers"],
    )
    assert resp.json()["data"]["reason"] == "IN_AFTERSALE"


async def test_partially_refunded_item_is_still_reviewable(client: AsyncClient, session) -> None:
    """★ 部分退款后剩余商品仍可评价（docs/12 §9）。

    这条专门钉住一个坑：**部分退款会把子单推到 70「已退款」**，
    如果资格判定用 `sub.status == FINISHED` 兜，剩余商品就永远评不了。
    所以判定必须看**订单项级**的退款数量。
    """
    ctx = await _prepare(client, session)
    order, item_id = await _finished_item(client, ctx, num=3, idem="part-1")
    sub_no = order["subs"][0]["orderSubNo"]

    # 造出"子单已退款、但这一项只退了 1 件"的状态
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE trade.order_item SET refunded_num = 1 WHERE id = :i"),
            {"i": int(item_id)},
        )
        await s.execute(
            text("UPDATE trade.order_sub SET status = 70 WHERE order_sub_no = :n"),
            {"n": sub_no},
        )

    resp = await client.post(
        "/api/reviews/eligibility",
        json={"orderItemId": item_id},
        headers=ctx["buyer_headers"],
    )
    data = resp.json()["data"]
    assert data["eligible"] is True, (
        "部分退款后剩余商品必须仍可评价（子单此时是 70「已退款」）"
    )

    # 而且真能提交成功
    review = await _submit(client, ctx, item_id=item_id, idem="part-2")
    assert review["reviewId"]


async def test_pending_list_includes_partially_refunded(client: AsyncClient, session) -> None:
    """待评价列表同样不能用子单状态兜 —— 部分退款的也要出现。"""
    ctx = await _prepare(client, session)
    order, item_id = await _finished_item(client, ctx, num=2, idem="pl-1")
    sub_no = order["subs"][0]["orderSubNo"]

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE trade.order_item SET refunded_num = 1 WHERE id = :i"),
            {"i": int(item_id)},
        )
        await s.execute(
            text("UPDATE trade.order_sub SET status = 70 WHERE order_sub_no = :n"),
            {"n": sub_no},
        )

    resp = await client.get("/api/reviews/pending", headers=ctx["buyer_headers"])
    assert ctx["sku_ids"][0] and resp.status_code == 200
    assert item_id in [i["orderItemId"] for i in resp.json()["data"]["items"]]


# ============================================================
# ③ 唯一性
# ============================================================
async def test_cannot_review_twice(client: AsyncClient, session) -> None:
    """★ 同一订单项只能评一次 —— 第二次数唯一索引拦下，给友好提示而不是 500。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="twice-1")
    await _submit(client, ctx, item_id=item_id, idem="twice-2")

    second = await _submit(client, ctx, item_id=item_id, idem="twice-3", expect=422)
    assert second["code"] == "ALREADY_REVIEWED"


async def test_concurrent_submit_only_one_wins(client: AsyncClient, session) -> None:
    """并发提交同一订单项：唯一索引保证只有一个成功。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="cc-1")

    async def _one(tag: str) -> str:
        resp = await client.post(
            "/api/reviews",
            json={"orderItemId": item_id, "score": 5, "content": "很好"},
            headers={**ctx["buyer_headers"], "Idempotency-Key": tag},
        )
        return "OK" if resp.status_code == 200 else resp.json().get("code", str(resp.status_code))

    results = await asyncio.gather(_one("cc-a"), _one("cc-b"))
    assert sorted(results) == ["ALREADY_REVIEWED", "OK"], results

    async with get_session_factory()() as s:
        count = await s.scalar(
            text("SELECT count(*) FROM review.review WHERE order_item_id = :i"),
            {"i": int(item_id)},
        )
    assert count == 1


async def test_buy_three_times_review_three_times(client: AsyncClient, session) -> None:
    """★ 买三次能评三次 —— 评论的粒度是**订单项**，不是商品。

    `unique(user_id, spu_id)` 那种做法会把"第二次购买是新的体验"也拦掉。
    """
    ctx = await _prepare(client, session)
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))

    for i in range(3):
        _, item_id = await _finished_item(client, ctx, num=1, idem=f"three-{i}")
        review = await _submit(client, ctx, item_id=item_id, score=4, idem=f"three-rev-{i}")
        assert review["reviewId"]

    count, score_sum, good, avg = await _spu_stats(spu_id)
    assert (count, score_sum, good) == (3, 12, 3)
    assert avg == 4.0


# ============================================================
# ④ 审核：状态与统计成对
# ============================================================
async def test_sensitive_word_goes_to_audit_then_publishes(client: AsyncClient, session) -> None:
    """★ 命中敏感词 → 待审核（不计统计）→ 运营通过 → 这时才 +1。"""
    ctx = await _prepare(client, session)
    admin = await _admin(client, session)
    _, item_id = await _finished_item(client, ctx, idem="aud-1")
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))

    review = await _submit(
        client, ctx, item_id=item_id, content="好评返现，加微信 领取红包", idem="aud-2"
    )
    assert review["status"] == 0, "命中引流词要转待审核"
    assert review["statusText"] == "待审核"

    # 待审核不该计入统计
    assert await _spu_stats(spu_id) == (0, 0, 0, None)

    # 运营通过 → 这一刻才 +1
    resp = await client.post(
        f"/api/admin/reviews/{review['reviewId']}/audit",
        json={"action": "APPROVE", "remark": "人工看过，没问题"},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    assert await _spu_stats(spu_id) == (1, 5, 1, 5.0)
    assert await _review_status(int(review["reviewId"])) == 1


async def test_block_and_unblock_move_stats(client: AsyncClient, session) -> None:
    """★ 屏蔽 −1、解除 +1 —— 状态与统计必须成对。"""
    ctx = await _prepare(client, session)
    admin = await _admin(client, session)
    _, item_id = await _finished_item(client, ctx, idem="blk-1")
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))
    review = await _submit(client, ctx, item_id=item_id, idem="blk-2")
    assert await _spu_stats(spu_id) == (1, 5, 1, 5.0)

    resp = await client.post(
        f"/api/admin/reviews/{review['reviewId']}/audit",
        json={"action": "BLOCK", "remark": "含违规内容"},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    count, score_sum, good, avg = await _spu_stats(spu_id)
    assert (count, score_sum, good) == (0, 0, 0), "屏蔽后要减回去"
    assert avg is None, "计数归零后平均分必须是 NULL"

    resp = await client.post(
        f"/api/admin/reviews/{review['reviewId']}/audit",
        json={"action": "UNBLOCK"},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    assert await _spu_stats(spu_id) == (1, 5, 1, 5.0), "解除屏蔽要加回来"


async def test_reject_does_not_touch_stats(client: AsyncClient, session) -> None:
    """驳回：从没计过，所以也不该减。"""
    ctx = await _prepare(client, session)
    admin = await _admin(client, session)
    _, item_id = await _finished_item(client, ctx, idem="rej-1")
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))
    review = await _submit(client, ctx, item_id=item_id, content="加微信 看货", idem="rej-2")
    assert review["status"] == 0

    resp = await client.post(
        f"/api/admin/reviews/{review['reviewId']}/audit",
        json={"action": "REJECT", "remark": "广告"},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    assert await _review_status(int(review["reviewId"])) == 3
    assert await _spu_stats(spu_id) == (0, 0, 0, None)

    # 审核不通过是终态，不能再改成通过
    again = await client.post(
        f"/api/admin/reviews/{review['reviewId']}/audit",
        json={"action": "APPROVE"},
        headers=auth_header(admin["accessToken"]),
    )
    assert again.status_code == 422


async def test_audit_requires_admin_role(client: AsyncClient, session) -> None:
    """普通买家不能处置评价。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="perm-1")
    review = await _submit(client, ctx, item_id=item_id, idem="perm-2")

    resp = await client.post(
        f"/api/admin/reviews/{review['reviewId']}/audit",
        json={"action": "BLOCK"},
        headers=ctx["buyer_headers"],
    )
    assert resp.status_code == 403


async def test_audit_queue_shows_pending(client: AsyncClient, session) -> None:
    """审核队列里能看到待审核的那条。"""
    ctx = await _prepare(client, session)
    admin = await _admin(client, session)
    _, item_id = await _finished_item(client, ctx, idem="q-1")
    review = await _submit(client, ctx, item_id=item_id, content="私聊我 有优惠", idem="q-2")

    resp = await client.get(
        "/api/admin/reviews/audit-queue", headers=auth_header(admin["accessToken"])
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert review["reviewId"] in [i["review"]["reviewId"] for i in data["items"]]
    assert data["pendingCount"] >= 1
    assert data["items"][0]["reasonText"] == "机审命中高风险词"


async def test_audit_queue_shows_second_audit_items(client: AsyncClient, session) -> None:
    """★ 抽检队列查的是"机审放行（已发布）但标了待抽检"的那批。

    原来这里把 ``status=待审核``（参数默认值）和 ``need_second_audit`` 叠在一起查，
    而 ``need_second_audit`` 只在**放行**的行上为 true（``machine_audit``），
    待审核的行必然是 false —— 两者交集恒为空，抽检视图**永远是空的**，
    哪怕库里堆着一批等抽查的；角标也一样恒为 0（它在"待审核"那一页里数已发布的行）。
    """
    ctx = await _prepare(client, session)
    admin = await _admin(client, session)
    ah = auth_header(admin["accessToken"])
    _, item_id = await _finished_item(client, ctx, idem="q-10")
    # 内容过机审 → 直接发布 + 标记待抽检
    review = await _submit(client, ctx, item_id=item_id, content="质量很好，包装扎实", idem="q-11")

    resp = await client.get(
        "/api/admin/reviews/audit-queue", params={"secondAuditOnly": "true"}, headers=ah
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert review["reviewId"] in [i["review"]["reviewId"] for i in data["items"]]
    assert data["items"][0]["reasonText"] == "机审放行，待抽检"
    assert data["secondAuditCount"] >= 1

    # ★ 两个视图互不相交：待审核视图里不该有它
    pending = (await client.get("/api/admin/reviews/audit-queue", headers=ah)).json()["data"]
    assert review["reviewId"] not in [i["review"]["reviewId"] for i in pending["items"]]


async def test_second_audit_pass_removes_it_from_queue(client: AsyncClient, session) -> None:
    """★ 抽检处置过就出队 ——「通过」不改状态，只清掉待抽检标记。

    不这么做的话，被抽检过但没问题的评价会永远赖在抽检队列里（队列只增不减）。
    """
    ctx = await _prepare(client, session)
    admin = await _admin(client, session)
    ah = auth_header(admin["accessToken"])
    _, item_id = await _finished_item(client, ctx, idem="q-20")
    review = await _submit(client, ctx, item_id=item_id, content="不错，会回购", idem="q-21")

    resp = await client.post(
        f"/api/admin/reviews/{review['reviewId']}/audit",
        json={"action": "APPROVE"},
        headers=ah,
    )
    assert resp.status_code == 200, resp.text

    data = (
        await client.get(
            "/api/admin/reviews/audit-queue", params={"secondAuditOnly": "true"}, headers=ah
        )
    ).json()["data"]
    assert review["reviewId"] not in [i["review"]["reviewId"] for i in data["items"]]
    assert data["secondAuditCount"] == 0

    # 状态仍是「已发布」—— 抽检通过不该把它撤下来
    async with get_session_factory()() as s:
        status = await s.scalar(
            text("SELECT status FROM review.review WHERE id = :i"), {"i": int(review["reviewId"])}
        )
    assert int(status) == int(ReviewStatus.PUBLISHED)


# ============================================================
# ⑤ 零评价的展示
# ============================================================
async def test_zero_reviews_returns_null_not_zero(client: AsyncClient, session) -> None:
    """★ 没有评价时平均分与好评率都是 null —— 不能显示成 5.0 也不能显示成 0。"""
    ctx = await _prepare(client, session)
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))

    assert await _spu_stats(spu_id) == (0, 0, 0, None)

    resp = await client.get(f"/api/spus/{spu_id}/review-stats")
    stats = resp.json()["data"]
    assert stats["reviewCount"] == 0
    assert stats["avgScore"] is None
    assert stats["goodRate"] is None
    assert stats["scoreDistribution"] == {}


async def test_product_card_has_null_avg_score(client: AsyncClient, session) -> None:
    """商品列表/详情里的评分也必须是 null（原来是 NOT NULL DEFAULT 5.00 的坑）。"""
    ctx = await _prepare(client, session)
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))

    # 列表卡片上带评分（详情页的评分走专门的 /review-stats）
    resp = await client.get("/api/search")
    assert resp.status_code == 200, resp.text
    card = next(s for s in resp.json()["data"]["items"] if s["id"] == str(spu_id))
    assert card["avgScore"] is None, "零评价的卡片不能显示成 5.0 或 0"
    assert card["reviewCount"] == 0


# ============================================================
# ⑥ 匿名与排序
# ============================================================
async def test_anonymous_hides_nickname(client: AsyncClient, session) -> None:
    """匿名评价：服务端替换昵称与头像，**不能指望前端隐藏**。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="anon-1")
    await _submit(client, ctx, item_id=item_id, anonymous=True, idem="anon-2")

    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))
    resp = await client.get(f"/api/spus/{spu_id}/reviews")
    item = resp.json()["data"]["items"][0]
    assert item["anonymous"] is True
    assert item["nickname"] == "匿名用户"
    assert item["avatar"] is None


async def test_recommend_sort_puts_content_first(client: AsyncClient, session) -> None:
    """推荐排序：有内容有图的排在前，纯打星的沉底。"""
    ctx = await _prepare(client, session)
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))

    # 先发一条只有星级的（用第三笔订单），再发一条有内容有图的
    _, bare_item = await _finished_item(client, ctx, idem="sort-1")
    await _submit(client, ctx, item_id=bare_item, content=None, idem="sort-2")

    _, rich_item = await _finished_item(client, ctx, idem="sort-3")
    await _submit(
        client,
        ctx,
        item_id=rich_item,
        content="非常详细的使用体验，值得购买",
        images=[f"reviews/{ctx['buyer_id']}/a.webp"],
        idem="sort-4",
    )

    resp = await client.get(f"/api/spus/{spu_id}/reviews?sort=recommend")
    items = resp.json()["data"]["items"]
    assert items[0]["content"], "有内容的应当排在前面"
    assert items[-1]["content"] is None


async def test_filter_good_and_with_image(client: AsyncClient, session) -> None:
    """按评分筛选与按有图筛选。"""
    ctx = await _prepare(client, session)
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))

    _, bad_item = await _finished_item(client, ctx, idem="flt-1")
    await _submit(client, ctx, item_id=bad_item, score=2, content="不太满意", idem="flt-2")

    _, good_item = await _finished_item(client, ctx, idem="flt-3")
    await _submit(
        client,
        ctx,
        item_id=good_item,
        score=5,
        content="很满意",
        images=[f"reviews/{ctx['buyer_id']}/b.webp"],
        idem="flt-4",
    )

    resp = await client.get(f"/api/spus/{spu_id}/reviews?filter=good")
    assert [i["score"] for i in resp.json()["data"]["items"]] == [5]

    resp = await client.get(f"/api/spus/{spu_id}/reviews?filter=with_image")
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["images"], "有图的那条要带图片"


async def test_image_urls_carry_thumb(client: AsyncClient, session) -> None:
    """图片 DTO 要同时给出原图与缩略图 URL（库里只存相对路径）。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="img-1")
    await _submit(
        client,
        ctx,
        item_id=item_id,
        images=[f"reviews/{ctx['buyer_id']}/abc.webp"],
        idem="img-2",
    )

    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))
    resp = await client.get(f"/api/spus/{spu_id}/reviews")
    image = resp.json()["data"]["items"][0]["images"][0]
    assert image["path"] == f"reviews/{ctx['buyer_id']}/abc.webp"
    assert image["url"].endswith("/abc.webp")
    assert image["thumbUrl"].endswith("/abc_t.webp")


# ============================================================
# ⑦ 商家回复
# ============================================================
async def test_merchant_reply_limit(client: AsyncClient, session) -> None:
    """★ 一条评价最多 3 条商家回复，第 4 条被拒。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="rep-1")
    review = await _submit(client, ctx, item_id=item_id, idem="rep-2")

    for i in range(3):
        resp = await client.post(
            f"/api/merchant/reviews/{review['reviewId']}/reply",
            json={"content": f"感谢反馈 {i}"},
            headers=ctx["merchant_headers"],
        )
        assert resp.status_code == 200, resp.text

    fourth = await client.post(
        f"/api/merchant/reviews/{review['reviewId']}/reply",
        json={"content": "再多说一句"},
        headers=ctx["merchant_headers"],
    )
    assert fourth.status_code == 422
    assert fourth.json()["code"] == "REPLY_LIMIT_EXCEEDED"

    # 回复展示在评价下面
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))
    resp = await client.get(f"/api/spus/{spu_id}/reviews")
    replies = resp.json()["data"]["items"][0]["replies"]
    assert len(replies) == 3
    assert replies[0]["replyTypeText"] == "商家回复"


async def test_merchant_cannot_reply_other_shop_review(client: AsyncClient, session) -> None:
    """★ 别人的店铺的评价：返回"不存在"，不泄露评价是否存在。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="own-1")
    review = await _submit(client, ctx, item_id=item_id, idem="own-2")

    other = await register(client, phone="13800138061")
    from tests.conftest import open_shop

    await open_shop(client, other["accessToken"], "另一家店")
    resp = await client.post(
        f"/api/merchant/reviews/{review['reviewId']}/reply",
        json={"content": "蹭一条"},
        headers=auth_header(other["accessToken"]),
    )
    assert resp.status_code == 404

    async with get_session_factory()() as s:
        assert (
            await s.scalar(text("SELECT count(*) FROM review.review_reply"))
        ) == 0, "不该写进去"


async def test_merchant_list_sees_own_shop_reviews(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="ml-1")
    review = await _submit(client, ctx, item_id=item_id, idem="ml-2")

    resp = await client.get("/api/merchant/reviews", headers=ctx["merchant_headers"])
    assert resp.status_code == 200, resp.text
    assert review["reviewId"] in [i["reviewId"] for i in resp.json()["data"]["items"]]


# ============================================================
# ⑧ 反刷与统计重算
# ============================================================
async def test_daily_limit_rejects(client: AsyncClient, session) -> None:
    """单用户当日评价数达上限后拒绝（Redis 计数）。"""
    ctx = await _prepare(client, session)
    redis = get_redis()
    from app.core.redis_keys import review_daily

    key = review_daily(ctx["buyer_id"], datetime.now(UTC).strftime("%Y%m%d"))
    await redis.set(key, DAILY_REVIEW_LIMIT)

    _, item_id = await _finished_item(client, ctx, idem="dl-1")
    resp = await client.post(
        "/api/reviews",
        json={"orderItemId": item_id, "score": 5, "content": "很好"},
        headers={**ctx["buyer_headers"], "Idempotency-Key": "dl-2"},
    )
    assert resp.status_code == 429
    assert resp.json()["code"] == "RATE_LIMITED"


async def test_review_still_works_when_redis_is_down(client: AsyncClient, session, monkeypatch) -> None:
    """★ Redis 挂了必须放行 —— 评价是业务数据，Redis 只是限流器。

    这与库存/券不同（那些 Redis 是正确性闸门）。不能让缓存故障阻断评价。
    """
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="rd-1")

    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(review_service, "get_redis", lambda: _Boom())

    review = await _submit(client, ctx, item_id=item_id, idem="rd-2")
    assert review["reviewId"]


async def test_recompute_fixes_drift(client: AsyncClient, session) -> None:
    """★ 手工把统计改歪 → 跑重算 → 归位 + 写告警。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="rc-1")
    await _submit(client, ctx, item_id=item_id, score=4, idem="rc-2")
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))
    assert await _spu_stats(spu_id) == (1, 4, 1, 4.0)

    # 把计数改歪（模拟"某处漏调了 delta"）
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE product.spu SET review_count = 99, review_score_sum = 111,"
                " good_review_count = 42 WHERE id = :i"
            ),
            {"i": spu_id},
        )

    from app.modules.review.tasks import recompute_spu_stats

    result = await recompute_spu_stats({"session_factory": get_session_factory()})
    assert result["fixed"] == 1
    assert await _spu_stats(spu_id) == (1, 4, 1, 4.0)

    async with get_session_factory()() as s:
        level = await s.scalar(
            text("SELECT max(level) FROM ops.alert WHERE source = 'review.stats_reconcile'")
        )
    assert level == 2, "统计漂移要写告警"


async def test_recompute_zeroes_orphan_counts(client: AsyncClient, session) -> None:
    """★ 没有任何已发布首评、计数却非零的商品要归零。

    这是最危险的一类漂移（评价全被屏蔽后没减回去），
    而"按 spu 聚合再更新"的 SQL 覆盖不到它 —— 必须有单独一条归零 SQL。
    """
    ctx = await _prepare(client, session)
    spu_id = await _spu_id_of(int(ctx["sku_ids"][0]))

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE product.spu SET review_count = 7, review_score_sum = 35 WHERE id = :i"),
            {"i": spu_id},
        )

    from app.modules.review.tasks import recompute_spu_stats

    result = await recompute_spu_stats({"session_factory": get_session_factory()})
    assert result["zeroed"] == 1
    assert await _spu_stats(spu_id) == (0, 0, 0, None)


# ============================================================
# ⑨ 我的评价
# ============================================================
async def test_my_reviews_lists_both_first_and_follow_up(client: AsyncClient, session) -> None:
    """我的评价里首评与追评都要出现（用户要能看到自己发过的全部内容）。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="mine-1")
    review = await _submit(client, ctx, item_id=item_id, idem="mine-2")
    await client.post(
        f"/api/reviews/{review['reviewId']}/follow-up",
        json={"content": "追一条"},
        headers=ctx["buyer_headers"],
    )

    resp = await client.get("/api/reviews/mine", headers=ctx["buyer_headers"])
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 2
    assert {i["isFollowUp"] for i in items} == {True, False}


async def test_suspect_flag_for_instant_review(client: AsyncClient, session) -> None:
    """★ 签收后 1 分钟内评价 → 标记 suspect（降权 + 进抽检）。

    正常用户至少要看一眼货。这里把签收时间改到 1 秒前再提交。
    """
    ctx = await _prepare(client, session)
    order, item_id = await _finished_item(client, ctx, idem="susp-1")

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE trade.order_sub SET receive_time = now() - interval '5 seconds'"
                " WHERE order_sub_no = :n"
            ),
            {"n": order["subs"][0]["orderSubNo"]},
        )

    await _submit(client, ctx, item_id=item_id, content="好", idem="susp-2")
    async with get_session_factory()() as s:
        suspect = await s.scalar(
            text("SELECT suspect FROM review.review WHERE order_item_id = :i"),
            {"i": int(item_id)},
        )
    assert suspect is True


async def test_image_path_must_be_own_directory(client: AsyncClient, session) -> None:
    """★ 图片路径必须是自己的目录，否则可以把别人的图塞进自己的评价。"""
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="imgp-1")

    resp = await client.post(
        "/api/reviews",
        json={
            "orderItemId": item_id,
            "score": 5,
            "content": "好",
            "images": ["reviews/999999/other.webp"],
        },
        headers={**ctx["buyer_headers"], "Idempotency-Key": "imgp-2"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_submit_requires_idempotency_key(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    _, item_id = await _finished_item(client, ctx, idem="idem-1")
    resp = await client.post(
        "/api/reviews",
        json={"orderItemId": item_id, "score": 5},
        headers=ctx["buyer_headers"],
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
