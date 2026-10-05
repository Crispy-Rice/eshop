"""trade + payment 的集成测试。连真实 PostgreSQL 与 Redis。

前面每个模块都只验证自己那一段，这里验证**整条链路**：
下单 → 支付 → 发货 → 收货，以及三件最容易出错的事 ——

1. **下单幂等**：点两次"提交订单"只能产生一个订单
2. **支付回调幂等**：渠道重试是常态，重复回调只能推进一次
3. **并发状态变更**：两个请求同时改同一个子单，只能有一个成功

这三条都不是"边界情况"，而是线上每天都会发生的常态。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import get_session_factory
from app.core.errors import BizError
from app.core.redis import get_redis
from app.core.snowflake import next_id
from app.modules.freight import service as freight_service
from app.modules.inventory import service as inv
from app.modules.inventory.models import Warehouse
from app.modules.promotion import service as promotion_service
from app.modules.promotion.models import CouponTemplate
from app.modules.trade import service as trade_service
from app.modules.trade.models import (
    ORDER_CLOSED,
    ORDER_FINISHED,
    ORDER_WAIT_DELIVER,
    ORDER_WAIT_PAY,
    ORDER_WAIT_RECEIVE,
)
from tests.conftest import auth_header, make_admin, open_shop, register
from tests.test_product import _make_category, _spu_payload

BUYER_PHONE = "13900139031"
BUYER2_PHONE = "13900139032"
MERCHANT_PHONE = "13800138030"

# seed 里每个 SKU 重 199g、价 699900 分（tests/test_product.py 的 _spu_payload）
SKU_PRICE = 699900
FREIGHT_FIRST = 1000  # 首重 10 元


@pytest.fixture(autouse=True)
async def clean_redis(app_runtime: None) -> AsyncIterator[None]:
    """清库存与券的 Redis key。

    DB 由 conftest 的 ``clean_tables`` 清，Redis 是另一套状态 ——
    不清的话上一个用例留下的库存分片会让下一个用例读到错误的可售数。
    """
    redis = get_redis()
    keys: list[str] = []
    for pattern in ("stock:*", "lock:stock_init:*", "coupon:*"):
        async for key in redis.scan_iter(match=pattern):
            keys.append(key)
    if keys:
        await redis.delete(*keys)
    yield


# ============================================================
# 造数据
# ============================================================
async def _prepare(client: AsyncClient, session) -> dict:
    """造好一条可下单的链路：商家 + 已上架商品 + 库存 + 运费模板 + 买家 + 地址。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")

    merchant = await register(client, phone=MERCHANT_PHONE)
    shop_id = await open_shop(client, merchant["accessToken"])
    merchant_headers = auth_header(merchant["accessToken"])

    resp = await client.post(
        "/api/merchant/spus", json=_spu_payload(category), headers=merchant_headers
    )
    assert resp.status_code == 200, resp.text
    spu_id = resp.json()["data"]["id"]
    sku_ids = [s["id"] for s in resp.json()["data"]["skus"]]

    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=merchant_headers)
    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": True},
        headers=auth_header(admin["accessToken"]),
    )

    # 仓库 + 库存
    async with get_session_factory()() as s, s.begin():
        wh = Warehouse(id=next_id(), shop_id=int(shop_id), name="测试仓", is_default=True)
        s.add(wh)
    wh_id = int(wh.id)
    async with get_session_factory()() as s, s.begin():
        for i, sku_id in enumerate(sku_ids):
            await inv.init(s, sku_id=int(sku_id), warehouse_id=wh_id, qty=100, biz_key=f"t-init:{i}")

    # 运费模板并绑定（不配区域规则 → 全国按模板本身计费）
    async with get_session_factory()() as s, s.begin():
        tpl = await freight_service.create_template(
            s,
            int(shop_id),
            _template_req(),
        )
    tpl_id = int(tpl.id)
    async with get_session_factory()() as s, s.begin():
        for sku_id in sku_ids:
            await freight_service.bind_sku(
                s, int(shop_id), sku_id=int(sku_id), template_id=tpl_id, warehouse_id=wh_id
            )

    buyer = await register(client, phone=BUYER_PHONE)
    buyer_headers = auth_header(buyer["accessToken"])
    address_id = await _make_address(client, buyer_headers)

    return {
        "shop_id": int(shop_id),
        "merchant_headers": merchant_headers,
        "buyer_headers": buyer_headers,
        "buyer_id": await _user_id(BUYER_PHONE),
        "sku_ids": sku_ids,
        "wh_id": wh_id,
        "address_id": address_id,
    }


async def _user_id(phone: str) -> int:
    """从库里取用户 id。注册接口只返回 token，不带用户对象。"""
    from app.core.crypto import phone_hash

    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT id FROM account.user WHERE phone_hash = :h"),
                {"h": phone_hash(phone)},
            )
        )


def _template_req():
    from app.modules.freight.schemas import FreightTemplateCreateRequest

    return FreightTemplateCreateRequest(
        name="默认模板",
        charge_type=1,
        first_unit=1000,
        first_price=FREIGHT_FIRST,
        add_unit=500,
        add_price=300,
    )


