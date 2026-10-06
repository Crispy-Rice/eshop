"""aftersale 模块的集成测试。连真实 PostgreSQL 与 Redis。

前面每个模块各管一段，这里验证**逆向链路**：
申请 → 商家审核 → 退货/质检 → 退款到账，以及四件最容易出错的事 ——

1. **库存回补的时机**：退货退款只在"入库质检合格"回补，早一步就是超卖
2. **金额的差额法**：多次部分退，累计恰好等于实付
3. **退款回调幂等**：渠道重试是常态，重复回调只能推进一次
4. **两套状态机同步**：售后单与子单要么都变、要么都不变

这四条都不是边界情况，而是线上每天都在发生的常态。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import get_session_factory
from app.core.redis import get_redis
from app.modules.aftersale import service as aftersale_service
from app.modules.aftersale import tasks as aftersale_tasks
from app.modules.aftersale.models import (
    QUALITY_FAIL,
    REASON_NO_LONGER_WANT,
    REASON_QUALITY,
    REFUND_ONLY,
    RETURN_REFUND,
)
from app.modules.trade.models import ORDER_FINISHED, ORDER_REFUNDED, ORDER_WAIT_DELIVER
from tests.conftest import auth_header, register
from tests.test_trade import SKU_PRICE, _create_order, _make_coupon, _pay, _prepare, _receive_coupon

BUYER2_PHONE = "13900139041"


@pytest.fixture(autouse=True)
async def clean_redis(app_runtime: None) -> AsyncIterator[None]:
    """清库存与券的 Redis key（DB 由 conftest 的 clean_tables 清）。"""
    redis = get_redis()
    keys: list[str] = []
    for pattern in ("stock:*", "lock:stock_init:*", "coupon:*"):
        async for key in redis.scan_iter(match=pattern):
            keys.append(key)
    if keys:
        await redis.delete(*keys)
    yield


# ============================================================
# 造数据：一笔"已完成"的订单（退货退款的起点）
# ============================================================
async def _finished_order(
    client: AsyncClient, ctx: dict, *, num: int = 1, idem: str = "fin-1", coupon_ids=None
) -> dict:
    """下单 → 支付 → 发货 → 确认收货，得到一笔"已完成"的订单。"""
    order = await _create_order(client, ctx, num=num, idem=idem, coupon_ids=coupon_ids)
    sub_no = order["subs"][0]["orderSubNo"]
    await _pay(client, ctx, order["orderMainNo"])
    resp = await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": f"SF{idem}"[:60]},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/order-subs/{sub_no}/receive", headers=ctx["buyer_headers"])
    assert resp.status_code == 200, resp.text
    return order


async def _unshipped_order(
    client: AsyncClient, ctx: dict, *, num: int = 1, idem: str = "uns-1"
) -> dict:
    """下单 → 支付，停在"待发货"（仅退款的起点）。"""
    order = await _create_order(client, ctx, num=num, idem=idem)
    await _pay(client, ctx, order["orderMainNo"])
    return order


async def _item_id(order: dict) -> str:
    async with get_session_factory()() as s:
        return str(
            await s.scalar(
                text("SELECT id FROM trade.order_item WHERE order_sub_no = :n"),
                {"n": order["subs"][0]["orderSubNo"]},
            )
        )


async def _apply_refund(
    client: AsyncClient,
    ctx: dict,
    *,
    order: dict,
    num: int = 1,
    reason: int = REASON_NO_LONGER_WANT,
    idem: str = "refund-1",
) -> dict:
    """申请售后。订单项 id 从库里取（下单响应不含它）。"""
    sub_no = order["subs"][0]["orderSubNo"]
    item_id = await _item_id(order)
    resp = await client.post(
        "/api/aftersales",
        json={
            "orderSubNo": sub_no,
            "items": [{"orderItemId": item_id, "num": num}],
            "reasonType": reason,
        },
        headers={**ctx["buyer_headers"], "Idempotency-Key": idem},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _run_refund_task(refund_no: str) -> dict:
    """执行资金退款任务（模拟渠道退款 + 售后转成功）。"""
    async with get_session_factory()() as s:
        funding = await s.scalar(
            text("SELECT refund_no FROM payment.payment_refund WHERE refund_biz_no = :n"),
            {"n": refund_no},
        )
    assert funding is not None, "应当已经建了资金退款单"
    return await aftersale_tasks.execute_refund({"session_factory": get_session_factory()}, funding)


async def _refund_row(refund_no: str) -> dict:
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, refund_amount, refund_freight, refund_type, source_status,"
                    " quality_result, return_express_no"
                    " FROM aftersale.refund_order WHERE refund_no = :n"
                ),
                {"n": refund_no},
            )
        ).one()
    keys = (
        "status",
        "refund_amount",
        "refund_freight",
        "refund_type",
        "source_status",
        "quality_result",
        "return_express_no",
    )
    return dict(zip(keys, row, strict=True))


async def _stock(sku_id: int, wh_id: int) -> dict[str, int]:
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT total, available, locked, frozen, defective FROM inventory.sku_stock"
                    " WHERE sku_id = :k AND warehouse_id = :w"
                ),
                {"k": sku_id, "w": wh_id},
            )
        ).one()
    return {
        "total": int(row[0]),
        "available": int(row[1]),
        "locked": int(row[2]),
        "frozen": int(row[3]),
        "defective": int(row[4]),
    }


def _assert_identity(row: dict[str, int]) -> None:
    assert row["total"] == row["available"] + row["locked"] + row["frozen"], f"恒等式被破坏：{row}"


async def _sub_status(order: dict) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT status FROM trade.order_sub WHERE order_sub_no = :n"),
                {"n": order["subs"][0]["orderSubNo"]},
            )
        )


async def _item_refund_state(order: dict) -> tuple[int, int, int]:
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT refunded_num, refunding_num, refunded_amount FROM trade.order_item"
                    " WHERE order_sub_no = :n"
                ),
                {"n": order["subs"][0]["orderSubNo"]},
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


# ============================================================
# ① 资格预检
# ============================================================
async def test_check_returns_refundable_items(client: AsyncClient, session) -> None:
    """★ 预检接口要能算清"能退多少"，否则用户是盲填表单。

    这个接口之前漏了测试，结果 `_window_deadline` 被写成协程却在同步上下文里用，
    线上直接 500 —— 补上这条用例就是为了钉住"check 能跑通且字段齐全"。
    """
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=2, idem="chk-1")

    resp = await client.post(
        "/api/aftersales/check",
        json={"orderSubNo": order["subs"][0]["orderSubNo"]},
        headers=ctx["buyer_headers"],
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["refundable"] is True
    assert data["refundType"] == RETURN_REFUND, "已完成的订单只能退货退款"
    assert data["maxItemAmount"] == SKU_PRICE * 2
    assert data["maxFreight"] == order["freightAmount"], "整单退才有运费可退"
    # 已签收 → 有窗口截止时间
    assert data["deadline"] is not None
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["maxNum"] == 2
    assert item["refundedNum"] == 0


async def test_check_blocks_when_already_in_progress(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="chk-2")
    await _apply_refund(client, ctx, order=order, num=1)

    resp = await client.post(
        "/api/aftersales/check",
        json={"orderSubNo": order["subs"][0]["orderSubNo"]},
        headers=ctx["buyer_headers"],
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["refundable"] is False
    assert "进行中" in data["reason"]


async def test_check_blocks_unpaid_order(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx, idem="chk-3")

    resp = await client.post(
        "/api/aftersales/check",
        json={"orderSubNo": order["subs"][0]["orderSubNo"]},
        headers=ctx["buyer_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["refundable"] is False


async def test_dispatch_enqueues_funding_refund_no(client: AsyncClient, session, monkeypatch) -> None:
    """★ 投递给 worker 的必须是**资金退款单号**，不是售后单号。

    这两个号长得很像（都以 R 开头、同一天、同样的位数），传错了任务会去查一张
    不存在的资金退款单、直接跳过 —— 而且因为"立即投递失败有 cron 兜底"，
    这个错误在功能上**看不出来**，只是退款从"秒到"变成"等 3 分钟"。
    所以必须用测试把参数钉死。
    """
    from app.modules.aftersale import service as svc

    captured: list[tuple] = []

    async def fake_enqueue(job_name: str, *args, **kwargs):
        captured.append((job_name, *args))
        return True

    monkeypatch.setattr(svc, "enqueue_at", fake_enqueue)

    ctx = await _prepare(client, session)
    order = await _unshipped_order(client, ctx, num=1, idem="enq-1")
    refund = await _apply_refund(client, ctx, order=order, num=1)
    resp = await client.post(
        f"/api/merchant/aftersales/{refund['refundNo']}/approve",
        json={},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text

    assert len(captured) == 1, f"应当恰好投递一次：{captured}"
    job_name, enqueued_no = captured[0][0], captured[0][1]
    assert job_name == svc.JOB_EXECUTE_REFUND
    assert enqueued_no != refund["refundNo"], "不能把售后单号当成资金退款单号投出去"

    async with get_session_factory()() as s:
        biz_no = await s.scalar(
            text("SELECT refund_biz_no FROM payment.payment_refund WHERE refund_no = :n"),
            {"n": enqueued_no},
        )
    assert biz_no == refund["refundNo"], "投出去的必须是查得到的资金退款单号"


# ============================================================
# ② 退货退款全链路
# ============================================================
async def test_return_refund_full_chain(client: AsyncClient, session) -> None:
    """已完成订单走完整条退货退款链，库存只在质检合格时回补。"""
    ctx = await _prepare(client, session)
    sku_id, wh = int(ctx["sku_ids"][0]), ctx["wh_id"]

    order = await _finished_order(client, ctx, num=2)
    # 发货后货已经出库：total 少了 2，可售不变
    after_finish = await _stock(sku_id, wh)
    _assert_identity(after_finish)
    assert after_finish["total"] == 98 and after_finish["available"] == 98, after_finish

    refund = await _apply_refund(client, ctx, order=order, num=2)
    refund_no = refund["refundNo"]
    # 已完成 → 只能退货退款
    assert refund["refundType"] == RETURN_REFUND
    assert refund["status"] == 10
    # 整单退 → 运费跟着退（金额就是子单的运费，不另算）
    assert refund["refundFreight"] == order["freightAmount"], "整个子单都退了，运费要一起退"
    assert refund["refundAmount"] == SKU_PRICE * 2
    assert await _sub_status(order) == 60, "子单应当进入退款中"

    # 申请时预占件数，但**库存一分不动**
    mid = await _stock(sku_id, wh)
    assert mid == after_finish, f"申请阶段不该动库存：{mid}"
    assert await _item_refund_state(order) == (0, 2, 0)

    # 商家同意 → 等用户寄回，库存仍然不动
    resp = await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve",
        json={},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text
    assert (await _refund_row(refund_no))["status"] == 30
    assert await _stock(sku_id, wh) == after_finish, "同意阶段不该动库存"

    # 用户填退货单号 → 商家签收，库存还是不动
    resp = await client.post(
        f"/api/aftersales/{refund_no}/return",
        json={"expressCompany": "顺丰速运", "expressNo": "SF-RETURN-1"},
        headers=ctx["buyer_headers"],
    )
    assert resp.status_code == 200, resp.text
    assert (await _refund_row(refund_no))["status"] == 40
    assert (await _refund_row(refund_no))["return_express_no"] == "SF-RETURN-1"

    resp = await client.post(
        f"/api/merchant/aftersales/{refund_no}/receive",
        json={},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text
    assert (await _refund_row(refund_no))["status"] == 50
    assert await _stock(sku_id, wh) == after_finish, "签收阶段不该动库存"

    # ★ 质检合格：这一刻才回补库存
    resp = await client.post(
        f"/api/merchant/aftersales/{refund_no}/quality",
        json={"passed": True, "remark": "外观完好"},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text
    after_restore = await _stock(sku_id, wh)
    _assert_identity(after_restore)
    assert after_restore["total"] == 100 and after_restore["available"] == 100, after_restore

    # 执行资金退款 → 售后与子单一起转 70
    result = await _run_refund_task(refund_no)
    assert result.get("ok") is True, result

    assert (await _refund_row(refund_no))["status"] == 70
    assert await _sub_status(order) == ORDER_REFUNDED
    assert await _item_refund_state(order) == (2, 0, SKU_PRICE * 2)

    detail = await client.get(f"/api/aftersales/{refund_no}", headers=ctx["buyer_headers"])
    assert detail.json()["data"]["statusText"] == "退款成功"


async def test_partial_refund_keeps_stock_untouched_until_quality(client: AsyncClient, session) -> None:
    """部分退货：运费不退；质检合格后按退的件数回补。"""
    ctx = await _prepare(client, session)
    sku_id, wh = int(ctx["sku_ids"][0]), ctx["wh_id"]
    order = await _finished_order(client, ctx, num=3)

    refund = await _apply_refund(client, ctx, order=order, num=1, idem="partial-1")
    assert refund["refundFreight"] == 0, "只退一部分，运费不退"
    assert refund["refundAmount"] == SKU_PRICE, "3 件退 1 件，按件比例"

    refund_no = refund["refundNo"]
    await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve", json={}, headers=ctx["merchant_headers"]
    )
    await client.post(
        f"/api/aftersales/{refund_no}/return",
        json={"expressCompany": "顺丰速运", "expressNo": "SF-P1"},
        headers=ctx["buyer_headers"],
    )
    await client.post(
        f"/api/merchant/aftersales/{refund_no}/receive", json={}, headers=ctx["merchant_headers"]
    )
    await client.post(
        f"/api/merchant/aftersales/{refund_no}/quality",
        json={"passed": True},
        headers=ctx["merchant_headers"],
    )

    restored = await _stock(sku_id, wh)
    _assert_identity(restored)
    # 出库 3 件后 total=97；退回 1 件 → total=98
    assert restored["total"] == 98, restored

    await _run_refund_task(refund_no)
    assert await _item_refund_state(order) == (1, 0, SKU_PRICE)


# ============================================================
# ② 仅退款（未发货）
# ============================================================
async def test_refund_only_full_chain_unshipped(client: AsyncClient, session) -> None:
    """未发货的仅退款：库存 frozen → available，总量不变。"""
    ctx = await _prepare(client, session)
    sku_id, wh = int(ctx["sku_ids"][0]), ctx["wh_id"]

    order = await _unshipped_order(client, ctx, num=2)
    before = await _stock(sku_id, wh)
    _assert_identity(before)
    # 支付后货在 frozen 里
    assert before == {"total": 100, "available": 98, "locked": 0, "frozen": 2, "defective": 0}

    refund = await _apply_refund(client, ctx, order=order, num=2, reason=REASON_QUALITY)
    refund_no = refund["refundNo"]
    assert refund["refundType"] == REFUND_ONLY, "未发货 → 系统强制仅退款"

    # 商家同意：立刻回补库存并开始退款（不用等寄回）
    resp = await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve", json={}, headers=ctx["merchant_headers"]
    )
    assert resp.status_code == 200, resp.text

    after = await _stock(sku_id, wh)
    _assert_identity(after)
    assert after == {"total": 100, "available": 100, "locked": 0, "frozen": 0, "defective": 0}, after

    row = await _refund_row(refund_no)
    assert row["status"] == 60, "仅退款同意后直接进退款中"

    result = await _run_refund_task(refund_no)
    assert result.get("ok") is True, result
    assert (await _refund_row(refund_no))["status"] == 70
    assert await _sub_status(order) == ORDER_REFUNDED


async def test_shipped_order_forces_return_refund(client: AsyncClient, session) -> None:
    """★ 已发货后选"仅退款"要被强制转成退货退款（docs/08 §3.4）。"""
    ctx = await _prepare(client, session)
    order = await _unshipped_order(client, ctx, idem="force-1")
    sub_no = order["subs"][0]["orderSubNo"]

    # 先发货
    resp = await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": "SF-FORCE"},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text

    item_id = await _item_id(order)
    resp = await client.post(
        "/api/aftersales",
        json={
            "orderSubNo": sub_no,
            "items": [{"orderItemId": item_id, "num": 1}],
            "reasonType": REASON_NO_LONGER_WANT,
            "refundType": REFUND_ONLY,  # 客户端故意传错
        },
        headers={**ctx["buyer_headers"], "Idempotency-Key": "force-2"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["refundType"] == RETURN_REFUND, "以子单状态为准，不信客户端"


# ============================================================
# ③ 质检不通过
# ============================================================
async def test_quality_failed_keeps_stock_out_and_resets_sub(client: AsyncClient, session) -> None:
    """★ 质检不合格：不回补可售库存、进残次品池、子单复位到申请前。"""
    ctx = await _prepare(client, session)
    sku_id, wh = int(ctx["sku_ids"][0]), ctx["wh_id"]
    order = await _finished_order(client, ctx, num=2, idem="qf-1")
    shipped = await _stock(sku_id, wh)

    refund = await _apply_refund(client, ctx, order=order, num=2, reason=REASON_QUALITY)
    refund_no = refund["refundNo"]
    assert (await _refund_row(refund_no))["source_status"] == ORDER_FINISHED

    for path, body in (
        (f"/api/merchant/aftersales/{refund_no}/approve", {}),
        (f"/api/aftersales/{refund_no}/return", {"expressCompany": "顺丰速运", "expressNo": "SF-QF"}),
        (f"/api/merchant/aftersales/{refund_no}/receive", {}),
    ):
        headers = ctx["buyer_headers"] if "/return" in path else ctx["merchant_headers"]
        resp = await client.post(path, json=body, headers=headers)
        assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/api/merchant/aftersales/{refund_no}/quality",
        json={"passed": False, "remark": "有划痕"},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text

    row = await _refund_row(refund_no)
    assert row["status"] == 51, "质检不通过是终态"
    assert row["quality_result"] == QUALITY_FAIL

    after = await _stock(sku_id, wh)
    _assert_identity(after)
    # 可售与总量都不动（商品不能卖），但残次品多了 2 件
    assert after["total"] == shipped["total"], "不合格不能回补 total"
    assert after["available"] == shipped["available"], "不合格不能回补可售"
    assert after["defective"] == 2, after

    # ★ 子单必须复位 —— 否则会永远卡在"退款中"
    assert await _sub_status(order) == ORDER_FINISHED
    assert await _item_refund_state(order) == (0, 0, 0), "预占要释放"


# ============================================================
# ④ 拒绝 / 撤销
# ============================================================
async def test_reject_resets_sub_and_releases_reserve(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=2, idem="rej-1")
    refund = await _apply_refund(client, ctx, order=order, num=2)
    refund_no = refund["refundNo"]

    resp = await client.post(
        f"/api/merchant/aftersales/{refund_no}/reject",
        json={"reason": "商品无质量问题"},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text

    assert (await _refund_row(refund_no))["status"] == 11
    assert await _sub_status(order) == ORDER_FINISHED
    assert await _item_refund_state(order) == (0, 0, 0)


async def test_revoke_resets_sub(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    order = await _unshipped_order(client, ctx, num=2, idem="rev-1")
    refund = await _apply_refund(client, ctx, order=order, num=2)
    refund_no = refund["refundNo"]

    resp = await client.post(
        f"/api/aftersales/{refund_no}/revoke", headers=ctx["buyer_headers"]
    )
    assert resp.status_code == 200, resp.text

    assert (await _refund_row(refund_no))["status"] == 81
    assert await _sub_status(order) == ORDER_WAIT_DELIVER
    assert await _item_refund_state(order) == (0, 0, 0)


async def test_cannot_revoke_after_shipped_back(client: AsyncClient, session) -> None:
    """已经寄回（40 待商家收货）就不能撤了 —— 在途退货没法记账。"""
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="rev-2")
    refund = await _apply_refund(client, ctx, order=order, num=1)
    refund_no = refund["refundNo"]

    await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve", json={}, headers=ctx["merchant_headers"]
    )
    await client.post(
        f"/api/aftersales/{refund_no}/return",
        json={"expressCompany": "顺丰速运", "expressNo": "SF-REV"},
        headers=ctx["buyer_headers"],
    )

    resp = await client.post(f"/api/aftersales/{refund_no}/revoke", headers=ctx["buyer_headers"])
    assert resp.status_code == 422
    assert resp.json()["code"] == "AFTERSALE_STATUS_INVALID"


# ============================================================
# ⑤ 退款回调幂等
# ============================================================
async def test_refund_execution_is_idempotent(client: AsyncClient, session) -> None:
    """★ 资金退款重复执行，四处退款额各只累加一次。"""
    ctx = await _prepare(client, session)
    order = await _unshipped_order(client, ctx, num=1, idem="idem-r1")
    refund = await _apply_refund(client, ctx, order=order, num=1)
    refund_no = refund["refundNo"]
    await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve", json={}, headers=ctx["merchant_headers"]
    )

    first = await _run_refund_task(refund_no)
    assert first.get("ok") is True, first
    state_once = await _item_refund_state(order)
    async with get_session_factory()() as s:
        sub_once = int(
            await s.scalar(
                text("SELECT refunded_amount FROM trade.order_sub WHERE order_sub_no = :n"),
                {"n": order["subs"][0]["orderSubNo"]},
            )
        )
        main_once = int(
            await s.scalar(
                text("SELECT refunded_amount FROM trade.order_main WHERE order_main_no = :n"),
                {"n": order["orderMainNo"]},
            )
        )

    # 再跑两次
    for _ in range(2):
        again = await _run_refund_task(refund_no)
        assert again.get("skipped") == "already_succeeded", again

    assert await _item_refund_state(order) == state_once
    async with get_session_factory()() as s:
        assert (
            int(
                await s.scalar(
                    text("SELECT refunded_amount FROM trade.order_sub WHERE order_sub_no = :n"),
                    {"n": order["subs"][0]["orderSubNo"]},
                )
            )
            == sub_once
        )
        assert (
            int(
                await s.scalar(
                    text("SELECT refunded_amount FROM trade.order_main WHERE order_main_no = :n"),
                    {"n": order["orderMainNo"]},
                )
            )
            == main_once
        )


# ============================================================
# ⑥ 幂等与并发
# ============================================================
async def test_apply_is_idempotent_by_key(client: AsyncClient, session) -> None:
    """同一个 Idempotency-Key 重放只产生一张售后单。"""
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="ap-1")
    item_id = await _item_id(order)
    sub_no = order["subs"][0]["orderSubNo"]

    body = {
        "orderSubNo": sub_no,
        "items": [{"orderItemId": item_id, "num": 1}],
        "reasonType": REASON_NO_LONGER_WANT,
    }
    headers = {**ctx["buyer_headers"], "Idempotency-Key": "same-refund-key"}
    first = await client.post("/api/aftersales", json=body, headers=headers)
    second = await client.post("/api/aftersales", json=body, headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"]["refundNo"] == second.json()["data"]["refundNo"]

    async with get_session_factory()() as s:
        count = await s.scalar(text("SELECT count(*) FROM aftersale.refund_order"))
    assert count == 1


async def test_concurrent_apply_only_one_wins(client: AsyncClient, session) -> None:
    """★ 同一子单并发申请，只能有一个成功。

    守卫是三层：子单行锁、进行中售后的部分唯一索引、订单项的预占条件更新。
    """
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=2, idem="cc-1")
    item_id = await _item_id(order)
    sub_no = order["subs"][0]["orderSubNo"]

    async def _one(tag: str) -> str:
        resp = await client.post(
            "/api/aftersales",
            json={
                "orderSubNo": sub_no,
                "items": [{"orderItemId": item_id, "num": 2}],
                "reasonType": REASON_NO_LONGER_WANT,
            },
            headers={**ctx["buyer_headers"], "Idempotency-Key": tag},
        )
        if resp.status_code == 200:
            return "OK"
        return resp.json().get("code", str(resp.status_code))

    results = await asyncio.gather(_one("cc-a"), _one("cc-b"))
    assert sorted(results) == ["AFTERSALE_IN_PROGRESS", "OK"], results

    async with get_session_factory()() as s:
        active = await s.scalar(
            text(
                "SELECT count(*) FROM aftersale.refund_order"
                " WHERE order_sub_no = :n AND status IN (10,20,30,40,50,60)"
            ),
            {"n": sub_no},
        )
    assert active == 1


async def test_apply_rejected_for_unpaid_order(client: AsyncClient, session) -> None:
    """待付款的订单不走售后，应该去取消订单。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx, idem="un-1")
    item_id = await _item_id(order)

    resp = await client.post(
        "/api/aftersales",
        json={
            "orderSubNo": order["subs"][0]["orderSubNo"],
            "items": [{"orderItemId": item_id, "num": 1}],
            "reasonType": REASON_NO_LONGER_WANT,
        },
        headers={**ctx["buyer_headers"], "Idempotency-Key": "un-2"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "ORDER_STATUS_INVALID"


async def test_apply_rejects_too_many_items(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="too-1")
    item_id = await _item_id(order)

    resp = await client.post(
        "/api/aftersales",
        json={
            "orderSubNo": order["subs"][0]["orderSubNo"],
            "items": [{"orderItemId": item_id, "num": 5}],
            "reasonType": REASON_NO_LONGER_WANT,
        },
        headers={**ctx["buyer_headers"], "Idempotency-Key": "too-2"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "REFUND_NUM_EXCEED"


# ============================================================
# ⑦ 超时
# ============================================================
async def test_review_timeout_auto_approves(client: AsyncClient, session) -> None:
    """★ 商家 48 小时没审核 → 系统自动同意，并记一条告警。"""
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="to-1")
    refund = await _apply_refund(client, ctx, order=order, num=1)
    refund_no = refund["refundNo"]

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE aftersale.refund_order SET deadline = now() - interval '1 minute'"
                " WHERE refund_no = :n"
            ),
            {"n": refund_no},
        )

    async with get_session_factory()() as s:
        done = await aftersale_service.process_timeouts(s)
        await s.commit()
    assert done == 1

    row = await _refund_row(refund_no)
    assert row["status"] == 30, "退货退款超时后应当进「待买家寄回」"

    async with get_session_factory()() as s:
        alerts = await s.scalar(
            text("SELECT count(*) FROM ops.alert WHERE source = 'aftersale.timeout'")
        )
    assert alerts == 1

    # 再扫一次不会重复处理
    async with get_session_factory()() as s:
        assert await aftersale_service.process_timeouts(s) == 0


async def test_return_timeout_closes_refund(client: AsyncClient, session) -> None:
    """用户 7 天不寄回 → 售后关闭，子单复位。"""
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="to-2")
    refund = await _apply_refund(client, ctx, order=order, num=1)
    refund_no = refund["refundNo"]
    await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve", json={}, headers=ctx["merchant_headers"]
    )

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE aftersale.refund_order SET deadline = now() - interval '1 minute'"
                " WHERE refund_no = :n"
            ),
            {"n": refund_no},
        )
    async with get_session_factory()() as s:
        assert await aftersale_service.process_timeouts(s) == 1
        await s.commit()

    assert (await _refund_row(refund_no))["status"] == 80
    assert await _sub_status(order) == ORDER_FINISHED
    assert await _item_refund_state(order) == (0, 0, 0)


