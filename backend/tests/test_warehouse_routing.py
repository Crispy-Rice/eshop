"""多仓 + 按收货区划路由的集成测试。连真实 PG + Redis，走完整 HTTP 链路。

这一块最容易出的错不是"报错"，而是**发错货**或**账面对不上** —— 所以这里既断言
"订单上记的仓"，也断言"库存扣在了哪一行"。

覆盖 docs/03 的「多仓与路由」：规则优先、**规则仓缺货时按候选链兜底**、仓记在子单上、
发货与售后读记录值（而不是重新择仓）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.db import get_session_factory
from app.core.redis import get_redis
from app.modules.inventory import service as inv
from app.modules.trade import service as trade_service
from tests.conftest import auth_header, make_admin, open_shop, register
from tests.test_product import _make_category, _spu_payload

MERCHANT_PHONE = "13800138040"
BUYER_PHONE = "13900139040"

SH_REGION = "310115"  # 上海市浦东新区
GD_REGION = "440305"  # 广东省深圳市南山区


@pytest.fixture(autouse=True)
async def clean_redis(app_runtime: None) -> AsyncIterator[None]:
    """库存与券的 Redis 是另一套状态，跨用例不清会读到上一个用例的分片。"""
    redis = get_redis()
    keys: list[str] = []
    for pattern in ("stock:*", "lock:stock_init:*", "coupon:*"):
        async for key in redis.scan_iter(match=pattern):
            keys.append(key)
    if keys:
        await redis.delete(*keys)
    yield


# ============================================================
# 造一个"一店两仓"的可下单环境
# ============================================================
async def _make_address(client: AsyncClient, headers: dict, region: str) -> str:
    province, city, district = {
        SH_REGION: ("上海市", "上海市", "浦东新区"),
        GD_REGION: ("广东省", "深圳市", "南山区"),
    }[region]
    resp = await client.post(
        "/api/me/addresses",
        json={
            "receiverName": "张三",
            "phone": "13800138000",
            "province": province,
            "city": city,
            "district": district,
            "detail": "某某路 1 号",
            "regionCode": region,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def _setup(client: AsyncClient, session) -> dict:
    """默认仓（上海，发全国）+ 华南仓（覆盖广东）。

    ★ 商店开店时自带一条**默认运费模板**，所以新仓不需要额外绑模板 ——
      ``freight.estimate`` 对"没绑定"的 SKU 会回落到店铺默认模板。
    """
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")

    merchant = await register(client, phone=MERCHANT_PHONE)
    shop_id = int(await open_shop(client, merchant["accessToken"]))
    mh = auth_header(merchant["accessToken"])

    resp = await client.post("/api/merchant/spus", json=_spu_payload(category), headers=mh)
    assert resp.status_code == 200, resp.text
    spu_id = resp.json()["data"]["id"]
    sku_ids = [s["id"] for s in resp.json()["data"]["skus"]]
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=mh)
    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": True},
        headers=auth_header(admin["accessToken"]),
    )

    first = (
        await client.post(
            "/api/merchant/warehouses",
            json={
                "name": "上海总仓",
                "province": "上海市",
                "city": "上海市",
                "district": "浦东新区",
                "regionCode": SH_REGION,
                "detail": "某某路 1 号",
                "contactName": "王五",
                "contactPhone": "13900000001",
            },
            headers=mh,
        )
    ).json()["data"]
    assert first["isDefault"] is True, "第一个建出来的仓自动是默认仓"
    # ★ 建仓顺手给该店全部 SKU 在这个仓补了 0 库存行
    assert first["stockedSkus"] == len(sku_ids)
    default_wh = first["id"]

    south = (
        await client.post(
            "/api/merchant/warehouses",
            json={
                "name": "华南仓",
                "province": "广东省",
                "city": "广州市",
                "district": "天河区",
                "regionCode": "440106",
            },
            headers=mh,
        )
    ).json()["data"]
    south_wh = south["id"]
    regions = await client.put(
        f"/api/merchant/warehouses/{south_wh}/regions", json={"rules": ["44"]}, headers=mh
    )
    assert regions.status_code == 200, regions.text

    # 两个仓都铺上货（用 init 直接给量，省掉逐条 adjust 的幂等键）
    async with get_session_factory()() as s, s.begin():
        for index, sku_id in enumerate(sku_ids):
            await inv.init(
                s, sku_id=int(sku_id), warehouse_id=int(default_wh), qty=50,
                biz_key=f"wh-def:{index}",
            )
            await inv.init(
                s, sku_id=int(sku_id), warehouse_id=int(south_wh), qty=50,
                biz_key=f"wh-south:{index}",
            )

    buyer = await register(client, phone=BUYER_PHONE)
    bh = auth_header(buyer["accessToken"])
    return {
        "shop_id": shop_id,
        "mh": mh,
        "bh": bh,
        "spu_id": spu_id,
        "sku_ids": sku_ids,
        "default_wh": default_wh,
        "south_wh": south_wh,
        "sh_addr": await _make_address(client, bh, SH_REGION),
        "gd_addr": await _make_address(client, bh, GD_REGION),
    }


async def _order(
    client: AsyncClient, ctx: dict, address_id: str, *, idem: str, sku_index: int = 0
) -> dict:
    resp = await client.post(
        "/api/orders",
        json={"items": [{"skuId": ctx["sku_ids"][sku_index], "num": 1}], "addressId": address_id},
        headers={**ctx["bh"], "Idempotency-Key": idem},
    )
    return resp


async def _pay(client: AsyncClient, ctx: dict, order_main_no: str) -> None:
    pay = await client.post(
        "/api/payments", json={"orderMainNo": order_main_no}, headers=ctx["bh"]
    )
    pay_no = pay.json()["data"]["payNo"]
    assert (
        await client.post(f"/api/payments/{pay_no}/mock-callback", json={})
    ).status_code == 200


async def _sub_of(order_main_no: str) -> tuple[str, int | None]:
    """母单下唯一那个子单的 (单号, 发货仓)。"""
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT order_sub_no, warehouse_id FROM trade.order_sub "
                    "WHERE order_main_no = :n"
                ),
                {"n": order_main_no},
            )
        ).one()
    return str(row[0]), (int(row[1]) if row[1] is not None else None)


async def _available(warehouse_id: str, sku_id: str) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text(
                    "SELECT available FROM inventory.sku_stock "
                    "WHERE warehouse_id = :w AND sku_id = :k"
                ),
                {"w": int(warehouse_id), "k": int(sku_id)},
            )
        )


async def _empty_stock(warehouse_id: str, sku_id: str) -> None:
    """把这个仓对这个 SKU 的**可售量**清成 0 —— 用来表达"这个仓没货"。

    ★ 直接改库而不是走 ``adjust``：`adjust` 要求行已存在、还要幂等键，而这里要的只是
      "没货"这个**状态**。
    ★ ``total`` 必须跟着 ``locked + frozen`` 走（表上的恒等式 CHECK
      ``total = available + locked + frozen`` 会拦）：前面已经下过的单可能留了预占，
      那种情况下 total 不能清成 0，只能把"可售"那部分抹掉。
    """
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE inventory.sku_stock"
                " SET available = 0, total = locked + frozen, version = version + 1"
                " WHERE warehouse_id = :w AND sku_id = :k"
            ),
            {"w": int(warehouse_id), "k": int(sku_id)},
        )


async def _freight_of(order_main_no: str) -> int:
    """母单下那个子单的运费（分）—— 验证"兜底换仓不改运费"用。"""
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT freight_amount FROM trade.order_sub WHERE order_main_no = :n"),
                {"n": order_main_no},
            )
        )


async def _stock_row_count(warehouse_id: str) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT count(*) FROM inventory.sku_stock WHERE warehouse_id = :w"),
                {"w": int(warehouse_id)},
            )
        )


# ============================================================
# 路由：按收货区划选仓
# ============================================================
async def test_order_routes_by_region_and_records_the_warehouse(
    client: AsyncClient, session
) -> None:
    """上海地址走默认仓、广东地址走华南仓 —— 而且**订单上记着这个仓**。

    断言三件事：订单子单的 ``warehouse_id``、库存扣在了哪一行、另一个仓没被动过。
    """
    ctx = await _setup(client, session)

    sh = await _order(client, ctx, ctx["sh_addr"], idem="sh-1")
    assert sh.status_code == 200, sh.text
    sh_main = sh.json()["data"]["orderMainNo"]
    _, sh_wh = await _sub_of(sh_main)
    assert sh_wh == int(ctx["default_wh"]), "上海该走默认仓"

    gd = await _order(client, ctx, ctx["gd_addr"], idem="gd-1")
    assert gd.status_code == 200, gd.text
    gd_main = gd.json()["data"]["orderMainNo"]
    _, gd_wh = await _sub_of(gd_main)
    assert gd_wh == int(ctx["south_wh"]), "广东该走华南仓"

    # 预占落在各自的仓上：50 → 49，另一半仍是 50
    sku = ctx["sku_ids"][0]
    assert await _available(ctx["default_wh"], sku) == 49
    assert await _available(ctx["south_wh"], sku) == 49
    # 另一件 SKU 谁都没碰
    assert await _available(ctx["default_wh"], ctx["sku_ids"][1]) == 50


async def test_rule_change_does_not_move_inflight_orders(client: AsyncClient, session) -> None:
    """商家改区域规则**不影响在途订单**：它读的是订单上记录的仓。

    ★ 这是"仓记在子单上"的全部意义。若发货/售后还去重新路由，
      下面这一改就会让退货回到一个当初根本没发过货的仓。
    """
    ctx = await _setup(client, session)
    gd = await _order(client, ctx, ctx["gd_addr"], idem="gd-2")
    gd_main = gd.json()["data"]["orderMainNo"]
    sub_no, gd_wh = await _sub_of(gd_main)
    assert gd_wh == int(ctx["south_wh"])

    # 广东改回默认仓发货
    assert (
        await client.put(
            f"/api/merchant/warehouses/{ctx['south_wh']}/regions",
            json={"rules": []},
            headers=ctx["mh"],
        )
    ).status_code == 200

    # 老订单：仍然指向华南仓
    async with get_session_factory()() as s:
        assert await trade_service.warehouse_id_of_sub(s, sub_no) == int(ctx["south_wh"])

    # 新订单：按新规则走默认仓
    fresh = await _order(client, ctx, ctx["gd_addr"], idem="gd-3")
    _, fresh_wh = await _sub_of(fresh.json()["data"]["orderMainNo"])
    assert fresh_wh == int(ctx["default_wh"])


async def test_default_warehouse_is_the_fallback(client: AsyncClient, session) -> None:
    """没命中任何规则的地址 → 默认仓。"""
    ctx = await _setup(client, session)
    resp = await _order(client, ctx, ctx["sh_addr"], idem="sh-fallback")
    _, wh = await _sub_of(resp.json()["data"]["orderMainNo"])
    assert wh == int(ctx["default_wh"])


# ============================================================
# 履约：发货单记真实仓
# ============================================================
async def test_delivery_records_the_routed_warehouse(client: AsyncClient, session) -> None:
    """发货单上写的是**子单的仓**（改造前这里硬编码 0）。"""
    ctx = await _setup(client, session)
    gd = await _order(client, ctx, ctx["gd_addr"], idem="gd-ship")
    main_no = gd.json()["data"]["orderMainNo"]
    await _pay(client, ctx, main_no)

    resp = await client.post(
        f"/api/merchant/order-subs/{main_no}-1/ship",
        json={"expressCompany": "顺丰", "expressNo": "SF123456789"},
        headers=ctx["mh"],
    )
    assert resp.status_code == 200, resp.text

    async with get_session_factory()() as s:
        wh = await s.scalar(
            text(
                "SELECT warehouse_id FROM trade.delivery_order WHERE order_main_no = :n"
            ),
            {"n": main_no},
        )
    assert int(wh) == int(ctx["south_wh"]), "发货单要记华南仓，不再是 0"


async def test_legacy_order_without_warehouse_falls_back_to_default(
    client: AsyncClient, session
) -> None:
    """迁移前的老单 ``warehouse_id`` 为空 → 回退**默认仓**。

    ★ 用上海地址下单：老单是"一期单仓"时代的产物，那时货就在默认仓里，
      所以"回退默认仓"才能与库存实际所在的那一行对上。
      （反过来说：如果硬把一张**路由到华南仓**的新单抹掉仓号，发货就会拿默认仓去扣
      华南仓预占的货 —— 那本来就不是这个兼容分支要处理的情形。）
    """
    ctx = await _setup(client, session)
    order = await _order(client, ctx, ctx["sh_addr"], idem="legacy-1")
    main_no = order.json()["data"]["orderMainNo"]
    await _pay(client, ctx, main_no)

    # 模拟"迁移前的老单"：把仓号抹掉
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE trade.order_sub SET warehouse_id = NULL WHERE order_main_no = :n"),
            {"n": main_no},
        )

    resp = await client.post(
        f"/api/merchant/order-subs/{main_no}-1/ship",
        json={"expressCompany": "顺丰", "expressNo": "SF987654321"},
        headers=ctx["mh"],
    )
    assert resp.status_code == 200, resp.text
    async with get_session_factory()() as s:
        wh = await s.scalar(
            text("SELECT warehouse_id FROM trade.delivery_order WHERE order_main_no = :n"),
            {"n": main_no},
        )
    assert int(wh) == int(ctx["default_wh"]), "没记录时回退默认仓（当初也是它）"


# ============================================================
# 库存行：预建 + 惰性补齐
# ============================================================
async def test_new_sku_without_stock_anywhere_is_rejected(
    client: AsyncClient, session
) -> None:
    """一个**哪个仓都没货**的商品 → 拒单，而且文案要顶用。

    ★ 建仓预建库存行只覆盖"建仓那一刻已存在"的 SKU，**之后**发布的商品在哪个仓都没有
      库存行。v1 的文案是「「华南仓」库存不足……**可以更换收货地址**」—— 兜底之后
      那句成了**假话**：候选链已经把该店所有启用的仓都试过了，换地址没有用。现在说的是
      "库存不足，可以联系商家补货"。
    ★ 恢复路径是**库存页**：商家筛到某个仓，`sync_missing` 会补出 0 库存行；他填上数量
      之后这单就能下了 —— 下面把这条路径走完。
    """
    ctx = await _setup(client, session)

    # 再发一件新商品（两个仓都已经存在，所以建仓预建覆盖不到它）
    category_id = (
        await client.get(f"/api/merchant/spus/{ctx['spu_id']}", headers=ctx["mh"])
    ).json()["data"]["categoryId"]
    resp = await client.post(
        "/api/merchant/spus",
        json={**_spu_payload(category_id), "title": "后上架的耳机"},
        headers=ctx["mh"],
    )
    assert resp.status_code == 200, resp.text
    new_spu_id = resp.json()["data"]["id"]
    new_sku = resp.json()["data"]["skus"][0]["id"]
    # 要走一遍上架（草稿商品下不了单）
    await client.post(f"/api/merchant/spus/{new_spu_id}/submit", headers=ctx["mh"])
    admin = await make_admin(client, session, phone="13900139041")
    assert (
        await client.post(
            f"/api/admin/spus/{new_spu_id}/audit",
            json={"approved": True},
            headers=auth_header(admin["accessToken"]),
        )
    ).status_code == 200

    # 华南仓还没有它的库存行
    assert await _stock_rows_of(ctx["south_wh"], new_sku) == 0

    # 广东省地址 → 候选链（华南仓 → 上海总仓 → …）**每个仓都没有它** → 拒单
    resp = await client.post(
        "/api/orders",
        json={"items": [{"skuId": new_sku, "num": 1}], "addressId": ctx["gd_addr"]},
        headers={**ctx["bh"], "Idempotency-Key": "new-sku-1"},
    )
    assert resp.status_code == 410, resp.text  # STOCK_SOLD_OUT 绑的是 410
    body = resp.json()
    assert body["code"] == "STOCK_SOLD_OUT"
    assert "库存不足" in body["message"], body["message"]
    assert "更换收货地址" not in body["message"], "兜底已试过所有仓，换地址没用"

    # ★ 恢复路径：商家把库存页筛到华南仓 → 该 SKU 的 0 库存行被补出来
    listing = await client.get(
        "/api/merchant/inventory",
        params={"warehouseId": ctx["south_wh"], "limit": 100},
        headers=ctx["mh"],
    )
    assert listing.status_code == 200, listing.text
    row = next((i for i in listing.json()["data"]["items"] if i["skuId"] == new_sku), None)
    assert row is not None, "库存页应该补出这个仓的库存行，商家才能给它调量"
    assert row["available"] == 0

    # 填上数量之后，同一个地址就下得了单，而且从补货的那个仓发
    async with get_session_factory()() as s, s.begin():
        await inv.init(
            s, sku_id=int(new_sku), warehouse_id=int(ctx["south_wh"]), qty=3,
            biz_key="new-sku-south",
        )
    resp = await client.post(
        "/api/orders",
        json={"items": [{"skuId": new_sku, "num": 1}], "addressId": ctx["gd_addr"]},
        headers={**ctx["bh"], "Idempotency-Key": "new-sku-2"},
    )
    assert resp.status_code == 200, resp.text
    _, warehouse = await _sub_of(resp.json()["data"]["orderMainNo"])
    assert warehouse == int(ctx["south_wh"]), "规则仓补上货之后应当从它发"


# ============================================================
# 缺货兜底：规则仓没货就从别的仓发
# ============================================================
async def test_falls_back_to_default_when_rule_warehouse_is_empty(
    client: AsyncClient, session
) -> None:
    """★ 本次改动的核心：广东规则仓（华南仓）没货 → 广东省地址**兜底到默认仓**。

    v1 在这里是"整单失败"（用户只能换地址）。现在候选链是
    「规则仓 → 默认仓 → 其余启用仓」，取第一个能一次盖住整单的仓。
    """
    ctx = await _setup(client, session)
    await _empty_stock(ctx["south_wh"], ctx["sku_ids"][0])  # 华南仓没货

    resp = await _order(client, ctx, ctx["gd_addr"], idem="fb-default-1")
    assert resp.status_code == 200, resp.text
    _, warehouse = await _sub_of(resp.json()["data"]["orderMainNo"])
    assert warehouse == int(ctx["default_wh"]), "规则仓没货时应当从兜底仓发"


async def test_falls_back_to_any_other_enabled_warehouse(client: AsyncClient, session) -> None:
    """规则仓与默认仓都没货 → 继续往候选链后面扫，扫到**任意启用仓**（按 id 升序）。

    这也是用户确认过的口径："能扫多远的兜底就扫多远"，而不是只兜到默认仓。
    """
    ctx = await _setup(client, session)
    sku = ctx["sku_ids"][0]
    third = (
        await client.post(
            "/api/merchant/warehouses",
            json={
                "name": "苏州备用仓",
                "province": "江苏省",
                "city": "苏州市",
                "district": "姑苏区",
                "regionCode": "320508",
            },
            headers=ctx["mh"],
        )
    ).json()["data"]["id"]
    async with get_session_factory()() as s, s.begin():
        await inv.init(
            s, sku_id=int(sku), warehouse_id=int(third), qty=7, biz_key="third-1"
        )

    # 规则仓（华南）与默认仓（上海）都没货，只有那个**没有任何规则**的备用仓有
    await _empty_stock(ctx["south_wh"], sku)
    await _empty_stock(ctx["default_wh"], sku)

    resp = await _order(client, ctx, ctx["gd_addr"], idem="fb-third-1")
    assert resp.status_code == 200, resp.text
    _, warehouse = await _sub_of(resp.json()["data"]["orderMainNo"])
    assert warehouse == int(third)


async def test_fallback_does_not_change_freight(client: AsyncClient, session) -> None:
    """★ 这条是整套设计的**前提**：兜底换仓**不改运费**，所以不会出现
    "算价一个价、下单另一个价"，也不需要价格确认弹窗。

    理由（docs/03 §12.1）：模板按 ``sku_id`` 取（**与仓无关**）、包裹按仓分组，而一个
    收货地址只落一个仓 —— 换仓之后仍然是"一个仓一个包裹"，重量与模板逐项不变。
    """
    ctx = await _setup(client, session)
    sku = ctx["sku_ids"][0]

    # 正常走法：广东省 → 华南仓
    first = await _order(client, ctx, ctx["gd_addr"], idem="fr-south")
    assert first.status_code == 200, first.text
    first_main = first.json()["data"]["orderMainNo"]
    _, warehouse_a = await _sub_of(first_main)
    assert warehouse_a == int(ctx["south_wh"])

    # 华南仓清空 → 同地址兜底到上海总仓
    await _empty_stock(ctx["south_wh"], sku)
    second = await _order(client, ctx, ctx["gd_addr"], idem="fr-fallback")
    assert second.status_code == 200, second.text
    second_main = second.json()["data"]["orderMainNo"]
    _, warehouse_b = await _sub_of(second_main)
    assert warehouse_b == int(ctx["default_wh"]), "第二单应当兜底了"

    assert await _freight_of(first_main) == await _freight_of(second_main), (
        "换了仓，运费必须一样 —— 否则兜底就成了一次静默涨价/降价"
    )


async def test_split_across_warehouses_is_rejected_with_a_usable_hint(
    client: AsyncClient, session
) -> None:
    """★ 兜底是**整单换仓**，不是逐件挑仓：这单的商品分散在不同仓、没有哪个仓能一次
    发齐 → 拒单，并提示"分开下单" —— **那真的管用**（择仓按整单判，单独下一单就会兜到
    有那个货的仓）。逐件挑仓要拆发货单，那会把交易粒度降到 (子单, SKU, 仓)，没做。
    """
    ctx = await _setup(client, session)
    sku_a, sku_b = ctx["sku_ids"][0], ctx["sku_ids"][1]
    # sku_a 只在默认仓（上海）有、sku_b 只在华南仓有 → 广东省地址下凑不到同一个仓
    await _empty_stock(ctx["south_wh"], sku_a)
    await _empty_stock(ctx["default_wh"], sku_b)

    resp = await client.post(
        "/api/orders",
        json={
            "items": [{"skuId": sku_a, "num": 1}, {"skuId": sku_b, "num": 1}],
            "addressId": ctx["gd_addr"],
        },
        headers={**ctx["bh"], "Idempotency-Key": "split-1"},
    )
    assert resp.status_code == 410, resp.text
    assert "分开下单" in resp.json()["message"], resp.json()["message"]

    # 分开下单真的能成
    one = await client.post(
        "/api/orders",
        json={"items": [{"skuId": sku_a, "num": 1}], "addressId": ctx["gd_addr"]},
        headers={**ctx["bh"], "Idempotency-Key": "split-a"},
    )
    assert one.status_code == 200, one.text
    _, warehouse_a = await _sub_of(one.json()["data"]["orderMainNo"])
    assert warehouse_a == int(ctx["default_wh"]), "sku_a 只有上海仓有 → 兜底过去"

    two = await client.post(
        "/api/orders",
        json={"items": [{"skuId": sku_b, "num": 1}], "addressId": ctx["gd_addr"]},
        headers={**ctx["bh"], "Idempotency-Key": "split-b"},
    )
    assert two.status_code == 200, two.text
    _, warehouse_b = await _sub_of(two.json()["data"]["orderMainNo"])
    assert warehouse_b == int(ctx["south_wh"]), "sku_b 只有华南仓有 → 规则仓本来就对"


async def test_checkout_can_submit_is_per_warehouse(client: AsyncClient, session) -> None:
    """结算页的可提交性**按仓**判：一个子单的货必须能落进同一个仓。

    ★ v1 用的是**跨仓求和** —— 于是"结算显示有货、下单才失败"。现在算价与下单用同一套
      判断，缺货时直接 ``canSubmit=false``，前端据此禁用提交按钮。
    """

    def calc(ctx: dict, address_id: str):
        return client.post(
            "/api/checkout/calc",
            json={"items": [{"skuId": ctx["sku_ids"][0], "num": 1}], "addressId": address_id},
            headers=ctx["bh"],
        )

    ctx = await _setup(client, session)
    ok = await calc(ctx, ctx["gd_addr"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["canSubmit"] is True

    # 两个仓都清空 → 哪儿都发不出
    await _empty_stock(ctx["south_wh"], ctx["sku_ids"][0])
    await _empty_stock(ctx["default_wh"], ctx["sku_ids"][0])
    blocked = await calc(ctx, ctx["gd_addr"])
    assert blocked.status_code == 200, blocked.text
    data = blocked.json()["data"]
    assert data["canSubmit"] is False
    assert any("库存不足" in n for n in data["notices"]), data["notices"]


async def _stock_rows_of(warehouse_id: str, sku_id: str) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM inventory.sku_stock "
                    "WHERE warehouse_id = :w AND sku_id = :k"
                ),
                {"w": int(warehouse_id), "k": int(sku_id)},
            )
        )


# ============================================================
# 仓库管理：停用与区域冲突
# ============================================================
async def test_disabled_warehouse_stops_new_orders_only(client: AsyncClient, session) -> None:
    """停用只影响**新订单**的路由；已经落在它上面的订单照常发货。"""
    ctx = await _setup(client, session)

    # 先下一单（广东 → 华南仓）
    gd = await _order(client, ctx, ctx["gd_addr"], idem="gd-disable")
    main_no = gd.json()["data"]["orderMainNo"]
    await _pay(client, ctx, main_no)

    # 停用华南仓
    assert (
        await client.post(
            f"/api/merchant/warehouses/{ctx['south_wh']}/status",
            json={"status": 2},
            headers=ctx["mh"],
        )
    ).status_code == 200

    # 新订单：广东不再走它，落到默认仓
    fresh = await _order(client, ctx, ctx["gd_addr"], idem="gd-after-disable")
    _, fresh_wh = await _sub_of(fresh.json()["data"]["orderMainNo"])
    assert fresh_wh == int(ctx["default_wh"]), "停用的仓不该再接新单"

    # 老订单：还是从它发，不受停用影响
    assert (
        await client.post(
            f"/api/merchant/order-subs/{main_no}-1/ship",
            json={"expressCompany": "顺丰", "expressNo": "SF000111222"},
            headers=ctx["mh"],
        )
    ).status_code == 200
    async with get_session_factory()() as s:
        wh = await s.scalar(
            text("SELECT warehouse_id FROM trade.delivery_order WHERE order_main_no = :n"),
            {"n": main_no},
        )
    assert int(wh) == int(ctx["south_wh"])


async def test_default_warehouse_cannot_be_disabled(client: AsyncClient, session) -> None:
    """默认仓不允许停用 —— 停了它，这个店就没有兜底仓了。"""
    ctx = await _setup(client, session)
    resp = await client.post(
        f"/api/merchant/warehouses/{ctx['default_wh']}/status",
        json={"status": 2},
        headers=ctx["mh"],
    )
    assert resp.status_code == 400, resp.text
    assert "默认仓" in resp.json()["message"]


async def test_region_can_only_belong_to_one_warehouse(client: AsyncClient, session) -> None:
    """一个区划只能由一个仓发货：抢别人的区划会拿到**说人话**的 400。"""
    ctx = await _setup(client, session)  # 华南仓已有 "44"
    resp = await client.put(
        f"/api/merchant/warehouses/{ctx['default_wh']}/regions",
        json={"rules": ["4403"]},  # 4403 是 "44" 的子集，但仍算同一片地方吗？
        headers=ctx["mh"],
    )
    # "4403" 与 "44" 是两个不同的码，允许共存（后者更具体、优先）
    assert resp.status_code == 200, resp.text

    # 但把已经被华南仓占着的 "44" 直接抢过来就不行了
    resp = await client.put(
        f"/api/merchant/warehouses/{ctx['default_wh']}/regions",
        json={"rules": ["44"]},
        headers=ctx["mh"],
    )
    assert resp.status_code == 400, resp.text
    assert "华南仓" in resp.json()["message"]