async def _make_address(client: AsyncClient, headers: dict) -> str:
    resp = await client.post(
        "/api/me/addresses",
        json={
            "receiverName": "张三",
            "phone": "13800138000",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "某某路 1 号",
            "regionCode": "310115",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def _make_coupon(*, threshold: int = 100, value: int = 2000) -> int:
    """建一个平台券模板（长期有效、进行中）。"""
    now = datetime.now(UTC)
    async with get_session_factory()() as s, s.begin():
        tpl = CouponTemplate(
            id=next_id(),
            shop_id=0,
            name="满1元减20",
            type=1,
            get_type=1,
            discount_value=value,
            threshold=threshold,
            total_count=100,
            per_user_limit=5,
            valid_type=1,
            valid_start=now - timedelta(hours=1),
            valid_end=now + timedelta(days=7),
            scope_type=1,
            status=2,
        )
        s.add(tpl)
    return int(tpl.id)


async def _receive_coupon(tpl_id: int, user_id: int, idem: str) -> int:
    """领券并返回券实例 id。"""
    async with get_session_factory()() as s:
        await promotion_service.receive(s, user_id=user_id, tpl_id=tpl_id, idem_key=idem)
        await s.commit()
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text(
                    "SELECT id FROM promotion.coupon_code "
                    "WHERE user_id = :u ORDER BY id DESC LIMIT 1"
                ),
                {"u": user_id},
            )
        )


async def _create_order(
    client: AsyncClient,
    ctx: dict,
    *,
    num: int = 1,
    idem: str = "idem-1",
    coupon_ids: list[int] | None = None,
    sku_index: int = 0,
) -> dict:
    body: dict = {
        "items": [{"skuId": ctx["sku_ids"][sku_index], "num": num}],
        "addressId": ctx["address_id"],
    }
    if coupon_ids:
        body["couponCodeIds"] = coupon_ids
    resp = await client.post(
        "/api/orders",
        json=body,
        headers={**ctx["buyer_headers"], "Idempotency-Key": idem},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _pay(client: AsyncClient, ctx: dict, order_main_no: str) -> dict:
    """发起支付 + 模拟回调。返回回调后的支付单。"""
    resp = await client.post(
        "/api/payments", json={"orderMainNo": order_main_no}, headers=ctx["buyer_headers"]
    )
    assert resp.status_code == 200, resp.text
    pay_no = resp.json()["data"]["payNo"]

    resp = await client.post(f"/api/payments/{pay_no}/mock-callback", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _db_statuses(order_main_no: str) -> tuple[int, int]:
    """返回 (母单状态, 子单状态)。"""
    async with get_session_factory()() as s:
        main = await s.scalar(
            text("SELECT status FROM trade.order_main WHERE order_main_no = :n"),
            {"n": order_main_no},
        )
        sub = await s.scalar(
            text("SELECT status FROM trade.order_sub WHERE order_main_no = :n"),
            {"n": order_main_no},
        )
    return int(main), int(sub)


async def _available(sku_id: int, wh_id: int) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text(
                    "SELECT available FROM inventory.sku_stock "
                    "WHERE sku_id = :k AND warehouse_id = :w"
                ),
                {"k": sku_id, "w": wh_id},
            )
        )


async def _stock_row(sku_id: int, wh_id: int) -> dict[str, int]:
    """(total, available, locked, frozen)。

    恒等式 ``total = available + locked + frozen`` 必须始终成立 ——
    它由 CHECK 约束兜底，这里是把它显式断言出来，好知道每一步各自动了哪一格。
    """
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT total, available, locked, frozen FROM inventory.sku_stock "
                    "WHERE sku_id = :k AND warehouse_id = :w"
                ),
                {"k": sku_id, "w": wh_id},
            )
        ).one()
    return {"total": int(row[0]), "available": int(row[1]), "locked": int(row[2]), "frozen": int(row[3])}


# ============================================================
# ① 下单全链路
# ============================================================
async def test_create_order_full_chain(client: AsyncClient, session) -> None:
    """算价 → 拆单 → 预占库存 → 落库，金额守恒。"""
    ctx = await _prepare(client, session)
    sku_id = int(ctx["sku_ids"][0])

    order = await _create_order(client, ctx, num=2)

    # 母单
    assert order["status"] == ORDER_WAIT_PAY
    assert order["payStatus"] == 0
    assert order["shopCount"] == 1
    assert order["totalAmount"] == SKU_PRICE * 2
    assert order["freightAmount"] == FREIGHT_FIRST
    assert order["payableAmount"] == SKU_PRICE * 2 + FREIGHT_FIRST
    # 单店铺不拆单
    assert len(order["subs"]) == 1

    sub = order["subs"][0]
    assert sub["shopId"] == str(ctx["shop_id"])
    assert sub["status"] == ORDER_WAIT_PAY
    # ★ 母子单金额守恒
    for field in ("totalAmount", "freightAmount", "payableAmount"):
        assert order[field] == sub[field], f"{field} 母子单不守恒"

    # 订单项是快照
    assert len(sub["items"]) == 1
    item = sub["items"][0]
    assert item["skuId"] == str(sku_id)
    assert item["unitPrice"] == SKU_PRICE
    assert item["num"] == 2
    assert item["itemAmount"] == SKU_PRICE * 2

    # 库存被预占：100 - 2 = 98
    assert await _available(sku_id, ctx["wh_id"]) == 98

    # 到点关单的延迟任务已注册成提交后回调（不在事务里执行）
    assert order["payRemainSeconds"] > 0
    assert order["canPay"] is True