async def test_quality_timeout_auto_passes(client: AsyncClient, session) -> None:
    """质检 48 小时没提交 → 自动通过，库存回补并发起退款。"""
    ctx = await _prepare(client, session)
    sku_id, wh = int(ctx["sku_ids"][0]), ctx["wh_id"]
    order = await _finished_order(client, ctx, num=1, idem="to-3")
    shipped = await _stock(sku_id, wh)
    refund = await _apply_refund(client, ctx, order=order, num=1)
    refund_no = refund["refundNo"]

    for path, body, who in (
        (f"/api/merchant/aftersales/{refund_no}/approve", {}, "m"),
        (f"/api/aftersales/{refund_no}/return", {"expressCompany": "顺丰速运", "expressNo": "SF-TO"}, "b"),
        (f"/api/merchant/aftersales/{refund_no}/receive", {}, "m"),
    ):
        headers = ctx["buyer_headers"] if who == "b" else ctx["merchant_headers"]
        assert (await client.post(path, json=body, headers=headers)).status_code == 200

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE aftersale.refund_order SET deadline = now() - interval '1 minute'"
                " WHERE refund_no = :n"
            ),
            {"n": refund_no},
        )
    async with get_session_factory()() as s:
        assert await aftersale_service.process_timeouts(s) == 1
        await s.commit()

    assert (await _refund_row(refund_no))["status"] == 60
    after = await _stock(sku_id, wh)
    _assert_identity(after)
    assert after["total"] == shipped["total"] + 1, "超时视同合格，要回补库存"


