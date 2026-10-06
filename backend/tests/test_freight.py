"""freight 模块的集成测试。连真实 PostgreSQL。

重点验证两件事：
1. **模板配置的校验**真的拦得住非法组合（docs/06 §12 点名的那些）
2. **运费真的接进了算价** —— promotion 那边原本 `freight` 恒为 0
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.db import get_session_factory
from app.core.snowflake import next_id
from app.modules.freight import repository as repo
from app.modules.freight import service
from app.modules.freight.schemas import (
    ExcludeRegionIn,
    FreightTemplateCreateRequest,
    RegionRuleIn,
)
from app.modules.inventory import service as inv
from app.modules.inventory.models import Warehouse
from tests.conftest import auth_header, make_admin, open_shop, register
from tests.test_product import _make_category, _spu_payload

SHOP_ID_PLACEHOLDER = 0
BUYER_PHONE = "13900139021"  # 避开其他测试文件用过的号


@pytest.fixture(autouse=True)
async def clean_freight(app_runtime: None):
    """清掉运费相关的表。"""
    async with get_session_factory()() as s, s.begin():
        for t in (
            "freight_exclude_region",
            "sku_freight_bind",
            "freight_region_rule",
            "freight_template",
        ):
            await s.execute(text(f"DELETE FROM freight.{t}"))
    yield


async def _make_template(
    shop_id: int,
    *,
    name: str = "默认模板",
    first_unit: int = 1000,
    first_price: int = 1000,
    add_unit: int = 500,
    add_price: int = 300,
    free_shipping: bool = False,
    free_threshold: int = 0,
) -> int:
    async with get_session_factory()() as s, s.begin():
        tpl = await service.create_template(
            s,
            shop_id,
            FreightTemplateCreateRequest(
                name=name,
                charge_type=1,
                first_unit=first_unit,
                first_price=first_price,
                add_unit=add_unit,
                add_price=add_price,
                free_shipping=free_shipping,
                free_threshold=free_threshold,
            ),
        )
    return int(tpl.id)


async def _make_warehouse(shop_id: int) -> int:
    async with get_session_factory()() as s, s.begin():
        wh = Warehouse(id=next_id(), shop_id=shop_id, name="测试仓", is_default=True)
        s.add(wh)
    return int(wh.id)


# ============================================================
# ① 配置校验（docs/06 §12）
# ============================================================
async def test_reject_negative_first_price() -> None:
    """负的首重价在**第一道**就被挡住：Pydantic 的 ``ge=0``。

    service 里的 ``validate_template`` 是第二道（挡那些能通过 schema、
    但业务上不合理的值，比如"按重量却首重 0 克"）。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FreightTemplateCreateRequest(name="坏模板", first_price=-1)


async def test_reject_zero_first_unit_for_weight() -> None:
    """按重量计费时首重必须 > 0，否则"首重 0 克"让计费公式失去意义。

    这个值能通过 Pydantic（``ge=0``），所以必须由 service 层的语义校验兜住。
    """
    async with get_session_factory()() as s:
        with pytest.raises(Exception) as exc:
            await service.create_template(
                s,
                1,
                FreightTemplateCreateRequest(
                    name="坏模板",
                    charge_type=1,
                    first_unit=0,
                    first_price=1000,
                    add_unit=500,
                    add_price=300,
                ),
            )
        await s.rollback()
    assert "首重必须大于 0" in str(exc.value)


async def test_zero_first_unit_ok_for_piece() -> None:
    """按件数计费时首件数可以是 0（等于没有免费额度）。"""
    async with get_session_factory()() as s, s.begin():
        tpl = await service.create_template(
            s,
            1,
            FreightTemplateCreateRequest(
                name="按件", charge_type=2, first_unit=0, first_price=500, add_unit=1, add_price=200
            ),
        )
    assert tpl.id is not None