async def test_create_order_requires_idempotency_key(client: AsyncClient, session) -> None:
    """没有 Idempotency-Key 直接拒绝 —— 这是下单接口的硬性要求。"""
    ctx = await _prepare(client, session)
    resp = await client.post(
        "/api/orders",
        json={"items": [{"skuId": ctx["sku_ids"][0], "num": 1}], "addressId": ctx["address_id"]},
        headers=ctx["buyer_headers"],
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_create_order_rejects_duplicate_sku(client: AsyncClient, session) -> None:
    """同一 SKU 传两次要在入口被挡住 —— 否则库存按两行预占、退款按两行退。"""
    ctx = await _prepare(client, session)
    sku = ctx["sku_ids"][0]
    resp = await client.post(
        "/api/orders",
        json={
            "items": [{"skuId": sku, "num": 1}, {"skuId": sku, "num": 1}],
            "addressId": ctx["address_id"],
        },
        headers={**ctx["buyer_headers"], "Idempotency-Key": "dup"},
    )
    assert resp.status_code == 400


# ============================================================
# ② 下单幂等
# ============================================================
async def test_create_order_is_idempotent(client: AsyncClient, session) -> None:
    """★ 同一个 Idempotency-Key 重放，只产生一个订单、只扣一次库存。

    用户在结算页连点两次是很常见的；不幂等就会下出两个订单、扣两次库存。
    """
    ctx = await _prepare(client, session)
    sku_id = int(ctx["sku_ids"][0])

    first = await _create_order(client, ctx, idem="same-key")
    second = await _create_order(client, ctx, idem="same-key")

    assert first["orderMainNo"] == second["orderMainNo"]
    async with get_session_factory()() as s:
        count = await s.scalar(text("SELECT count(*) FROM trade.order_main"))
    assert count == 1, "重放不该产生第二个订单"
    assert await _available(sku_id, ctx["wh_id"]) == 99, "重放不该重复扣库存"

    # 换一个 key 是**新**订单
    third = await _create_order(client, ctx, idem="another-key")
    assert third["orderMainNo"] != first["orderMainNo"]


# ============================================================
# ③ 支付链路与回调幂等
# ============================================================
async def test_pay_flow_advances_order(client: AsyncClient, session) -> None:
    """发起支付 → 模拟回调 → 母单与所有子单一起变成「待发货」。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]

    payment = await _pay(client, ctx, main_no)

    assert payment["status"] == 1
    assert payment["paidAmount"] == order["payableAmount"]

    main_status, sub_status = await _db_statuses(main_no)
    assert main_status == ORDER_WAIT_DELIVER
    assert sub_status == ORDER_WAIT_DELIVER

    detail = await client.get(f"/api/orders/{main_no}", headers=ctx["buyer_headers"])
    assert detail.json()["data"]["payStatus"] == 1


async def test_payment_callback_is_idempotent(client: AsyncClient, session) -> None:
    """★ 回调重放只推进一次订单。

    渠道在网络抖动、超时重试时会重复回调，**这是常态不是异常**。
    挡不住就会重复扣库存、重复发通知。
    """
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]

    resp = await client.post(
        "/api/payments", json={"orderMainNo": main_no}, headers=ctx["buyer_headers"]
    )
    pay_no = resp.json()["data"]["payNo"]

    first = await client.post(f"/api/payments/{pay_no}/mock-callback", json={})
    assert first.status_code == 200
    # 再回调两次
    second = await client.post(f"/api/payments/{pay_no}/mock-callback", json={})
    third = await client.post(f"/api/payments/{pay_no}/mock-callback", json={})
    assert second.status_code == 200 and third.status_code == 200

    # 支付状态流转流水只应有一条（PAY_SUCCESS 只发生一次）
    async with get_session_factory()() as s:
        flows = await s.scalar(
            text(
                "SELECT count(*) FROM trade.order_state_flow "
                "WHERE order_no LIKE :p AND event = 'PAY_SUCCESS'"
            ),
            {"p": f"{main_no}-%"},
        )
    assert flows == 1, f"PAY_SUCCESS 应当只发生一次，实际 {flows} 次"

    main_status, _ = await _db_statuses(main_no)
    assert main_status == ORDER_WAIT_DELIVER


async def test_payment_consumes_coupon(client: AsyncClient, session) -> None:
    """★ 支付成功后券从「已锁定」变成「已核销」。

    卡在"锁着"的话，用户的券既用不掉也退不回来 —— 必须由支付成功这条路径推进。
    """
    ctx = await _prepare(client, session)
    tpl_id = await _make_coupon()
    code_id = await _receive_coupon(tpl_id, ctx["buyer_id"], "receive-use")

    order = await _create_order(client, ctx, coupon_ids=[code_id])
    assert await _coupon_status(code_id) == 2, "下单后是已锁定"

    await _pay(client, ctx, order["orderMainNo"])

    assert await _coupon_status(code_id) == 3, "支付后应当是已核销"
    async with get_session_factory()() as s:
        use_amount = int(
            await s.scalar(
                text("SELECT use_amount FROM promotion.coupon_code WHERE id = :c"),
                {"c": code_id},
            )
        )
    assert use_amount == 2000, "核销金额应当是这张券实际抵扣的 20 元"


async def _coupon_status(code_id: int) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT status FROM promotion.coupon_code WHERE id = :c"), {"c": code_id}
            )
        )


async def test_create_payment_is_idempotent(client: AsyncClient, session) -> None:
    """同一母单重复发起支付返回同一张支付单 —— 用户来回切页面不该产生多张。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]

    a = await client.post(
        "/api/payments", json={"orderMainNo": main_no}, headers=ctx["buyer_headers"]
    )
    b = await client.post(
        "/api/payments", json={"orderMainNo": main_no}, headers=ctx["buyer_headers"]
    )
    assert a.json()["data"]["payNo"] == b.json()["data"]["payNo"]