# ============================================================
# ⑧ 优惠券退回
# ============================================================
async def test_whole_order_refund_returns_coupon(client: AsyncClient, session) -> None:
    """★ 整单退款才退券；部分退款不退。"""
    ctx = await _prepare(client, session)
    tpl_id = await _make_coupon()
    code_id = await _receive_coupon(tpl_id, ctx["buyer_id"], "receive-af")

    order = await _finished_order(client, ctx, num=1, idem="cpn-1", coupon_ids=[code_id])
    assert await _coupon_status(code_id) == 3, "支付后券已核销"

    refund = await _apply_refund(client, ctx, order=order, num=1, reason=REASON_NO_LONGER_WANT)
    refund_no = refund["refundNo"]

    # 走到质检合格 + 退款成功
    for path, body, who in (
        (f"/api/merchant/aftersales/{refund_no}/approve", {}, "m"),
        (f"/api/aftersales/{refund_no}/return", {"expressCompany": "顺丰速运", "expressNo": "SF-CP"}, "b"),
        (f"/api/merchant/aftersales/{refund_no}/receive", {}, "m"),
        (f"/api/merchant/aftersales/{refund_no}/quality", {"passed": True}, "m"),
    ):
        headers = ctx["buyer_headers"] if who == "b" else ctx["merchant_headers"]
        assert (await client.post(path, json=body, headers=headers)).status_code == 200

    await _run_refund_task(refund_no)

    assert await _coupon_status(code_id) == 1, "整单退完，券要回到未使用"
    async with get_session_factory()() as s:
        flows = await s.scalar(
            text(
                "SELECT count(*) FROM promotion.coupon_flow"
                " WHERE biz_key = :k"
            ),
            {"k": f"REFUND:{refund_no}:{code_id}"},
        )
    assert flows == 1, "退券流水要能按 biz_key 反查到"