async def test_region_rules_require_nationwide_default() -> None:
    """★ 配了区域规则就必须保留"全国默认"，否则其他地区无规则可匹配。

    这是最容易漏、后果又最严重的一条配置错误 —— 用户会被"该地区不配送"拦住，
    而商家的本意显然不是这样。
    """
    shop_id = 990201
    tpl_id = await _make_template(shop_id)

    async with get_session_factory()() as s, s.begin():
        with pytest.raises(Exception) as exc:
            await service.replace_region_rules(
                s,
                shop_id,
                tpl_id,
                [
                    RegionRuleIn(
                        region_code="310000",
                        region_level=1,
                        first_unit=1000,
                        first_price=1200,
                        add_unit=500,
                        add_price=300,
                    )
                ],
            )
        await s.rollback()
    assert "全国默认" in str(exc.value)


async def test_region_rules_with_default_ok() -> None:
    shop_id = 990201
    tpl_id = await _make_template(shop_id)
    async with get_session_factory()() as s, s.begin():
        n = await service.replace_region_rules(
            s,
            shop_id,
            tpl_id,
            [
                RegionRuleIn(
                    region_code="0",
                    region_level=1,
                    first_unit=1000,
                    first_price=1000,
                    add_unit=500,
                    add_price=300,
                ),
                RegionRuleIn(
                    region_code="31",
                    region_level=1,
                    first_unit=1000,
                    first_price=1200,
                    add_unit=500,
                    add_price=300,
                ),
            ],
        )
    assert n == 2


async def test_exclude_conflicts_with_delivery_rule() -> None:
    """同一区域既配配送又配不发货 → 拦住（docs/06 §12 第 4 条）。"""
    shop_id = 990201
    tpl_id = await _make_template(shop_id)
    async with get_session_factory()() as s, s.begin():
        await service.replace_region_rules(
            s,
            shop_id,
            tpl_id,
            [
                RegionRuleIn(
                    region_code="0",
                    region_level=1,
                    first_unit=1000,
                    first_price=1000,
                    add_unit=500,
                    add_price=300,
                ),
                RegionRuleIn(
                    region_code="31",
                    region_level=1,
                    first_unit=1000,
                    first_price=1200,
                    add_unit=500,
                    add_price=300,
                ),
            ],
        )

    async with get_session_factory()() as s:
        with pytest.raises(Exception) as exc:
            await service.replace_exclude_regions(
                s, shop_id, tpl_id, [ExcludeRegionIn(region_code="31", reason="不送")]
            )
        await s.rollback()
    assert "二选一" in str(exc.value)


async def test_update_template_reports_affected_skus() -> None:
    """改模板要告诉商家影响面。"""
    shop_id = 990201
    tpl_id = await _make_template(shop_id)
    wh_id = await _make_warehouse(shop_id)

    async with get_session_factory()() as s, s.begin():
        # 直接用 repo 建绑定行：这条用例只关心"计数反映绑定数"。
        # 走 service.bind_sku 的话它现在会校验 SKU 归属（防跨店绑定），
        # 那就得先造一件真商品，跟这条用例要验的东西无关。
        await repo.upsert_bind(
            s,
            sku_id=12345,
            template_id=tpl_id,
            warehouse_id=wh_id,
            priority=0,
            bind_id=next_id(),
        )

    async with get_session_factory()() as s, s.begin():
        from app.modules.freight.schemas import FreightTemplateUpdateRequest

        _tpl, affected = await service.update_template(
            s,
            shop_id,
            tpl_id,
            FreightTemplateUpdateRequest(
                name="改过的", first_unit=1000, first_price=1200, add_unit=500, add_price=300
            ),
        )
    assert affected == 1