async def test_cannot_pay_closed_order(client: AsyncClient, session) -> None:
    """已取消的订单不能再发起支付。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]
    await client.post(f"/api/orders/{main_no}/cancel", headers=ctx["buyer_headers"])

    resp = await client.post(
        "/api/payments", json={"orderMainNo": main_no}, headers=ctx["buyer_headers"]
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "ORDER_STATUS_INVALID"


async def test_payment_is_scoped_to_owner(client: AsyncClient, session) -> None:
    """查不了别人的支付单。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    resp = await client.post(
        "/api/payments", json={"orderMainNo": order["orderMainNo"]}, headers=ctx["buyer_headers"]
    )
    pay_no = resp.json()["data"]["payNo"]

    other = await register(client, phone=BUYER2_PHONE)
    resp = await client.get(f"/api/payments/{pay_no}", headers=auth_header(other["accessToken"]))
    assert resp.status_code == 404


# ============================================================
# ④ 发货与收货
# ============================================================
async def test_ship_and_receive_full_flow(client: AsyncClient, session) -> None:
    """待发货 → 发货 → 待收货 → 确认收货 → 已完成（母子单同步）。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]
    sub_no = order["subs"][0]["orderSubNo"]
    await _pay(client, ctx, main_no)

    resp = await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": "SF123456789"},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text

    main_status, sub_status = await _db_statuses(main_no)
    assert sub_status == ORDER_WAIT_RECEIVE
    assert main_status == ORDER_WAIT_RECEIVE

    resp = await client.post(f"/api/order-subs/{sub_no}/receive", headers=ctx["buyer_headers"])
    assert resp.status_code == 200, resp.text

    main_status, sub_status = await _db_statuses(main_no)
    assert sub_status == ORDER_FINISHED
    assert main_status == ORDER_FINISHED


async def test_ship_twice_is_rejected(client: AsyncClient, session) -> None:
    """★ 重复发货被状态机拒绝。

    不能"静默成功" —— 商家要能知道快递单号没被改掉。
    """
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    sub_no = order["subs"][0]["orderSubNo"]
    await _pay(client, ctx, order["orderMainNo"])

    body = {"expressCompany": "顺丰速运", "expressNo": "SF000001"}
    first = await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship", json=body, headers=ctx["merchant_headers"]
    )
    assert first.status_code == 200

    body["expressNo"] = "SF000002"
    second = await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship", json=body, headers=ctx["merchant_headers"]
    )
    assert second.status_code == 422
    assert second.json()["code"] == "ORDER_STATUS_INVALID"


async def test_ship_rejects_duplicate_express_no(client: AsyncClient, session) -> None:
    """★ 运单号撞号要给 400 明确提示，不能是 500。

    ``uk_delivery_express`` 是全局唯一约束。不预检的话，唯一冲突会中断整个
    PG 事务、冒成 500「系统繁忙」—— 而单号填重是商家自己改一下就能解决的问题。
    """
    ctx = await _prepare(client, session)
    express = {"expressCompany": "顺丰速运", "expressNo": "SF-DUP-0001"}

    first_order = await _create_order(client, ctx, idem="idem-dup-1")
    await _pay(client, ctx, first_order["orderMainNo"])
    first_sub = first_order["subs"][0]["orderSubNo"]
    ok = await client.post(
        f"/api/merchant/order-subs/{first_sub}/ship", json=express, headers=ctx["merchant_headers"]
    )
    assert ok.status_code == 200, ok.text

    # 第二单用同一个单号
    second_order = await _create_order(client, ctx, idem="idem-dup-2")
    await _pay(client, ctx, second_order["orderMainNo"])
    second_sub = second_order["subs"][0]["orderSubNo"]

    dup = await client.post(
        f"/api/merchant/order-subs/{second_sub}/ship", json=express, headers=ctx["merchant_headers"]
    )
    assert dup.status_code == 400, dup.text
    assert dup.json()["code"] == "VALIDATION_ERROR"
    assert "快递单号" in dup.json()["message"]

    # ★ 被拒之后子单必须**还停在待发货** —— 预检要发生在状态机之前，
    #   否则单号被拒了状态却已经推到「待收货」，商家再也发不了货。
    _, sub_status = await _db_statuses(second_order["orderMainNo"])
    assert sub_status == ORDER_WAIT_DELIVER


async def test_deleted_product_leaves_history_orders_intact(client: AsyncClient, session) -> None:
    """★ 商品软删后历史订单必须完好 —— 订单读的是商品快照。

    这也是"删除为什么必须是软删"的直接证据：物理删掉 product.spu 那一行之后，
    所有指向它的订单项就都悬空了。
    """
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    await _pay(client, ctx, order["orderMainNo"])

    # 软删要求先下架（在售的不让直接删）
    spu = (await client.get("/api/merchant/spus", headers=ctx["merchant_headers"])).json()["data"][
        "items"
    ][0]
    off = await client.post(
        f"/api/merchant/spus/{spu['id']}/off-shelf", headers=ctx["merchant_headers"]
    )
    assert off.status_code == 200, off.text
    gone = await client.delete(f"/api/merchant/spus/{spu['id']}", headers=ctx["merchant_headers"])
    assert gone.status_code == 200, gone.text

    # 买家侧：订单还在，商品名/规格/金额一个不少
    detail = (
        await client.get(f"/api/orders/{order['orderMainNo']}", headers=ctx["buyer_headers"])
    ).json()["data"]
    items = detail["subs"][0]["items"]
    assert len(items) == 1
    assert items[0]["title"] == spu["title"]
    assert items[0]["skuId"] == ctx["sku_ids"][0]

    # 商家仍然能把这一单发出去 —— 发货读订单，不读商品
    shipped = await client.post(
        f"/api/merchant/order-subs/{order['subs'][0]['orderSubNo']}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": "SF-DEL-0001"},
        headers=ctx["merchant_headers"],
    )
    assert shipped.status_code == 200, shipped.text


async def test_cannot_ship_unpaid_order(client: AsyncClient, session) -> None:
    """没付款不能发货。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    sub_no = order["subs"][0]["orderSubNo"]

    resp = await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": "SF000003"},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 422