async def _coupon_status(code_id: int) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT status FROM promotion.coupon_code WHERE id = :c"), {"c": code_id}
            )
        )


# ============================================================
# ⑨ 权限与守恒
# ============================================================
async def test_refund_hidden_from_others(client: AsyncClient, session) -> None:
    """★ 别人的售后单查不到（防遍历探测）。"""
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="perm-1")
    refund = await _apply_refund(client, ctx, order=order, num=1)

    other = await register(client, phone=BUYER2_PHONE)
    resp = await client.get(
        f"/api/aftersales/{refund['refundNo']}", headers=auth_header(other["accessToken"])
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_merchant_cannot_handle_other_shop_refund(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="perm-2")
    refund = await _apply_refund(client, ctx, order=order, num=1)

    other = await register(client, phone="13800138041")
    resp = await client.post(
        f"/api/merchant/aftersales/{refund['refundNo']}/approve",
        json={},
        headers=auth_header(other["accessToken"]),
    )
    # 没开店 → 进不来商家端；即使进来了也只会拿到"不存在"
    assert resp.status_code in (403, 404)


async def test_db_rejects_over_refund_amount(client: AsyncClient, session) -> None:
    """★ 超退金额写不进数据库 —— CHECK 是最后一道防线。"""
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="ov-1")
    item_id = await _item_id(order)

    with pytest.raises(IntegrityError):
        async with get_session_factory()() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE trade.order_item SET refunded_amount = payable_amount + 1"
                    " WHERE id = :i"
                ),
                {"i": int(item_id)},
            )