# ============================================================
# ② 算价里的运费（集成）
# ============================================================
async def _prepare_product(client: AsyncClient, session) -> tuple[dict, str, str]:
    """发一个商品并上库存。返回 (买家 headers, sku_id, spu_id)。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")

    merchant = await register(client, phone="13800138031")
    shop_id = await open_shop(client, merchant["accessToken"])
    mh = auth_header(merchant["accessToken"])
    # 不用手动建模板：开店自带一条默认模板，而没绑定的 SKU 就是回落到它

    resp = await client.post(
        "/api/merchant/spus", json=_spu_payload(category), headers=mh
    )
    assert resp.status_code == 200, resp.text
    spu_id = resp.json()["data"]["id"]
    sku_id = resp.json()["data"]["skus"][0]["id"]

    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=mh)
    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": True},
        headers=auth_header(admin["accessToken"]),
    )

    wh_id = await _make_warehouse(int(shop_id))
    async with get_session_factory()() as s, s.begin():
        await inv.init(s, sku_id=int(sku_id), warehouse_id=wh_id, qty=100, biz_key="f-init")

    buyer = await register(client, phone=BUYER_PHONE)
    return (
        {"headers": auth_header(buyer["accessToken"]), "shop_id": int(shop_id), "wh_id": wh_id},
        sku_id,
        spu_id,
    )


async def _make_address(client: AsyncClient, headers: dict, region_code: str = "310115") -> str:
    resp = await client.post(
        "/api/me/addresses",
        json={
            "receiverName": "张三",
            "phone": "13800138000",
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "某某路 1 号",
            "regionCode": region_code,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def test_calc_includes_real_freight(client: AsyncClient, session) -> None:
    """★ 结算页的运费不再是 0。

    这是本模块的核心价值：promotion 那边原本 `freight` 恒为 0 并标注"未实现"，
    绑上运费模板之后要能算出真实运费。
    """
    ctx, sku_id, _spu = await _prepare_product(client, session)
    headers = ctx["headers"]

    tpl_id = await _make_template(
        ctx["shop_id"], first_unit=1000, first_price=1000, add_unit=500, add_price=300
    )
    async with get_session_factory()() as s, s.begin():
        await service.bind_sku(
            s, ctx["shop_id"], sku_id=int(sku_id), template_id=tpl_id, warehouse_id=ctx["wh_id"]
        )

    address_id = await _make_address(client, headers)

    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku_id, "num": 1}], "addressId": address_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    # 商品重 199g（seed 里写的），1 件 → 200g < 1000g 首重 → 收首重价 10 元
    assert data["freight"] == 1000, f"运费应当是被算出来的，实际 {data['freight']}"
    assert data["payableAmount"] == data["totalAmount"] - data["itemDiscount"] - data["shopDiscount"] - data["platformDiscount"] + data["freight"]
    # "运费未实现"的标注不该再出现
    assert not any("运费模块尚未实现" in n for n in data["notices"])


async def test_calc_without_address_says_so(client: AsyncClient, session) -> None:
    """没传地址时运费按 0 计，但**要说明原因**，不能让人以为算漏了。"""
    ctx, sku_id, _spu = await _prepare_product(client, session)
    headers = ctx["headers"]

    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku_id, "num": 1}]},
        headers=headers,
    )
    data = resp.json()["data"]
    assert data["freight"] == 0
    assert any("收货地址" in n for n in data["notices"])


async def test_unbound_sku_falls_back_to_default_template(client: AsyncClient, session) -> None:
    """没绑模板的规格回落到**店铺默认模板**（docs/06 §404 写的那半句）。

    ★ 绑定是逐条 SKU 的，新发布的商品天然在模板之外；以前这种情况直接拒单，
      而且报错发生在**买家结算**那一刻 —— 商家那边毫无感知。
    ★ 这里**显式指定**一条默认模板（首重 ¥12.34），而不是依赖"开店送的那条"：
      断言才是在验"回落"，而不是在验默认模板自己的参数。
    """
    ctx, sku_id, _spu = await _prepare_product(client, session)
    headers = ctx["headers"]

    tpl = await _make_template(ctx["shop_id"], name="兜底模板", first_price=1234)
    async with get_session_factory()() as s, s.begin():
        await service.set_default_template(s, ctx["shop_id"], tpl)

    address_id = await _make_address(client, headers)
    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku_id, "num": 1}], "addressId": address_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    # 商品 199g < 首重 1000g → 收兜底模板的首重价
    assert resp.json()["data"]["freight"] == 1234


async def test_unbound_sku_rejected_without_default(client: AsyncClient, session) -> None:
    """连默认模板都没有，才是真的算不出来，必须拒（docs/06 §10）。"""
    ctx, sku_id, _spu = await _prepare_product(client, session)
    headers = ctx["headers"]
    # 摘掉默认标记，模拟"这个店一条模板都没配"
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE freight.freight_template SET is_default = false WHERE shop_id = :i"),
            {"i": ctx["shop_id"]},
        )
    address_id = await _make_address(client, headers)

    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku_id, "num": 1}], "addressId": address_id},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "SKU_NOT_SUPPORTED"
    # 提示要说清是"既没绑定、也没默认"，而不是含糊的"没绑模板"
    assert "默认模板" in resp.json()["message"]


async def test_disabled_default_template_rejected(client: AsyncClient, session) -> None:
    """默认模板被停用 = 没有兜底。提示要指明是"停用了"，别让人以为压根没配。"""
    ctx, sku_id, _spu = await _prepare_product(client, session)
    headers = ctx["headers"]
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE freight.freight_template SET status = 2 WHERE shop_id = :i"),
            {"i": ctx["shop_id"]},
        )
    address_id = await _make_address(client, headers)

    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku_id, "num": 1}], "addressId": address_id},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "SKU_NOT_SUPPORTED"
    assert "停用" in resp.json()["message"]


async def test_excluded_region_rejected(client: AsyncClient, session) -> None:
    """不发货区域在结算页就被拦住，不进入下单流程。"""
    ctx, sku_id, _spu = await _prepare_product(client, session)
    headers = ctx["headers"]

    tpl_id = await _make_template(ctx["shop_id"])
    async with get_session_factory()() as s, s.begin():
        await service.bind_sku(
            s, ctx["shop_id"], sku_id=int(sku_id), template_id=tpl_id, warehouse_id=ctx["wh_id"]
        )
        await service.replace_exclude_regions(
            s, ctx["shop_id"], tpl_id, [ExcludeRegionIn(region_code="31", reason="暂不配送")]
        )

    address_id = await _make_address(client, headers, region_code="310115")

    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku_id, "num": 1}], "addressId": address_id},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "NOT_DELIVERABLE"


async def test_freight_estimate_endpoint(client: AsyncClient, session) -> None:
    """单独估运费的接口（购物车页用）。"""
    ctx, sku_id, _spu = await _prepare_product(client, session)
    headers = ctx["headers"]
    tpl_id = await _make_template(ctx["shop_id"])
    async with get_session_factory()() as s, s.begin():
        await service.bind_sku(
            s, ctx["shop_id"], sku_id=int(sku_id), template_id=tpl_id, warehouse_id=ctx["wh_id"]
        )
    address_id = await _make_address(client, headers)

    resp = await client.post(
        "/api/checkout/freight",
        json={"items": [{"skuId": sku_id, "num": 3}], "addressId": address_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # 3 件 × 199g = 597g < 1000g → 首重价
    assert data["total"] == 1000
    assert len(data["packages"]) == 1


async def test_freight_coupon_applies_to_freight(client: AsyncClient, session) -> None:
    """运费券作用于运费本身，门槛也针对运费（docs/06 §7）。"""
    from app.modules.promotion.models import CODE_UNUSED, COUPON_TYPE_FREIGHT, CouponCode, CouponTemplate

    ctx, sku_id, _spu = await _prepare_product(client, session)
    headers = ctx["headers"]

    tpl_id = await _make_template(ctx["shop_id"])
    async with get_session_factory()() as s, s.begin():
        await service.bind_sku(
            s, ctx["shop_id"], sku_id=int(sku_id), template_id=tpl_id, warehouse_id=ctx["wh_id"]
        )

    # 直接造一张运费券（面额 5 元、无门槛）
    now = datetime.now(UTC)
    async with get_session_factory()() as s, s.begin():
        c_tpl = CouponTemplate(
            id=next_id(),
            shop_id=0,
            name="运费券减5元",
            type=COUPON_TYPE_FREIGHT,
            get_type=1,
            discount_value=500,
            threshold=0,
            total_count=10,
            per_user_limit=1,
            valid_type=1,
            valid_start=now - timedelta(hours=1),
            valid_end=now + timedelta(days=7),
            scope_type=1,
            status=2,
        )
        s.add(c_tpl)
        await s.flush()
        buyer_id = int(
            await s.scalar(
                text("SELECT id FROM account.user ORDER BY id DESC LIMIT 1")
            )
        )
        code = CouponCode(
            id=next_id(),
            coupon_template_id=c_tpl.id,
            user_id=buyer_id,
            code="FREIGHTTEST01",
            status=CODE_UNUSED,
            valid_start=c_tpl.valid_start,
            valid_end=c_tpl.valid_end,
            source=1,
        )
        s.add(code)

    address_id = await _make_address(client, headers)
    resp = await client.post(
        "/api/checkout/calc",
        json={
            "items": [{"skuId": sku_id, "num": 1}],
            "couponCodeIds": [str(code.id)],
            "addressId": address_id,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["freight"] == 1000 - 500, "运费被券抵扣 5 元"
    assert any("运费券" in n for n in data["notices"])
    # ★ 运费券不能影响商品金额的优惠
    assert data["itemDiscount"] == 0
    assert data["platformDiscount"] == 0


# ============================================================
# ⑨ 模板绑定明细（只读）
#
# 补的坑：模板出参只有一个 ``boundSkuCount`` 数字，商家看不到**绑了哪些**，
# 绑错了也撤不掉（解绑接口仍未做，见计划里"明确不做"）。
# ============================================================
async def _merchant_with_sku(
    client: AsyncClient, session, phone: str = "13800138041"
) -> tuple[dict[str, str], int, int, int]:
    """建商家 + 已上架商品 + 仓库。返回 (商家 headers, shop_id, sku_id, wh_id)。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")

    merchant = await register(client, phone=phone)
    shop_id = int(await open_shop(client, merchant["accessToken"]))
    mh = auth_header(merchant["accessToken"])

    resp = await client.post("/api/merchant/spus", json=_spu_payload(category), headers=mh)
    assert resp.status_code == 200, resp.text
    sku_id = int(resp.json()["data"]["skus"][0]["id"])
    wh_id = await _make_warehouse(shop_id)
    return mh, shop_id, sku_id, wh_id


