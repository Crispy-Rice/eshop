"""cart 模块的集成测试。连真实 PostgreSQL。

购物车本身没有并发写竞争，所以这里重点验证的是**语义正确性**：
累加 vs 覆盖、上限封顶、越权隔离、实时状态判定、按店铺分组的小计。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import text

from app.core.crypto import phone_hash
from app.core.db import get_session_factory
from app.core.snowflake import next_id
from app.modules.cart.models import MAX_NUM_PER_SKU, MAX_SKUS_PER_CART
from app.modules.inventory import service as inv
from app.modules.inventory.models import Warehouse
from tests.conftest import auth_header, make_admin, open_shop, register
from tests.test_product import _make_category

BUYER_PHONE = "13900139005"  # 注意避开 conftest 里 make_admin 的默认号 13900139001
MERCHANT_PHONE = "13800138010"


async def _setup(client: AsyncClient, session) -> dict:
    """造好：商家 + 已上架商品（3 个 SKU）+ 买家 + 库存。返回各方句柄。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")

    merchant = await register(client, phone=MERCHANT_PHONE)
    shop_id = await open_shop(client, merchant["accessToken"])
    merchant_headers = auth_header(merchant["accessToken"])

    from tests.test_product import _spu_payload

    resp = await client.post(
        "/api/merchant/spus", json=_spu_payload(category), headers=merchant_headers
    )
    assert resp.status_code == 200, resp.text
    spu_id = resp.json()["data"]["id"]
    sku_ids = [s["id"] for s in resp.json()["data"]["skus"]]

    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=merchant_headers)
    await client.post(
        f"/api/admin/spus/{spu_id}/audit", json={"approved": True}, headers=auth_header(admin["accessToken"])
    )

    # 建仓 + 给每个 SKU 上 100 件库存（"有效"状态需要 available > 0）
    async with get_session_factory()() as s, s.begin():
        wh = Warehouse(id=next_id(), shop_id=int(shop_id), name="测试仓", is_default=True)
        s.add(wh)
        wh_id = wh.id
    async with get_session_factory()() as s, s.begin():
        for i, sku_id in enumerate(sku_ids):
            await inv.init(
                s, sku_id=int(sku_id), warehouse_id=wh_id, qty=100, biz_key=f"init:{i}"
            )

    buyer = await register(client, phone=BUYER_PHONE)
    return {
        "buyer_headers": auth_header(buyer["accessToken"]),
        "buyer_token": buyer["accessToken"],
        "merchant_headers": merchant_headers,
        "merchant_token": merchant["accessToken"],
        "sku_ids": sku_ids,
        "shop_id": shop_id,
        "spu_id": spu_id,
    }