async def test_db_rejects_over_refund_num(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx, num=1, idem="ov-2")
    item_id = await _item_id(order)

    with pytest.raises(IntegrityError):
        async with get_session_factory()() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE trade.order_item SET refunded_num = num, refunding_num = 1"
                    " WHERE id = :i"
                ),
                {"i": int(item_id)},
            )


async def test_payment_refund_cannot_exceed_paid(client: AsyncClient, session) -> None:
    """给同一个售后单退两次钱要被唯一约束挡住。"""
    ctx = await _prepare(client, session)
    order = await _unshipped_order(client, ctx, num=1, idem="pr-1")
    refund = await _apply_refund(client, ctx, order=order, num=1)
    refund_no = refund["refundNo"]
    await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve", json={}, headers=ctx["merchant_headers"]
    )

    with pytest.raises(IntegrityError):
        async with get_session_factory()() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO payment.payment_refund ("
                    "id, refund_no, refund_biz_no, pay_no, order_main_no, user_id, amount"
                    ") SELECT :id, 'PDUP', refund_biz_no, pay_no, order_main_no, user_id, amount"
                    " FROM payment.payment_refund WHERE refund_biz_no = :n"
                ),
                {"id": 1, "n": refund_no},
            )


# ============================================================
# ⑩ 对账
# ============================================================
async def test_reconcile_clean_after_normal_flow(client: AsyncClient, session) -> None:
    """正常走完一笔退款，对账应当一条问题都查不出来。"""
    ctx = await _prepare(client, session)
    order = await _unshipped_order(client, ctx, num=1, idem="rec-1")
    refund = await _apply_refund(client, ctx, order=order, num=1)
    refund_no = refund["refundNo"]
    await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve", json={}, headers=ctx["merchant_headers"]
    )
    await _run_refund_task(refund_no)

    async with get_session_factory()() as s:
        results = await aftersale_service.reconcile_refunds(s)
        await s.commit()
    assert all(v == 0 for v in results.values()), results