async def test_receive_is_scoped_to_owner(client: AsyncClient, session) -> None:
    """别人不能替你确认收货。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    sub_no = order["subs"][0]["orderSubNo"]
    await _pay(client, ctx, order["orderMainNo"])
    await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": "SF000004"},
        headers=ctx["merchant_headers"],
    )

    other = await register(client, phone=BUYER2_PHONE)
    resp = await client.post(
        f"/api/order-subs/{sub_no}/receive", headers=auth_header(other["accessToken"])
    )
    assert resp.status_code == 404


# ============================================================
# ⑤ 取消与超时关单
# ============================================================
async def test_cancel_releases_stock_and_coupon(client: AsyncClient, session) -> None:
    """取消订单要同时：关母单 → 关子单 → 回补库存 → 解锁券 → 关支付单。"""
    ctx = await _prepare(client, session)
    sku_id = int(ctx["sku_ids"][0])

    tpl_id = await _make_coupon()
    code_id = await _receive_coupon(tpl_id, ctx["buyer_id"], "receive-1")

    order = await _create_order(client, ctx, coupon_ids=[code_id])
    main_no = order["orderMainNo"]

    # 用了券：实付比原价 + 运费少 20 元
    assert order["payableAmount"] == SKU_PRICE + FREIGHT_FIRST - 2000
    assert await _available(sku_id, ctx["wh_id"]) == 99
    async with get_session_factory()() as s:
        assert (
            int(
                await s.scalar(
                    text("SELECT status FROM promotion.coupon_code WHERE id = :c"),
                    {"c": code_id},
                )
            )
            == 2
        ), "下单后券应当被锁定"

    # 先发起支付（会建支付单），再取消 —— 支付单也要一起关掉
    await client.post(
        "/api/payments", json={"orderMainNo": main_no}, headers=ctx["buyer_headers"]
    )

    resp = await client.post(f"/api/orders/{main_no}/cancel", headers=ctx["buyer_headers"])
    assert resp.status_code == 200, resp.text

    main_status, sub_status = await _db_statuses(main_no)
    assert main_status == ORDER_CLOSED and sub_status == ORDER_CLOSED
    assert await _available(sku_id, ctx["wh_id"]) == 100, "库存要回补"

    async with get_session_factory()() as s:
        coupon_status = int(
            await s.scalar(
                text("SELECT status FROM promotion.coupon_code WHERE id = :c"), {"c": code_id}
            )
        )
        pay_status = int(
            await s.scalar(
                text("SELECT status FROM payment.payment WHERE order_main_no = :n"), {"n": main_no}
            )
        )
    assert coupon_status == 1, "券要回到未使用"
    assert pay_status == 2, "支付单要一起关闭"


async def test_cancel_is_idempotent(client: AsyncClient, session) -> None:
    """重复取消不是错误 —— 前端刷新重试、网络重发都会走到这里。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]

    a = await client.post(f"/api/orders/{main_no}/cancel", headers=ctx["buyer_headers"])
    b = await client.post(f"/api/orders/{main_no}/cancel", headers=ctx["buyer_headers"])
    assert a.status_code == 200 and b.status_code == 200