async def _add(client: AsyncClient, headers: dict, sku_id: str, num: int = 1) -> dict:
    resp = await client.post("/api/cart/items", json={"skuId": sku_id, "num": num}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _cart(client: AsyncClient, headers: dict) -> dict:
    resp = await client.get("/api/cart", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _all_items(cart: dict) -> list[dict]:
    items = [i for g in cart["groups"] for i in g["items"]]
    return items + cart["invalidItems"]


def _find(cart: dict, sku_id: str) -> dict:
    for item in _all_items(cart):
        if item["skuId"] == sku_id:
            return item
    raise AssertionError(f"购物车里没有 sku {sku_id}")


async def _off_shelf(client: AsyncClient, ctx: dict) -> None:
    """下架整个 SPU。路径是 /off-shelf，别写成 /off。"""
    resp = await client.post(
        f"/api/merchant/spus/{ctx['spu_id']}/off-shelf", headers=ctx["merchant_headers"]
    )
    assert resp.status_code == 200, resp.text


async def _delete_sku(sku_id: str) -> None:
    """模拟"商品被删除"。

    要先删 ``sku_spec`` —— 它外键指向 ``sku``，直接删 SKU 会违反外键约束。
    真实场景里由商家删商品的流程处理，测试里手工做。
    """
    async with get_session_factory()() as s, s.begin():
        await s.execute(text("DELETE FROM product.sku_spec WHERE sku_id = :sku"), {"sku": int(sku_id)})
        await s.execute(text("DELETE FROM product.sku WHERE id = :sku"), {"sku": int(sku_id)})


# ============================================================
# ① 累加与上限
# ============================================================
async def test_add_accumulates_and_caps(client: AsyncClient, session) -> None:
    """同一 SKU 加购两次只有一行、num 累加；累计超过 200 时封顶在 200。"""
    ctx = await _setup(client, session)
    h, sku = ctx["buyer_headers"], ctx["sku_ids"][0]

    await _add(client, h, sku, 3)
    await _add(client, h, sku, 4)

    cart = await _cart(client, h)
    assert len(_all_items(cart)) == 1, "同一 SKU 只能有一行"
    assert _find(cart, sku)["num"] == 7, "加购应当是累加"

    # 再加 1000 件：受 Quantity 上限（200）限制，分多次加
    for _ in range(20):
        await _add(client, h, sku, 200)
    cart = await _cart(client, h)
    assert _find(cart, sku)["num"] == MAX_NUM_PER_SKU, "单 SKU 数量必须封顶在 200"


async def test_cart_full_limit(client: AsyncClient, session) -> None:
    """整车 SKU 种类上限。"""
    ctx = await _setup(client, session)
    h = ctx["buyer_headers"]

    async with get_session_factory()() as s:
        buyer_id = int(
            await s.scalar(
                text("SELECT id FROM account.user WHERE phone_hash = :h"),
                {"h": phone_hash(BUYER_PHONE)},
            )
        )

    # 直接把车塞满，避免真的发 100 次请求
    async with get_session_factory()() as s, s.begin():
        for i in range(MAX_SKUS_PER_CART):
            await s.execute(
                text(
                    "INSERT INTO cart.cart_item "
                    "(id, user_id, shop_id, sku_id, spu_id, price_snapshot, num) "
                    "VALUES (:id, :uid, 1, :sku, 1, 100, 1)"
                ),
                {"id": next_id(), "uid": buyer_id, "sku": 100000 + i},
            )

    resp = await client.post(
        "/api/cart/items", json={"skuId": ctx["sku_ids"][0], "num": 1}, headers=h
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "CART_FULL"


# ============================================================
# ② SET 语义
# ============================================================
async def test_update_num_is_set_not_accumulate(client: AsyncClient, session) -> None:
    """PUT 是"设为"，不是"累加"。重复调用结果相同。"""
    ctx = await _setup(client, session)
    h, sku = ctx["buyer_headers"], ctx["sku_ids"][0]

    await _add(client, h, sku, 5)
    resp = await client.put(f"/api/cart/items/{sku}", json={"num": 9}, headers=h)
    assert resp.status_code == 200, resp.text
    assert _find(await _cart(client, h), sku)["num"] == 9

    # 再来一次，仍然是 9（如果是累加会变成 18）
    await client.put(f"/api/cart/items/{sku}", json={"num": 9}, headers=h)
    assert _find(await _cart(client, h), sku)["num"] == 9


async def test_update_num_missing_item(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    resp = await client.put(
        f"/api/cart/items/{ctx['sku_ids'][0]}", json={"num": 3}, headers=ctx["buyer_headers"]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "CART_ITEM_NOT_FOUND"


# ============================================================
# ③ 越权
# ============================================================
async def test_cannot_touch_another_users_cart(client: AsyncClient, session) -> None:
    """别的用户既改不动也删不掉我的购物车。"""
    ctx = await _setup(client, session)
    owner, sku = ctx["buyer_headers"], ctx["sku_ids"][0]
    await _add(client, owner, sku, 2)

    other = await register(client, phone="13900139009")
    other_h = auth_header(other["accessToken"])

    # 改：按 (user_id, sku_id) 过滤，别人的行改不到 → 404
    resp = await client.put(f"/api/cart/items/{sku}", json={"num": 99}, headers=other_h)
    assert resp.status_code == 404

    # 删：删除是幂等的，不会报错，但也不能真的删掉
    await client.request(
        "DELETE", "/api/cart/items", json={"skuIds": [sku]}, headers=other_h
    )
    assert _find(await _cart(client, owner), sku)["num"] == 2, "别人的删除不能影响我的购物车"


# ============================================================
# ④ 状态判定
# ============================================================
async def test_status_no_stock(client: AsyncClient, session) -> None:
    """库存为 0 → 无货，且**不计入合计**。"""
    ctx = await _setup(client, session)
    h, sku = ctx["buyer_headers"], ctx["sku_ids"][0]
    await _add(client, h, sku, 2)

    assert (await _cart(client, h))["totalAmount"] > 0

    # 把库存清零
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE inventory.sku_stock SET available = 0, locked = total "
                "WHERE sku_id = :sku"
            ),
            {"sku": int(sku)},
        )

    cart = await _cart(client, h)
    item = _find(cart, sku)
    assert item["statusText"] == "无货"
    assert item["available"] == 0
    assert cart["totalAmount"] == 0, "无货的商品不能算进合计"


async def test_status_off_shelf(client: AsyncClient, session) -> None:
    """商品下架 → 已下架，出现在 invalidItems 里。"""
    ctx = await _setup(client, session)
    h, sku = ctx["buyer_headers"], ctx["sku_ids"][0]
    await _add(client, h, sku, 1)

    await _off_shelf(client, ctx)

    cart = await _cart(client, h)
    assert any(i["skuId"] == sku for i in cart["invalidItems"]), "下架商品应进 invalidItems"
    assert cart["totalAmount"] == 0


async def test_status_invalid_when_sku_deleted(client: AsyncClient, session) -> None:
    """SKU 被删除 → 失效。"""
    ctx = await _setup(client, session)
    h, sku = ctx["buyer_headers"], ctx["sku_ids"][0]
    await _add(client, h, sku, 1)

    await _delete_sku(sku)

    cart = await _cart(client, h)
    item = next(i for i in cart["invalidItems"] if i["skuId"] == sku)
    assert item["statusText"] == "失效"


async def test_status_valid(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    h, sku = ctx["buyer_headers"], ctx["sku_ids"][0]
    await _add(client, h, sku, 1)
    item = _find(await _cart(client, h), sku)
    assert item["statusText"] == "有效"
    assert item["available"] == 100


# ============================================================
# ⑤ 降价提醒
# ============================================================
async def test_price_down_hint(client: AsyncClient, session) -> None:
    """加购后降价 → priceDown 是差额；涨价 → 不提示。"""
    ctx = await _setup(client, session)
    h, sku = ctx["buyer_headers"], ctx["sku_ids"][0]
    await _add(client, h, sku, 1)

    snapshot = _find(await _cart(client, h), sku)["priceSnapshot"]

    # 降价 10000 分
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE product.sku SET price = :p WHERE id = :sku"),
            {"p": snapshot - 10000, "sku": int(sku)},
        )
    item = _find(await _cart(client, h), sku)
    assert item["priceDown"] == 10000
    assert item["price"] == snapshot - 10000, "展示的是当前价"
    assert item["priceSnapshot"] == snapshot, "快照不变"

    # 涨价 5000 分 → 不再提示降价
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE product.sku SET price = :p WHERE id = :sku"),
            {"p": snapshot + 5000, "sku": int(sku)},
        )
    assert _find(await _cart(client, h), sku)["priceDown"] == 0


# ============================================================
# ⑥ 按店铺分组
# ============================================================
async def test_group_by_shop(client: AsyncClient, session) -> None:
    """跨店加购 → 分成两组，每组小计独立且正确。"""
    ctx = await _setup(client, session)
    h, sku_ids = ctx["buyer_headers"], ctx["sku_ids"]

    await _add(client, h, sku_ids[0], 2)
    await _add(client, h, sku_ids[1], 1)

    cart = await _cart(client, h)
    assert len(cart["groups"]) == 1, "同一店铺的商品应在同一组"
    group = cart["groups"][0]
    assert group["shopId"] == ctx["shop_id"]
    assert group["shopName"] == "测试旗舰店"
    assert len(group["items"]) == 2

    expected = sum(i["itemAmount"] for i in group["items"] if i["selected"])
    assert group["selectedAmount"] == expected
    assert cart["totalAmount"] == expected


async def test_selected_excluded_from_total(client: AsyncClient, session) -> None:
    """未勾选的商品不计入合计。"""
    ctx = await _setup(client, session)
    h, sku_ids = ctx["buyer_headers"], ctx["sku_ids"]

    await _add(client, h, sku_ids[0], 1)
    await _add(client, h, sku_ids[1], 1)
    full = (await _cart(client, h))["totalAmount"]
    assert full > 0

    # 取消勾选第一个
    await client.put(
        "/api/cart/select", json={"skuIds": [sku_ids[0]], "selected": False}, headers=h
    )
    cart = await _cart(client, h)
    assert cart["totalAmount"] == _find(cart, sku_ids[1])["itemAmount"]
    assert cart["totalCount"] == 1


# ============================================================
# ⑦ 批量删除与清除失效
# ============================================================
async def test_batch_delete(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    h, sku_ids = ctx["buyer_headers"], ctx["sku_ids"]

    for sku in sku_ids:
        await _add(client, h, sku, 1)
    assert len(_all_items(await _cart(client, h))) == 3

    resp = await client.request(
        "DELETE", "/api/cart/items", json={"skuIds": [sku_ids[0], sku_ids[1]]}, headers=h
    )
    assert resp.status_code == 200, resp.text

    remaining = _all_items(await _cart(client, h))
    assert len(remaining) == 1
    assert remaining[0]["skuId"] == sku_ids[2]


async def test_clear_invalid(client: AsyncClient, session) -> None:
    """清除失效商品：只删失效/下架，有效的留着。"""
    ctx = await _setup(client, session)
    h, sku_ids = ctx["buyer_headers"], ctx["sku_ids"]

    for sku in sku_ids:
        await _add(client, h, sku, 1)

    # 删掉一个 SKU，让它变成"失效"
    await _delete_sku(sku_ids[0])

    assert len(_all_items(await _cart(client, h))) == 3
    await client.delete("/api/cart/invalid", headers=h)

    remaining = _all_items(await _cart(client, h))
    assert len(remaining) == 2, "失效的那件应被清掉"
    assert sku_ids[0] not in {i["skuId"] for i in remaining}


# ============================================================
# ⑧ 加购校验与角标
# ============================================================
async def test_add_off_shelf_rejected(client: AsyncClient, session) -> None:
    """已下架的商品不能加购。"""
    ctx = await _setup(client, session)
    await _off_shelf(client, ctx)

    resp = await client.post(
        "/api/cart/items", json={"skuId": ctx["sku_ids"][0], "num": 1}, headers=ctx["buyer_headers"]
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "CART_ITEM_INVALID"


async def test_cart_count(client: AsyncClient, session) -> None:
    ctx = await _setup(client, session)
    h, sku_ids = ctx["buyer_headers"], ctx["sku_ids"]

    resp = await client.get("/api/cart/count", headers=h)
    assert resp.json()["data"]["count"] == 0

    await _add(client, h, sku_ids[0], 1)
    await _add(client, h, sku_ids[1], 1)
    # 同一 SKU 再加不增加"种类数"
    await _add(client, h, sku_ids[0], 5)

    resp = await client.get("/api/cart/count", headers=h)
    assert resp.json()["data"]["count"] == 2


async def test_cart_requires_login(client: AsyncClient, session) -> None:
    resp = await client.get("/api/cart")
    assert resp.status_code == 401


async def test_cover_falls_back_to_spu_main_image(client: AsyncClient, session) -> None:
    """★ SKU 没单独设封面时，购物车该显示商品主图，而不是空白/灰块。

    商家的习惯是只传一张主图，规格图是选填的。回落做在
    ``product.service.batch_get_skus`` 里 —— 购物车、结算算价、下单快照共用它，
    让每个页面自己写 ``coverImage || mainImage`` 迟早漏一个。
    """
    ctx = await _setup(client, session)
    sku_id = ctx["sku_ids"][0]
    buyer = ctx["buyer_headers"]

    async def set_cover(value: str) -> None:
        async with get_session_factory()() as s, s.begin():
            await s.execute(
                text("UPDATE product.sku SET cover_image = :v WHERE id = :id"),
                {"v": value, "id": int(sku_id)},
            )

    await set_cover("")
    await _add(client, buyer, sku_id)
    assert _find(await _cart(client, buyer), sku_id)["coverImage"] == "/media/ip16.webp", (
        "SKU 没封面时该回落成商品主图"
    )

    # 反证：SKU 有自己的封面时不能被主图顶掉
    await set_cover("/media/own.webp")
    assert _find(await _cart(client, buyer), sku_id)["coverImage"] == "/media/own.webp"