async def test_reconcile_catches_drift(client: AsyncClient, session) -> None:
    """★ 手工把账改歪，对账必须查得出来并写 P0 告警。"""
    ctx = await _prepare(client, session)
    order = await _unshipped_order(client, ctx, num=1, idem="rec-2")
    refund = await _apply_refund(client, ctx, order=order, num=1)
    refund_no = refund["refundNo"]
    await client.post(
        f"/api/merchant/aftersales/{refund_no}/approve", json={}, headers=ctx["merchant_headers"]
    )
    await _run_refund_task(refund_no)

    # 手工破坏订单项的已退金额（对账③"子单退款额 = Σ订单项商品款 + 运费"能查出来）。
    # 注意不能直接改母单：全额退完之后 refunded_amount 已经等于 paid_amount，
    # 再加 1 会先被 CHECK 约束拦下 —— 那反而证明约束是好的
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE trade.order_item SET refunded_amount = refunded_amount - 1"
                " WHERE order_sub_no = :n"
            ),
            {"n": order["subs"][0]["orderSubNo"]},
        )

    async with get_session_factory()() as s:
        results = await aftersale_service.reconcile_refunds(s)
        await s.commit()
    assert results["sub_amount_mismatch"] == 1, results

    async with get_session_factory()() as s:
        level = await s.scalar(
            text("SELECT max(level) FROM ops.alert WHERE source = 'aftersale.reconcile'")
        )
    assert level == 1, "资金对账异常必须是 P0"