async def test_list_template_binds_carries_display_names(
    client: AsyncClient, session
) -> None:
    """绑完之后要能看到明细，而且显示的是商品标题不是裸 ID。"""
    mh, shop_id, sku_id, wh_id = await _merchant_with_sku(client, session)
    tpl_id = await _make_template(shop_id)

    resp = await client.post(
        "/api/merchant/freight/bind",
        json={
            "skuId": str(sku_id),
            "templateId": str(tpl_id),
            "warehouseId": str(wh_id),
            "priority": 3,
        },
        headers=mh,
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/api/merchant/freight/templates/{tpl_id}/binds", headers=mh
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert len(rows) == 1

    row = rows[0]
    assert row["skuId"] == str(sku_id)
    assert row["warehouseId"] == str(wh_id)
    assert row["priority"] == 3
    assert row["enabled"] is True
    # 显示名在后端拼好（freight 读 product 是既有依赖，estimate 那条路径就在用）
    assert row["spuTitle"]
    assert row["skuCode"]
    assert row["warehouseName"] == "测试仓"


async def test_list_template_binds_empty_when_nothing_bound(
    client: AsyncClient, session
) -> None:
    mh, shop_id, _sku, _wh = await _merchant_with_sku(client, session)
    tpl_id = await _make_template(shop_id)
    resp = await client.get(
        f"/api/merchant/freight/templates/{tpl_id}/binds", headers=mh
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_list_binds_of_other_shop_is_404(client: AsyncClient, session) -> None:
    """★ 不能拿别人的 template_id 问出"他绑了哪些 SKU"。"""
    mh_a, shop_a, sku_a, wh_a = await _merchant_with_sku(
        client, session, phone="13800138042"
    )
    tpl_a = await _make_template(shop_a)
    bound = await client.post(
        "/api/merchant/freight/bind",
        json={"skuId": str(sku_a), "templateId": str(tpl_a), "warehouseId": str(wh_a)},
        headers=mh_a,
    )
    assert bound.status_code == 200, bound.text

    merchant_b = await register(client, phone="13800138043")
    await open_shop(client, merchant_b["accessToken"], name="另一家店")

    resp = await client.get(
        f"/api/merchant/freight/templates/{tpl_a}/binds",
        headers=auth_header(merchant_b["accessToken"]),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# ============================================================
# ⑩ 已软删商品在运费绑定上的表现
#
# 软删只改 ``product.spu.deleted``，**不会**去删 ``freight.sku_freight_bind``
# 里的行 —— 模块边界规定 freight 的表只有 freight 自己能写，product 不能反向
# 依赖它。所以"删了就该消失"这件事只能在 freight 的读侧做。
# ============================================================
async def _only_spu_id(client: AsyncClient, mh: dict[str, str]) -> str:
    """这个商家唯一那件商品的 id（``_merchant_with_sku`` 只造一件）。"""
    resp = await client.get("/api/merchant/spus", params={"limit": 10}, headers=mh)
    items = resp.json()["data"]["items"]
    assert len(items) == 1, f"预期正好一件商品，实际 {len(items)}"
    return str(items[0]["id"])


async def test_deleted_product_leaves_bind_list_and_count(
    client: AsyncClient, session
) -> None:
    """★ 商品软删后，它的运费绑定不该再出现在明细里，"已绑定 N 个 SKU"要一起减。

    残留行还在 ``freight.sku_freight_bind`` 里，商家却看不到 —— 这正是
    "删了却像没删干净"的来源。
    """
    mh, shop_id, sku_id, wh_id = await _merchant_with_sku(
        client, session, phone="13800138044"
    )
    tpl_id = await _make_template(shop_id)
    bound = await client.post(
        "/api/merchant/freight/bind",
        json={"skuId": str(sku_id), "templateId": str(tpl_id), "warehouseId": str(wh_id)},
        headers=mh,
    )
    assert bound.status_code == 200, bound.text

    # 删之前先确认这条链路本来是通的，否则下面的空断言会空过
    rows = (
        await client.get(f"/api/merchant/freight/templates/{tpl_id}/binds", headers=mh)
    ).json()["data"]
    assert len(rows) == 1
    # ★ 按 id 找自己那条：开店自带一条默认模板，列表里不止一条，不能用 [0]
    tpls = (await client.get("/api/merchant/freight/templates", headers=mh)).json()["data"]
    assert next(t for t in tpls if t["id"] == str(tpl_id))["boundSkuCount"] == 1

    spu_id = await _only_spu_id(client, mh)
    assert (await client.delete(f"/api/merchant/spus/{spu_id}", headers=mh)).status_code == 200

    rows = (
        await client.get(f"/api/merchant/freight/templates/{tpl_id}/binds", headers=mh)
    ).json()["data"]
    assert rows == []
    tpls = (await client.get("/api/merchant/freight/templates", headers=mh)).json()["data"]
    count = next(t for t in tpls if t["id"] == str(tpl_id))["boundSkuCount"]
    assert count == 0, "计数必须和明细一致，否则商家看到空明细配个 1"
    # 行本身留在库里：将来若做"恢复已删商品"，绑定关系还在
    assert (
        await session.scalar(
            text("SELECT count(*) FROM freight.sku_freight_bind WHERE template_id = :t"),
            {"t": tpl_id},
        )
        == 1
    )


async def test_bind_rejects_deleted_sku(client: AsyncClient, session) -> None:
    """★ 给已删商品建绑定没有意义 —— 那件商品连下单都下不了。"""
    mh, shop_id, sku_id, wh_id = await _merchant_with_sku(
        client, session, phone="13800138045"
    )
    tpl_id = await _make_template(shop_id)
    spu_id = await _only_spu_id(client, mh)
    assert (await client.delete(f"/api/merchant/spus/{spu_id}", headers=mh)).status_code == 200

    resp = await client.post(
        "/api/merchant/freight/bind",
        json={"skuId": str(sku_id), "templateId": str(tpl_id), "warehouseId": str(wh_id)},
        headers=mh,
    )
    assert resp.status_code == 404


async def test_bind_rejects_another_shops_sku(client: AsyncClient, session) -> None:
    """★ 只校验模板归属不够：拿别人的 sku_id 就能把竞争对手的商品绑到自己的
    模板上 —— 建个 0 元模板、优先级拉满，对方的商品对买家就包邮了。"""
    mh_a, shop_a, sku_a, wh_a = await _merchant_with_sku(
        client, session, phone="13800138046"
    )
    # 先证明这组参数在"自己的 SKU + 自己的模板"下是能绑上的，
    # 否则下面的 404 可能是别的原因造成的，白过
    tpl_a = await _make_template(shop_a)
    ok = await client.post(
        "/api/merchant/freight/bind",
        json={"skuId": str(sku_a), "templateId": str(tpl_a), "warehouseId": str(wh_a)},
        headers=mh_a,
    )
    assert ok.status_code == 200, ok.text

    merchant_b = await register(client, phone="13800138047")
    shop_b = int(await open_shop(client, merchant_b["accessToken"], name="另一家店"))
    mh_b = auth_header(merchant_b["accessToken"])
    tpl_b = await _make_template(shop_b)

    resp = await client.post(
        "/api/merchant/freight/bind",
        json={"skuId": str(sku_a), "templateId": str(tpl_b), "warehouseId": str(wh_a)},
        headers=mh_b,
    )
    assert resp.status_code == 404
    assert (
        await session.scalar(
            text("SELECT count(*) FROM freight.sku_freight_bind WHERE template_id = :t"),
            {"t": tpl_b},
        )
        == 0
    )


# ============================================================
# 店铺默认模板（未绑定的 SKU 回落到它）
# ============================================================
async def test_extra_template_does_not_steal_default(client: AsyncClient, session) -> None:
    """默认位只有一条：开店自带的那条是默认，之后建的不会抢走它。

    （"开店自带一条默认模板"这件事本身在 ``test_account.py`` 里测 ——
      那是 account 的行为。）
    """
    merchant = await register(client, phone="13800138061")
    shop_id = int(await open_shop(client, merchant["accessToken"]))
    mh = auth_header(merchant["accessToken"])
    auto = (await client.get("/api/merchant/freight/templates", headers=mh)).json()["data"][0]

    second = await _make_template(shop_id, name="第二条")

    rows = (await client.get("/api/merchant/freight/templates", headers=mh)).json()["data"]
    assert [t["id"] for t in rows if t["isDefault"]] == [auto["id"]]
    assert str(second) in [t["id"] for t in rows]


async def test_set_default_moves_the_flag(client: AsyncClient, session) -> None:
    """设为默认会顶掉旧的 —— 一个店只能有一条默认（部分唯一索引兜着）。"""
    merchant = await register(client, phone="13800138062")
    shop_id = int(await open_shop(client, merchant["accessToken"]))
    mh = auth_header(merchant["accessToken"])
    auto = (await client.get("/api/merchant/freight/templates", headers=mh)).json()["data"][0]
    second = await _make_template(shop_id, name="第二条")

    resp = await client.put(f"/api/merchant/freight/templates/{second}/default", headers=mh)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["isDefault"] is True

    rows = (await client.get("/api/merchant/freight/templates", headers=mh)).json()["data"]
    assert [t["id"] for t in rows if t["isDefault"]] == [str(second)]
    assert auto["id"] not in [t["id"] for t in rows if t["isDefault"]]


async def test_disabled_template_cannot_become_default(client: AsyncClient, session) -> None:
    """停用的模板不能设成默认。

    默认是"没绑定时的兜底"，一条停用的兜底**等于没有兜底**，却会让商家以为
    已经配好了 —— 比没设更坏，因为它看起来是配好的。
    """
    merchant = await register(client, phone="13800138063")
    shop_id = int(await open_shop(client, merchant["accessToken"]))
    mh = auth_header(merchant["accessToken"])
    second = await _make_template(shop_id, name="第二条")

    off = await client.put(
        f"/api/merchant/freight/templates/{second}",
        json={"name": "第二条", "firstPrice": 1000, "addPrice": 300, "status": 2},
        headers=mh,
    )
    assert off.status_code == 200, off.text

    resp = await client.put(f"/api/merchant/freight/templates/{second}/default", headers=mh)
    assert resp.status_code == 400
    assert "停用" in resp.json()["message"]