async def test_cannot_cancel_paid_order(client: AsyncClient, session) -> None:
    """已付款的订单不能直接取消（要走退款）。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]
    await _pay(client, ctx, main_no)

    resp = await client.post(f"/api/orders/{main_no}/cancel", headers=ctx["buyer_headers"])
    assert resp.status_code == 422


async def test_timeout_scan_closes_expired_orders(client: AsyncClient, session) -> None:
    """★ 把支付截止时间改到过去 → 兜底扫描关单。

    这条路径是延迟任务丢失时的保命符：订单超时不关会一直占着库存和券。
    """
    ctx = await _prepare(client, session)
    sku_id = int(ctx["sku_ids"][0])
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE trade.order_main SET pay_deadline = now() - interval '1 minute' "
                "WHERE order_main_no = :n"
            ),
            {"n": main_no},
        )

    async with get_session_factory()() as s:
        closed = await trade_service.close_timeout_orders(s)
        await s.commit()
    assert closed == 1

    main_status, sub_status = await _db_statuses(main_no)
    assert main_status == ORDER_CLOSED and sub_status == ORDER_CLOSED
    assert await _available(sku_id, ctx["wh_id"]) == 100

    # 再扫一次不会重复处理（CAS 关母单返回 0 行）
    async with get_session_factory()() as s:
        assert await trade_service.close_timeout_orders(s) == 0


async def test_timeout_scan_skips_paid_orders(client: AsyncClient, session) -> None:
    """已支付的订单即使 pay_deadline 过了也不该被关掉。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]
    await _pay(client, ctx, main_no)

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE trade.order_main SET pay_deadline = now() - interval '1 minute' "
                "WHERE order_main_no = :n"
            ),
            {"n": main_no},
        )

    async with get_session_factory()() as s:
        assert await trade_service.close_timeout_orders(s) == 0
        await s.commit()

    main_status, _ = await _db_statuses(main_no)
    assert main_status == ORDER_WAIT_DELIVER


async def test_auto_receive_after_deadline(client: AsyncClient, session) -> None:
    """发货后超过自动收货时间 → cron 确认收货。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    main_no = order["orderMainNo"]
    sub_no = order["subs"][0]["orderSubNo"]
    await _pay(client, ctx, main_no)
    await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": "SF000005"},
        headers=ctx["merchant_headers"],
    )

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE trade.order_sub SET auto_finish_time = now() - interval '1 day' "
                "WHERE order_sub_no = :n"
            ),
            {"n": sub_no},
        )

    async with get_session_factory()() as s:
        done = await trade_service.auto_receive_expired(s)
        await s.commit()
    assert done == 1

    main_status, sub_status = await _db_statuses(main_no)
    assert sub_status == ORDER_FINISHED and main_status == ORDER_FINISHED


async def test_auto_receive_skips_unshipped(client: AsyncClient, session) -> None:
    """还没发货的子单不该被自动收货 —— auto_finish_time 是下单时就写好的。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    await _pay(client, ctx, order["orderMainNo"])

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE trade.order_sub SET auto_finish_time = now() - interval '1 day'")
        )
    async with get_session_factory()() as s:
        assert await trade_service.auto_receive_expired(s) == 0
        await s.commit()


# ============================================================
# ⑥ 并发状态变更（三重保护）
# ============================================================
async def test_concurrent_ship_only_one_wins(client: AsyncClient, session) -> None:
    """★ 两个请求同时给同一子单发货，只能有一个成功。

    三重保护在这里同时起作用：行锁 + 状态机 + CAS。
    """
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    sub_no = order["subs"][0]["orderSubNo"]
    await _pay(client, ctx, order["orderMainNo"])

    async def _ship(express_no: str) -> str:
        async with get_session_factory()() as s:
            try:
                await trade_service.ship(
                    s,
                    shop_id=ctx["shop_id"],
                    order_sub_no=sub_no,
                    express_company="顺丰速运",
                    express_no=express_no,
                )
                await s.commit()
                return "OK"
            except BizError:
                await s.rollback()
                return "REJECTED"

    results = await asyncio.gather(_ship("SF-A"), _ship("SF-B"))
    assert sorted(results) == ["OK", "REJECTED"], f"应当恰好一个成功，实际 {results}"

    async with get_session_factory()() as s:
        deliveries = await s.scalar(
            text("SELECT count(*) FROM trade.delivery_order WHERE order_sub_no = :n"),
            {"n": sub_no},
        )
    assert deliveries == 1, "只该有一张发货单"


# ============================================================
# ⑦ 金额守恒由数据库兜底
# ============================================================
async def test_db_rejects_unbalanced_order(client: AsyncClient, session) -> None:
    """★ 金额不平的订单**写不进数据库**。

    应用层的守恒校验（``_assert_conservation``）是第一道；这里是最后一道 ——
    即使哪天代码改错了，CHECK 约束也不会让不平的数据落库。
    """
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)

    with pytest.raises(IntegrityError):
        async with get_session_factory()() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE trade.order_main SET payable_amount = payable_amount + 1 "
                    "WHERE order_main_no = :n"
                ),
                {"n": order["orderMainNo"]},
            )