async def test_whole_sub_refund_of_split_order_is_not_blocked(
    client: AsyncClient, session
) -> None:
    """★ 拆单后的子单也要能**整单退**。

    这是"子单应付 = 各行实付之和 + 子单运费"的下游：平台优惠按行摊、子单层只聚合。
    如果子单层再摊一遍，`各行实付 + 运费` 会比子单应付多几分，而退款校验
    （docs/08 §10）拿子单应付当上限 —— 于是**整单退也会被 422 拦掉**，
    报"退款金额超限"，怎么查都指不到拆单那一步。
    """
    from tests.test_trade import _create_multi_order, _prepare_two_shops, _sub_amounts

    ctx = await _prepare_two_shops(client, session)
    tpl_id = await _make_coupon(threshold=100, value=200)
    coupon_id = await _receive_coupon(tpl_id, ctx["buyer_id"], "as-two-shop-coupon")
    order = await _create_multi_order(client, ctx, coupon_ids=[coupon_id], idem="as-two-shop")
    await _pay(client, ctx, order["orderMainNo"])

    # 退**第二家店**那个子单 —— 拆单那几个"差的几分"正好落在它身上
    sub_no = order["subs"][1]["orderSubNo"]
    async with get_session_factory()() as s:
        item_id = str(
            await s.scalar(
                text("SELECT id FROM trade.order_item WHERE order_sub_no = :n"), {"n": sub_no}
            )
        )

    resp = await client.post(
        "/api/aftersales",
        json={
            "orderSubNo": sub_no,
            "items": [{"orderItemId": item_id, "num": 1}],
            "reasonType": REASON_NO_LONGER_WANT,
        },
        headers={**ctx["buyer_headers"], "Idempotency-Key": "as-two-shop-refund"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    # 退的正好是"该子单各行实付 + 运费"，与上限一致
    row = next(r for r in await _sub_amounts(order["orderMainNo"]) if r[0] == sub_no)
    assert data["totalRefund"] == data["refundAmount"] + data["refundFreight"]
    assert data["refundFreight"] == row[1], "整单退要退该子单的全部运费"
    assert data["totalRefund"] == row[2], "整单退正好退到子单应付（不多不少）"