async def test_db_rejects_duplicate_request_id(client: AsyncClient, session) -> None:
    """同一用户的同一幂等键在数据库层是唯一的 —— Redis 幂等键丢了也挡得住。"""
    ctx = await _prepare(client, session)
    await _create_order(client, ctx, idem="unique-key")

    with pytest.raises(IntegrityError):
        async with get_session_factory()() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO trade.order_main ("
                    "id, order_main_no, user_id, request_id, shop_count, total_amount,"
                    " item_discount, shop_discount, platform_discount, coupon_amount,"
                    " point_deduction, point_used, freight_amount, payable_amount,"
                    " paid_amount, refunded_amount, status, pay_status, receiver_name,"
                    " receiver_phone, receiver_province, receiver_city, receiver_district,"
                    " receiver_detail, region_code, freight_detail, order_source, pay_deadline"
                    ") VALUES ("
                    ":id, 'M0000000000000000000', :u, 'unique-key', 1, 100,"
                    " 0, 0, 0, 0, 0, 0, 0, 100, 0, 0, 10, 0, 'x', 'y', 'p', 'c', 'd',"
                    " 'e', '310115', '{}'::jsonb, 4, now() + interval '30 min')"
                ),
                {"id": next_id(), "u": ctx["buyer_id"]},
            )


# ============================================================
# ⑧ 列表与分页
# ============================================================
async def test_my_orders_list_and_cursor(client: AsyncClient, session) -> None:
    """我的订单：按状态筛、游标分页不重不漏。"""
    ctx = await _prepare(client, session)
    for i in range(3):
        await _create_order(client, ctx, idem=f"list-{i}")

    resp = await client.get("/api/orders?limit=2", headers=ctx["buyer_headers"])
    data = resp.json()["data"]
    assert len(data["items"]) == 2
    assert data["hasMore"] is True
    assert data["nextCursor"]

    first_page = [i["orderMainNo"] for i in data["items"]]

    resp = await client.get(
        f"/api/orders?limit=2&cursor={data['nextCursor']}", headers=ctx["buyer_headers"]
    )
    data2 = resp.json()["data"]
    assert len(data2["items"]) == 1
    assert data2["hasMore"] is False
    assert data2["nextCursor"] is None

    seen = first_page + [i["orderMainNo"] for i in data2["items"]]
    assert len(set(seen)) == 3, "游标翻页不该重复"

    # 按状态筛：全部是待付款
    resp = await client.get(
        f"/api/orders?status={ORDER_WAIT_PAY}", headers=ctx["buyer_headers"]
    )
    assert len(resp.json()["data"]["items"]) == 3
    resp = await client.get(
        f"/api/orders?status={ORDER_FINISHED}", headers=ctx["buyer_headers"]
    )
    assert resp.json()["data"]["items"] == []


async def test_order_detail_hides_others_orders(client: AsyncClient, session) -> None:
    """★ 拿别人的订单号查不到 —— 订单号里的 Luhn 只挡手输错误，不挡恶意查询。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)

    other = await register(client, phone=BUYER2_PHONE)
    resp = await client.get(
        f"/api/orders/{order['orderMainNo']}", headers=auth_header(other["accessToken"])
    )
    assert resp.status_code == 404
    # 刻意不返回 403：不泄露"这个订单号确实存在"
    assert resp.json()["code"] == "ORDER_ITEM_NOT_FOUND"


async def test_merchant_order_list_shows_only_own_shop(client: AsyncClient, session) -> None:
    """商家列表只看得到自己店铺的子单（走 idx_order_sub_shop）。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    await _pay(client, ctx, order["orderMainNo"])

    resp = await client.get("/api/merchant/orders", headers=ctx["merchant_headers"])
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 1

    item = items[0]
    assert item["orderSubNo"] == order["subs"][0]["orderSubNo"]
    assert item["shopId"] == str(ctx["shop_id"])
    assert item["canShip"] is True
    # 商家要发货就必须能看到收货人
    assert item["buyerName"] == "张三"
    assert "浦东新区" in item["receiverFull"]

    # 按状态筛
    resp = await client.get(
        f"/api/merchant/orders?status={ORDER_WAIT_DELIVER}", headers=ctx["merchant_headers"]
    )
    assert len(resp.json()["data"]["items"]) == 1
    resp = await client.get(
        f"/api/merchant/orders?status={ORDER_FINISHED}", headers=ctx["merchant_headers"]
    )
    assert resp.json()["data"]["items"] == []


async def test_merchant_order_detail(client: AsyncClient, session) -> None:
    """商家订单详情：带收货信息与商品明细，别人查不到。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    sub_no = order["subs"][0]["orderSubNo"]
    await _pay(client, ctx, order["orderMainNo"])

    resp = await client.get(
        f"/api/merchant/orders/{sub_no}", headers=ctx["merchant_headers"]
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["orderSubNo"] == sub_no
    assert data["buyerName"] == "张三"
    assert data["canShip"] is True
    assert data["itemKindCount"] == 1
    assert "浦东新区" in data["receiverFull"]

    # 另一个商家（或买家）拿同一个子单号查不到 —— 不泄露他人订单
    other = await register(client, phone=BUYER2_PHONE)
    resp = await client.get(
        f"/api/merchant/orders/{sub_no}", headers=auth_header(other["accessToken"])
    )
    assert resp.status_code == 403  # 没开店，进不来商家端


async def test_merchant_order_detail_hides_other_shops(client: AsyncClient, session) -> None:
    """★ 别人的店铺子单查不到，返回"不存在"而不是"无权限"。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)
    sub_no = order["subs"][0]["orderSubNo"]

    # 再开一家店，用它的身份去查第一家的子单
    other_merchant = await register(client, phone="13800138031")
    await open_shop(client, other_merchant["accessToken"], "另一家店")
    resp = await client.get(
        f"/api/merchant/orders/{sub_no}",
        headers=auth_header(other_merchant["accessToken"]),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "ORDER_ITEM_NOT_FOUND"


def _assert_balanced(row: dict[str, int]) -> None:
    assert row["total"] == row["available"] + row["locked"] + row["frozen"], f"库存恒等式被破坏：{row}"


async def test_stock_lifecycle_locked_to_frozen_to_deducted(client: AsyncClient, session) -> None:
    """★ 库存走完四步：可售 → 预占 → 冻结 → 实扣。

    每一步各自动哪一格是这个模块最容易接错的地方：
    - **下单**只把 available 挪进 locked（总量不变，货还在仓里）
    - **支付**只把 locked 挪进 frozen（钱收了，但还没出库）
    - **发货**才真正把 frozen 扣掉（total 变小）
    漏掉中间任何一步，账面上都会出现对不上的格子。
    """
    ctx = await _prepare(client, session)
    sku_id = int(ctx["sku_ids"][0])
    wh = ctx["wh_id"]

    before = await _stock_row(sku_id, wh)
    _assert_balanced(before)
    assert before == {"total": 100, "available": 100, "locked": 0, "frozen": 0}

    # ① 下单 2 件 → available 少 2，locked 多 2，总量不变
    order = await _create_order(client, ctx, num=2)
    sub_no = order["subs"][0]["orderSubNo"]

    after_order = await _stock_row(sku_id, wh)
    _assert_balanced(after_order)
    assert after_order == {"total": 100, "available": 98, "locked": 2, "frozen": 0}, after_order

    # ② 支付 → locked 转 frozen
    await _pay(client, ctx, order["orderMainNo"])

    after_pay = await _stock_row(sku_id, wh)
    _assert_balanced(after_pay)
    assert after_pay == {"total": 100, "available": 98, "locked": 0, "frozen": 2}, after_pay

    # ③ 发货 → frozen 真正扣掉，总量才变小
    resp = await client.post(
        f"/api/merchant/order-subs/{sub_no}/ship",
        json={"expressCompany": "顺丰速运", "expressNo": "SF-LIFE-1"},
        headers=ctx["merchant_headers"],
    )
    assert resp.status_code == 200, resp.text

    after_ship = await _stock_row(sku_id, wh)
    _assert_balanced(after_ship)
    assert after_ship == {"total": 98, "available": 98, "locked": 0, "frozen": 0}, after_ship


async def test_stock_released_on_cancel(client: AsyncClient, session) -> None:
    """未支付关单：locked 退回 available，总量不变。"""
    ctx = await _prepare(client, session)
    sku_id = int(ctx["sku_ids"][0])
    wh = ctx["wh_id"]

    order = await _create_order(client, ctx, num=3)
    mid = await _stock_row(sku_id, wh)
    assert mid == {"total": 100, "available": 97, "locked": 3, "frozen": 0}, mid

    await client.post(f"/api/orders/{order['orderMainNo']}/cancel", headers=ctx["buyer_headers"])

    after = await _stock_row(sku_id, wh)
    _assert_balanced(after)
    assert after == {"total": 100, "available": 100, "locked": 0, "frozen": 0}, after


# ============================================================
# ⑨ 库存不足
# ============================================================
async def test_order_rejected_when_stock_insufficient(client: AsyncClient, session) -> None:
    """库存不够就下不了单 —— 预占在 Redis + DB 两侧都拦。"""
    ctx = await _prepare(client, session)
    sku_id = int(ctx["sku_ids"][0])

    # 把库存锁到只剩 1 件
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE inventory.sku_stock SET total = 1, available = 1 "
                "WHERE sku_id = :k AND warehouse_id = :w"
            ),
            {"k": sku_id, "w": ctx["wh_id"]},
        )

    resp = await client.post(
        "/api/orders",
        json={"items": [{"skuId": str(sku_id), "num": 5}], "addressId": ctx["address_id"]},
        headers={**ctx["buyer_headers"], "Idempotency-Key": "over-stock"},
    )
    assert resp.status_code in (410, 422), resp.text


async def test_order_item_snapshot_carries_fallback_cover(
    client: AsyncClient, session
) -> None:
    """★ 下单快照存的必须是**回落后的**封面图（SKU 没设图就用商品主图）。

    快照写的是算价行里的 ``cover_image``，而那个值来自
    ``product.service.batch_get_skus``。少了这层回落，订单详情和售后页
    （读的是同一份快照）里就全是空白图。
    """
    ctx = await _prepare(client, session)
    sku_id = int(ctx["sku_ids"][0])
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE product.sku SET cover_image = '' WHERE id = :id"), {"id": sku_id}
        )

    order = await _create_order(client, ctx)

    async with get_session_factory()() as s:
        img = await s.scalar(
            text("SELECT cover_image_snap FROM trade.order_item WHERE order_main_no = :n"),
            {"n": order["orderMainNo"]},
        )
    assert img == "/media/ip16.webp", "快照该是回落后的商品主图，不是空串"
