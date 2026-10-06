"""promotion 模块的集成测试。连真实 PostgreSQL + Redis。

重点是**需求②「券超发」**：Redis Lua 拦第一道、DB 条件更新拦第二道，
两道都要单独验证有效 —— 只验第一道的话，Redis 挂掉那天才会发现问题。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.core.db import get_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.redis import get_redis
from app.core.snowflake import next_id
from app.modules.promotion import repository as repo
from app.modules.promotion import service
from app.modules.promotion.models import (
    ACTIVITY_STATUS_ENDED,
    ACTIVITY_STATUS_NOT_STARTED,
    ACTIVITY_STATUS_ONGOING,
    ACTIVITY_STATUS_VOID,
    CALC_DIRECT,
    CALC_FIXED,
    CODE_UNUSED,
    COUPON_TPL_ENDED,
    COUPON_TPL_NOT_STARTED,
    COUPON_TPL_ONGOING,
    COUPON_TPL_VOID,
    DISCOUNT_ITEM_PROMO,
    DISCOUNT_PLATFORM_PROMO,
    DISCOUNT_SHOP_PROMO,
    ISSUE_MAX_PER_USER_TPL,
    LEVEL_ITEM,
    LEVEL_PLATFORM,
    LEVEL_SHOP,
    SCOPE_CATEGORY,
    SCOPE_SKU,
    VALID_DAYS_AFTER,
    CouponCode,
    CouponTemplate,
    PromoActivity,
)
from app.modules.promotion.router import ACTIVE_ACTIVITY_LIMIT
from app.modules.promotion.tasks import refresh_promo_status
from tests.conftest import auth_header, make_admin, open_shop, register

SHOP_ID = 990101
BUYER_PHONE = "13900139011"  # 避开 make_admin 的 13900139001
ADMIN_PHONE = "13900139091"  # 再避开别处的管理员号


# ============================================================
# 夹具
# ============================================================
@pytest.fixture(autouse=True)
async def clean_promotion(app_runtime: None):
    """清掉券相关的 DB 与 Redis 状态。"""
    async with get_session_factory()() as s, s.begin():
        for t in (
            "coupon_flow",
            "coupon_receive_log",
            "coupon_user_quota",
            "coupon_code",
            "coupon_template",
            "promo_activity",
            "banner",
        ):
            await s.execute(text(f"DELETE FROM promotion.{t}"))
    redis = get_redis()
    keys = [k async for k in redis.scan_iter(match="coupon:*")]
    if keys:
        await redis.delete(*keys)
    yield


async def _make_template(
    *,
    total: int = 10,
    per_user: int = 1,
    threshold: int = 0,
    value: int = 2000,
    ctype: int = 1,
    days: int = 7,
) -> int:
    """直接建一个进行中的券模板，返回 id。"""
    now = datetime.now(UTC)
    async with get_session_factory()() as s, s.begin():
        tpl = CouponTemplate(
            id=next_id(),
            shop_id=0,
            name=f"测试券{total}-{per_user}",
            type=ctype,
            get_type=1,
            discount_value=value,
            threshold=threshold,
            total_count=total,
            per_user_limit=per_user,
            valid_type=1,
            valid_start=now - timedelta(hours=1),
            valid_end=now + timedelta(days=days),
            scope_type=1,
            status=2,
        )
        s.add(tpl)
    return tpl.id


async def _receive(tpl_id: int, user_id: int, idem: str) -> str:
    """直接调 service 领券，返回结果码（成功为 'OK'）。"""
    async with get_session_factory()() as s:
        try:
            await service.receive(s, user_id=user_id, tpl_id=tpl_id, idem_key=idem)
            await s.commit()
            return "OK"
        except BizError as e:
            await s.rollback()
            return str(e.code.value)


async def _issued(tpl_id: int) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT issued_count FROM promotion.coupon_template WHERE id = :t"),
                {"t": tpl_id},
            )
            or 0
        )


# ============================================================
# ① 并发不超发 —— 需求②的核心
# ============================================================
async def test_concurrent_receive_never_oversells() -> None:
    """限量 10 张，50 个不同用户并发领取 —— 必须恰好发出 10 张。

    这是整个券体系存在的理由。Redis Lua 保证"检查 + 扣减 + 限领"原子，
    DB 的条件更新 + CHECK 约束是第二道保险。
    """
    tpl_id = await _make_template(total=10, per_user=1)

    results = await asyncio.gather(
        *(_receive(tpl_id, user_id=1000 + i, idem=f"IDEM-{i}") for i in range(50))
    )

    ok = [r for r in results if r == "OK"]
    assert len(ok) == 10, f"应恰好发出 10 张，实际 {len(ok)}"

    assert await _issued(tpl_id) == 10, "DB 账本必须与发出量一致"

    async with get_session_factory()() as s:
        codes = int(
            await s.scalar(
                text("SELECT count(*) FROM promotion.coupon_code WHERE coupon_template_id = :t"),
                {"t": tpl_id},
            )
            or 0
        )
    assert codes == 10, "券实例数必须等于发出量"


async def test_redis_stock_reaches_zero_exactly() -> None:
    """发完之后 Redis 剩余额度必须**恰好**是 0，不能是负数。"""
    tpl_id = await _make_template(total=5, per_user=1)
    await asyncio.gather(*(_receive(tpl_id, 2000 + i, f"K-{i}") for i in range(20)))

    redis = get_redis()
    from app.core import redis_keys as rk

    stock = await redis.get(rk.coupon_tpl_stock(tpl_id))
    assert int(stock) == 0, f"Redis 剩余额度应为 0，实际 {stock}"


# ============================================================
# ② 限领
# ============================================================
async def test_per_user_limit_under_concurrency() -> None:
    """每人限 1 张，同一用户并发领 5 次 —— 只能拿到 1 张。"""
    tpl_id = await _make_template(total=100, per_user=1)

    results = await asyncio.gather(
        *(_receive(tpl_id, user_id=3001, idem=f"U-{i}") for i in range(5))
    )
    assert results.count("OK") == 1, f"限领 1 张，实际成功 {results.count('OK')} 次"
    assert await _issued(tpl_id) == 1

    async with get_session_factory()() as s:
        quota = await s.scalar(
            text(
                "SELECT received FROM promotion.coupon_user_quota "
                "WHERE coupon_template_id = :t AND user_id = 3001"
            ),
            {"t": tpl_id},
        )
    assert quota == 1, "限领计数也必须只加一次"


async def test_per_user_limit_n() -> None:
    """限领 N 张时能领到 N 张，第 N+1 次被拒。

    这是"为什么限领不能靠唯一索引"的证明 —— 唯一索引在限领 N 张时会误拦。
    """
    tpl_id = await _make_template(total=100, per_user=3)
    codes = [await _receive(tpl_id, 4001, f"N-{i}") for i in range(4)]
    assert codes[:3] == ["OK", "OK", "OK"]
    assert codes[3] == ErrorCode.COUPON_LIMIT_EXCEEDED.value
    assert await _issued(tpl_id) == 3


# ============================================================
# ③ 幂等
# ============================================================
async def test_same_idempotency_key_issues_once() -> None:
    """同一 Idempotency-Key 重放只发一张。

    模拟"客户端超时重试"：没有幂等键的话，重试会多发一张。
    """
    tpl_id = await _make_template(total=100, per_user=10)

    first = await _receive(tpl_id, 5001, "SAME-KEY")
    second = await _receive(tpl_id, 5001, "SAME-KEY")

    assert first == "OK"
    assert second == "OK", "重放应当返回首次结果而不是报错"
    assert await _issued(tpl_id) == 1, "重放不能多发"


async def test_different_keys_issue_separately() -> None:
    """换了幂等键就是新的一次领取，应当真的多发一张（限领允许时）。"""
    tpl_id = await _make_template(total=100, per_user=5)
    assert await _receive(tpl_id, 5002, "K-1") == "OK"
    assert await _receive(tpl_id, 5002, "K-2") == "OK"
    assert await _issued(tpl_id) == 2


# ============================================================
# ④ DB 兜底（第二道防线）
# ============================================================
async def test_db_blocks_oversell_when_redis_drifted() -> None:
    """Redis 说还有货、DB 已经发满 —— DB 的条件更新必须拦住。

    模拟 Redis 漂移（或者有人绕过应用直接改库）。这正是"不能只靠 Redis"的原因：
    Redis 重启丢写入、或某次补偿没跑成，都会造成这个局面。
    """
    tpl_id = await _make_template(total=3, per_user=100)

    # 绕过应用：直接标记 DB 已发满，但**不动 Redis**
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text("UPDATE promotion.coupon_template SET issued_count = total_count WHERE id = :t"),
            {"t": tpl_id},
        )

    # 预热 Redis 并让它以为还有额度（因为 issued_count 被改时 Redis 没同步）
    from app.core import redis_keys as rk

    redis = get_redis()
    async with get_session_factory()() as s:
        tpl = await repo.get_template(s, tpl_id)
        await service.warm_template(redis, tpl)
    # 手工把 Redis 的额度改大，制造"Redis 有货但 DB 已满"
    await redis.set(rk.coupon_tpl_stock(tpl_id), 99)

    result = await _receive(tpl_id, 6001, "FORCE")
    assert result == ErrorCode.COUPON_SOLD_OUT.value, "DB 必须拦住超发"

    assert await _issued(tpl_id) == 3, "issued_count 不能被推高"
    async with get_session_factory()() as s:
        codes = int(
            await s.scalar(
                text("SELECT count(*) FROM promotion.coupon_code WHERE coupon_template_id = :t"),
                {"t": tpl_id},
            )
            or 0
        )
    assert codes == 0, "被拦下的领取不能留下券实例"


async def test_check_constraint_guards_oversell() -> None:
    """表上的 CHECK 约束是最后一道：绕过所有应用逻辑也改不出超发。"""
    tpl_id = await _make_template(total=3, per_user=1)

    async with get_session_factory()() as s:
        with pytest.raises(Exception) as exc:
            # 直接写到超发
            await s.execute(
                text("UPDATE promotion.coupon_template SET issued_count = 4 WHERE id = :t"),
                {"t": tpl_id},
            )
        await s.rollback()
    assert "ck_coupon_template" in str(exc.value) or "check" in str(exc.value).lower()


# ============================================================
# ⑤ 生命周期
# ============================================================
async def _issue_one(tpl_id: int, user_id: int) -> int:
    async with get_session_factory()() as s:
        code = await service.receive(
            s, user_id=user_id, tpl_id=tpl_id, idem_key=f"L-{user_id}-{tpl_id}"
        )
        await s.commit()
        return int(code.id)


async def test_lifecycle_lock_use_refund() -> None:
    """锁 → 核销 → 退回，并且重复调用都幂等。"""
    tpl_id = await _make_template(total=10, per_user=1)
    code_id = await _issue_one(tpl_id, 7001)

    async with get_session_factory()() as s, s.begin():
        await service.lock(s, code_id=code_id, user_id=7001, order_main_no="ORD-A")
    assert await _status(code_id) == 2

    # 重复锁同一单：幂等
    async with get_session_factory()() as s, s.begin():
        await service.lock(s, code_id=code_id, user_id=7001, order_main_no="ORD-A")
    assert await _status(code_id) == 2

    async with get_session_factory()() as s, s.begin():
        await service.use(
            s, code_id=code_id, user_id=7001, order_main_no="ORD-A", use_amount=2000
        )
    assert await _status(code_id) == 3

    # 重复核销：幂等，used_count 不能加两次
    async with get_session_factory()() as s, s.begin():
        await service.use(
            s, code_id=code_id, user_id=7001, order_main_no="ORD-A", use_amount=2000
        )
    async with get_session_factory()() as s:
        used = await s.scalar(
            text("SELECT used_count FROM promotion.coupon_template WHERE id = :t"),
            {"t": tpl_id},
        )
    assert used == 1, "重复核销不能让 used_count 加两次"

    async with get_session_factory()() as s, s.begin():
        await service.refund(s, code_id=code_id, user_id=7001, refund_no="RF-A")
    assert await _status(code_id) == 1, "退回后回到未使用"

    async with get_session_factory()() as s:
        flows = int(
            await s.scalar(text("SELECT count(*) FROM promotion.coupon_flow")) or 0
        )
    assert flows == 3, "锁/核销/退回各一条流水"


async def test_cannot_lock_code_held_by_another_order() -> None:
    """已被 A 单锁定的券，B 单锁不走。"""
    tpl_id = await _make_template(total=10, per_user=1)
    code_id = await _issue_one(tpl_id, 7002)

    async with get_session_factory()() as s, s.begin():
        await service.lock(s, code_id=code_id, user_id=7002, order_main_no="ORD-A")

    async with get_session_factory()() as s:
        with pytest.raises(BizError) as exc:
            await service.lock(s, code_id=code_id, user_id=7002, order_main_no="ORD-B")
        await s.rollback()
    assert exc.value.code is ErrorCode.COUPON_LOCKED
    assert await _status(code_id) == 2, "失败的锁定不能改状态"


async def test_cannot_lock_expired_code() -> None:
    """过期的券锁不上 —— 条件里的 ``valid_end >= now()`` 会拦住。"""
    tpl_id = await _make_template(total=10, per_user=1, days=7)
    code_id = await _issue_one(tpl_id, 7003)

    # 把券的有效期改到过去
    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE promotion.coupon_code "
                "SET valid_start = now() - interval '2 days', valid_end = now() - interval '1 day' "
                "WHERE id = :i"
            ),
            {"i": code_id},
        )

    async with get_session_factory()() as s:
        with pytest.raises(BizError) as exc:
            await service.lock(s, code_id=code_id, user_id=7003, order_main_no="ORD-C")
        await s.rollback()
    assert exc.value.code is ErrorCode.COUPON_LOCKED


async def _status(code_id: int) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(
                text("SELECT status FROM promotion.coupon_code WHERE id = :i"), {"i": code_id}
            )
        )


# ============================================================
# ⑥ 客服补发不占活动额度
# ============================================================
async def test_admin_issue_does_not_consume_quota() -> None:
    """补发不增加 issued_count，也不占用户的限领名额（docs/04 §11）。"""
    tpl_id = await _make_template(total=3, per_user=1)

    async with get_session_factory()() as s, s.begin():
        codes = await service.issue_by_admin(
            s, tpl_id=tpl_id, user_id=8001, operator="admin:1", count=2
        )
    assert len(codes) == 2
    assert await _issued(tpl_id) == 0, "补发不占活动额度"

    # 用户仍然能正常领取（限领名额没被补发用掉）
    assert await _receive(tpl_id, 8001, "AFTER-ISSUE") == "OK"


# ============================================================
# ⑦ 算价接口（HTTP 层端到端）
# ============================================================
async def test_calc_price_endpoint(client: AsyncClient) -> None:
    """走一遍真实 HTTP：发商品 → 上库存 → 领券 → 算价，验证逐级优惠与分摊。"""
    from tests.test_product import _create_and_publish

    async with get_session_factory()() as session:
        spu_id, _merchant_token = await _create_and_publish(client, session)

    # 给这个 SPU 的 SKU 上库存
    detail = await client.get(f"/api/spus/{spu_id}")
    sku_ids = [s["id"] for s in detail.json()["data"]["skus"]]
    sku = sku_ids[0]
    price = next(s["price"] for s in detail.json()["data"]["skus"] if s["id"] == sku)

    from app.modules.inventory import service as inv
    from app.modules.inventory.models import Warehouse

    async with get_session_factory()() as s, s.begin():
        wh = Warehouse(id=next_id(), shop_id=SHOP_ID, name="测试仓", is_default=True)
        s.add(wh)
        wh_id = wh.id
    async with get_session_factory()() as s, s.begin():
        await inv.init(
            s, sku_id=int(sku), warehouse_id=wh_id, qty=100, biz_key="calc-init"
        )

    buyer = await register(client, phone=BUYER_PHONE)
    headers = auth_header(buyer["accessToken"])

    # 领一张平台券（满 0 减 10 元）
    tpl_id = await _make_template(total=10, per_user=1, value=1000)
    resp = await client.post(
        f"/api/coupons/{tpl_id}/receive",
        headers={**headers, "Idempotency-Key": "CALC-K"},
    )
    assert resp.status_code == 200, resp.text
    code_id = resp.json()["data"]["id"]

    # 算价
    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku, "num": 2}], "couponCodeIds": [code_id]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["totalAmount"] == price * 2
    assert data["platformDiscount"] == 1000
    assert data["payableAmount"] == price * 2 - 1000
    # 分摊明细必须给出来：退款时按它算每行退多少
    line = data["items"][0]
    assert line["discountAmount"] == 1000
    assert sum(a["amount"] for a in line["allocations"]) == 1000
    # 本期未实现的两项要显式标注，别让前端以为算漏了
    assert data["freight"] == 0
    assert data["pointDeduction"] == 0
    assert len(data["notices"]) >= 1


async def test_calc_price_reports_unavailable_reason(client: AsyncClient) -> None:
    """用不了的券要说明原因，尤其是"还差多少"（docs/05 §7.3）。"""
    from tests.test_product import _create_and_publish

    async with get_session_factory()() as session:
        spu_id, _ = await _create_and_publish(client, session)

    detail = await client.get(f"/api/spus/{spu_id}")
    sku = detail.json()["data"]["skus"][0]["id"]

    buyer = await register(client, phone="13900139012")
    headers = auth_header(buyer["accessToken"])

    # 门槛设成 99999 元，肯定够不着
    tpl_id = await _make_template(total=10, per_user=1, value=1000, threshold=9999900)
    resp = await client.post(
        f"/api/coupons/{tpl_id}/receive",
        headers={**headers, "Idempotency-Key": "UNAVAIL-K"},
    )
    code_id = resp.json()["data"]["id"]

    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku, "num": 1}], "couponCodeIds": [code_id]},
        headers=headers,
    )
    data = resp.json()["data"]
    assert data["platformDiscount"] == 0
    assert len(data["unavailableCoupons"]) == 1
    assert data["unavailableCoupons"][0]["reason"] == "THRESHOLD_NOT_MET"
    assert "还差" in data["unavailableCoupons"][0]["reasonText"]


# ============================================================
# ⑧ 店铺券只作用于本店
# ============================================================
async def test_shop_coupon_only_matches_own_shop() -> None:
    """``shop_id > 0`` 的券只作用于本店商品。

    领取本身不校验店铺（领券时还不知道要买什么），
    作用范围由算价引擎处理 —— 那部分在 ``test_pricing.py`` 里覆盖。
    """
    now = datetime.now(UTC)
    async with get_session_factory()() as s, s.begin():
        tpl = CouponTemplate(
            id=next_id(),
            shop_id=SHOP_ID,
            name="店铺券",
            type=1,
            get_type=1,
            discount_value=1000,
            threshold=0,
            total_count=10,
            per_user_limit=1,
            valid_type=1,
            valid_start=now - timedelta(hours=1),
            valid_end=now + timedelta(days=7),
            scope_type=1,
            status=2,
        )
        s.add(tpl)

    assert await _receive(tpl.id, 9001, "SHOP-K") == "OK"

    async with get_session_factory()() as s:
        code = await s.scalar(select(CouponCode).where(CouponCode.user_id == 9001))
    assert code is not None
    assert code.status == CODE_UNUSED


# ============================================================
# ⑨ 鉴权与幂等键
# ============================================================
async def test_receive_requires_login(client: AsyncClient) -> None:
    resp = await client.post("/api/coupons/1/receive", headers={"Idempotency-Key": "X"})
    assert resp.status_code == 401


async def test_receive_requires_idempotency_key(client: AsyncClient) -> None:
    """缺少 Idempotency-Key 必须报错，不能"没带就跳过幂等"。"""
    user = await register(client, phone=BUYER_PHONE)
    resp = await client.post(
        "/api/coupons/1/receive", headers=auth_header(user["accessToken"])
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ============================================================
# ⑩ 运营列表（只读）
#
# 这三个接口补的是一个很具体的坑：运营端原先**一个 GET 都没有**，
# 建完券模板/活动之后界面上再也看不到它，运营会以为提交失败。
# ============================================================
async def _admin(client: AsyncClient, session) -> dict:
    return await make_admin(client, session, phone=ADMIN_PHONE)


async def _create_template_via_api(
    client: AsyncClient,
    headers: dict,
    *,
    name: str = "满200减30",
    total: int = 500,
    starts_in: timedelta = timedelta(hours=-1),
    ends_in: timedelta = timedelta(days=30),
    valid_days: int | None = None,
) -> str:
    """建一个券模板。默认是"已经在有效期内"的固定区间券。

    给了 ``valid_days`` 就建「领取后 N 天」型 —— 那种没有 ``validStart``/``validEnd``，
    是验证状态推进时唯一落在"恒为进行中"分支的形状。
    """
    now = datetime.now(UTC)
    payload: dict = {
        "name": name,
        "type": 1,
        "discountValue": 3000,
        "threshold": 20000,
        "totalCount": total,
        "perUserLimit": 2,
        "scopeType": 1,
    }
    if valid_days is not None:
        payload["validType"] = VALID_DAYS_AFTER
        payload["validDays"] = valid_days
    else:
        payload["validType"] = 1
        payload["validStart"] = (now + starts_in).isoformat()
        payload["validEnd"] = (now + ends_in).isoformat()

    resp = await client.post("/api/admin/coupons/templates", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["data"]["id"])


async def test_admin_template_list_carries_quota_fields(client: AsyncClient, session) -> None:
    """运营建完券必须能在列表里看到，且能看到发行量。

    券中心那份出参（``CouponTemplateOut``）恰好不返回
    ``totalCount``/``issuedCount``/``scopeValue``，所以这里特意断言这几个字段。
    """
    admin = await _admin(client, session)
    h = auth_header(admin["accessToken"])
    tpl_id = await _create_template_via_api(client, h)

    resp = await client.get("/api/admin/coupons/templates", headers=h)
    assert resp.status_code == 200, resp.text
    row = next(i for i in resp.json()["data"]["items"] if i["id"] == tpl_id)

    assert row["totalCount"] == 500
    assert row["issuedCount"] == 0
    assert row["threshold"] == 20000
    assert row["typeText"] == "满减券"
    assert row["statusText"] == "进行中"


async def test_admin_template_list_paginates(client: AsyncClient, session) -> None:
    """游标分页：翻到底 hasMore 变 False，且两页不重叠。"""
    admin = await _admin(client, session)
    h = auth_header(admin["accessToken"])
    for i in range(3):
        await _create_template_via_api(client, h, name=f"券{i}")

    first = (await client.get("/api/admin/coupons/templates?limit=2", headers=h)).json()["data"]
    assert len(first["items"]) == 2
    assert first["hasMore"] is True
    assert first["nextCursor"]

    second = (
        await client.get(
            f"/api/admin/coupons/templates?limit=2&cursor={first['nextCursor']}", headers=h
        )
    ).json()["data"]
    assert len(second["items"]) == 1
    assert second["hasMore"] is False
    assert second["nextCursor"] is None

    ids = {i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}
    assert len(ids) == 3


async def test_admin_template_list_rejects_non_admin(client: AsyncClient) -> None:
    """买家调运营接口必须 403 —— 营销数据不该对普通用户开放。"""
    user = await register(client, phone=BUYER_PHONE)
    resp = await client.get(
        "/api/admin/coupons/templates", headers=auth_header(user["accessToken"])
    )
    assert resp.status_code == 403


async def test_bad_cursor_is_400_not_500(client: AsyncClient, session) -> None:
    """游标是外部输入，坏值要变成 400，不能让 base64 解码异常冒成 500。"""
    admin = await _admin(client, session)
    resp = await client.get(
        "/api/admin/coupons/templates?cursor=!!!not-base64!!!",
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_admin_activity_list_translates_texts(client: AsyncClient, session) -> None:
    """活动列表把层级/算法/状态翻成文案，前端不必各写一套中文。"""
    admin = await _admin(client, session)
    h = auth_header(admin["accessToken"])
    now = datetime.now(UTC)

    resp = await client.post(
        "/api/admin/promotions",
        json={
            "name": "店铺满减",
            "level": 1,
            "type": "PROMO_ORDER_SHOP",
            "calcType": 1,
            "discountValue": 5000,
            "threshold": 30000,
            "startAt": (now - timedelta(hours=1)).isoformat(),
            "endAt": (now + timedelta(days=7)).isoformat(),
            "priority": 5,
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    act_id = str(resp.json()["data"]["id"])

    data = (await client.get("/api/admin/promotions", headers=h)).json()["data"]
    row = next(i for i in data["items"] if i["id"] == act_id)

    assert row["level"] == 1
    assert row["levelText"] == "店铺级"
    assert row["type"] == "PROMO_ORDER_SHOP"
    assert row["typeText"] == "店铺活动"
    assert row["calcTypeText"] == "直降"
    assert row["statusText"] == "进行中"
    assert row["priority"] == 5


async def test_admin_activity_list_rejects_non_admin(client: AsyncClient) -> None:
    user = await register(client, phone=BUYER_PHONE)
    resp = await client.get(
        "/api/admin/promotions", headers=auth_header(user["accessToken"])
    )
    assert resp.status_code == 403


# ============================================================
# 首页 Banner
#
# 这条链路此前完全不存在（代码库里没有任何 banner 概念），所以从建到删整条走一遍。
# ============================================================
async def test_banner_crud_and_public_visibility(client: AsyncClient, session) -> None:
    """★ 公开接口只出启用中的，且按 sort 排序；停用后商城立刻看不到。"""
    admin = await make_admin(client, session)
    ah = auth_header(admin["accessToken"])

    created: list[dict] = []
    for i, (title, sort) in enumerate([("新品首发", 20), ("618 主会场", 10)]):
        resp = await client.post(
            "/api/admin/banners",
            json={
                "title": title,
                "image": f"/media/banners/x/{i}.webp",
                "sort": sort,
                "linkUrl": "/products/1",
            },
            headers=ah,
        )
        assert resp.status_code == 200, resp.text
        created.append(resp.json()["data"])

    # 公开接口：不需要登录，按 sort 升序（10 在前）
    public = await client.get("/api/banners")
    assert public.status_code == 200
    assert [b["title"] for b in public.json()["data"]] == ["618 主会场", "新品首发"]

    # 停用一条：公开接口里没了，但管理端还看得到 ——
    # 否则停用过的图就再也找不回来，等于变相删除
    banner_id = created[1]["id"]
    resp = await client.put(f"/api/admin/banners/{banner_id}", json={"status": 2}, headers=ah)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == 2
    assert [b["title"] for b in (await client.get("/api/banners")).json()["data"]] == ["新品首发"]
    assert len((await client.get("/api/admin/banners", headers=ah)).json()["data"]) == 2

    # 删除：成功一次，再来就是 404
    assert (await client.delete(f"/api/admin/banners/{banner_id}", headers=ah)).status_code == 200
    assert (await client.delete(f"/api/admin/banners/{banner_id}", headers=ah)).status_code == 404


async def test_banner_update_clears_link_and_keeps_others(
    client: AsyncClient, session
) -> None:
    """部分更新：只改传了的字段；link_url 传空串 = 清空链接。"""
    admin = await make_admin(client, session)
    ah = auth_header(admin["accessToken"])
    resp = await client.post(
        "/api/admin/banners",
        json={"title": "带链接", "image": "/media/banners/a.webp", "linkUrl": "/products/9", "sort": 5},
        headers=ah,
    )
    banner_id = resp.json()["data"]["id"]

    # 只改排序，标题/链接/图都不动
    resp = await client.put(f"/api/admin/banners/{banner_id}", json={"sort": 1}, headers=ah)
    data = resp.json()["data"]
    assert (data["sort"], data["title"], data["linkUrl"], data["image"]) == (
        1,
        "带链接",
        "/products/9",
        "/media/banners/a.webp",
    )

    # 空串清空链接（None 是"不改"，两者语义不同）
    resp = await client.put(f"/api/admin/banners/{banner_id}", json={"linkUrl": ""}, headers=ah)
    assert resp.json()["data"]["linkUrl"] is None


async def test_banner_rejects_external_link(client: AsyncClient, session) -> None:
    """★ link_url 只接受站内路径：前端只会 router.push，外链会变成死链。"""
    admin = await make_admin(client, session)
    resp = await client.post(
        "/api/admin/banners",
        json={
            "title": "外链",
            "image": "/media/banners/x.webp",
            "linkUrl": "https://example.com/a",
        },
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 400, resp.text


async def test_banner_admin_endpoints_are_role_guarded(client: AsyncClient, session) -> None:
    """管理端点是 admin / finance —— 买家碰不到；公开接口不需要登录。"""
    buyer = await register(client, phone="13800138091")
    bh = auth_header(buyer["accessToken"])

    assert (await client.get("/api/admin/banners", headers=bh)).status_code == 403
    resp = await client.post(
        "/api/admin/banners",
        json={"title": "x", "image": "/media/banners/x.webp"},
        headers=bh,
    )
    assert resp.status_code == 403
    # 公开接口不带令牌也通
    assert (await client.get("/api/banners")).status_code == 200


# ============================================================
# 定向发券：按手机号定位用户
# ============================================================


async def test_lookup_user_by_phone(client: AsyncClient, session) -> None:
    """★ 运营手上只有手机号，必须能换回 userId —— 雪花 ID 他拿不到。

    这个接口存在的唯一理由就是那件事，所以返回的三样都要能用：
    ``userId`` 拿去发券，昵称 + 打码号拿去核对没找错人。
    """
    buyer = await register(client, phone=BUYER_PHONE)
    buyer_id = (
        await client.get("/api/me", headers=auth_header(buyer["accessToken"]))
    ).json()["data"]["id"]
    admin = await _admin(client, session)

    resp = await client.get(
        "/api/admin/users/lookup",
        params={"phone": BUYER_PHONE},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["userId"] == buyer_id
    assert data["nickname"]
    assert data["phoneMasked"] == "139****9011"


async def test_lookup_user_by_phone_not_found(client: AsyncClient, session) -> None:
    """查不到要明确报错，不能返回空让运营去猜。"""
    admin = await _admin(client, session)
    resp = await client.get(
        "/api/admin/users/lookup",
        params={"phone": "13900139999"},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 404
    assert "手机号" in resp.json()["message"]


async def test_issue_rejects_unknown_user(client: AsyncClient, session) -> None:
    """★ 给不存在的用户发券要拦住。

    以前不校验 —— 运营手抄错一位数字就会静静地发出一张永远没人能领的券：
    库里多了记录、模板上多了券，但谁都不会发现。
    """
    admin = await _admin(client, session)
    headers = auth_header(admin["accessToken"])
    tpl_id = await _create_template_via_api(client, headers)

    resp = await client.post(
        "/api/admin/coupons/issue",
        json={"templateId": tpl_id, "userId": str(next_id()), "count": 1},
        headers=headers,
    )
    assert resp.status_code == 404
    assert "用户" in resp.json()["message"]


# ============================================================
# 补发的两道闸 + 记录
# ============================================================


async def _issue_ctx(client: AsyncClient, session) -> dict:
    """一个可发券的上下文：管理员 headers、模板 id、收件人 userId。"""
    admin = await _admin(client, session)
    headers = auth_header(admin["accessToken"])
    tpl_id = await _create_template_via_api(client, headers, name="补发验证券")
    buyer = await register(client, phone=BUYER_PHONE)
    buyer_id = (
        await client.get("/api/me", headers=auth_header(buyer["accessToken"]))
    ).json()["data"]["id"]
    return {"headers": headers, "tplId": tpl_id, "userId": buyer_id}


async def test_issue_per_user_template_cap(client: AsyncClient, session) -> None:
    """★ 同一用户在同一模板上累计最多补发 N 张 —— 挡住"给一个小号反复刷"。

    这道闸**不限时间窗**：累积行为加窗口反而能隔天绕过去。
    """
    ctx = await _issue_ctx(client, session)

    async def issue(n: int):
        return await client.post(
            "/api/admin/coupons/issue",
            json={"templateId": ctx["tplId"], "userId": ctx["userId"], "count": n},
            headers=ctx["headers"],
        )

    assert (await issue(ISSUE_MAX_PER_USER_TPL)).status_code == 200
    over = await issue(1)
    assert over.status_code == 400
    assert "上限" in over.json()["message"]


async def test_issue_operator_daily_cap(client: AsyncClient, session, monkeypatch) -> None:
    """★ 单个运营 24 小时的补发总量也封顶。

    阈值调小来测 —— 真按 200 张跑这个用例要发 200 次请求，不值得。
    """
    monkeypatch.setattr(service, "ISSUE_MAX_PER_OPERATOR_24H", 2)
    ctx = await _issue_ctx(client, session)

    async def issue(n: int):
        return await client.post(
            "/api/admin/coupons/issue",
            json={"templateId": ctx["tplId"], "userId": ctx["userId"], "count": n},
            headers=ctx["headers"],
        )

    assert (await issue(2)).status_code == 200
    over = await issue(1)
    assert over.status_code == 400
    assert "24 小时" in over.json()["message"]


async def test_issue_records_are_visible(client: AsyncClient, session) -> None:
    """★ 补发要查得到：谁、何时、给谁、发了哪张券。

    补发**不占活动额度**，"活动库存"根本不构成约束 —— 真正有效的那道是"能被看见"。
    """
    ctx = await _issue_ctx(client, session)
    resp = await client.post(
        "/api/admin/coupons/issue",
        json={"templateId": ctx["tplId"], "userId": ctx["userId"], "count": 2},
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    issued = {c["code"] for c in resp.json()["data"]}
    assert len(issued) == 2

    page = await client.get("/api/admin/coupons/issues", headers=ctx["headers"])
    assert page.status_code == 200, page.text
    data = page.json()["data"]

    mine = [r for r in data["items"] if r["code"] in issued]
    assert len(mine) == 2  # 一行 = 一张券
    row = mine[0]
    assert row["operatorName"]  # 操作人名解析出来了，不是 admin:123 这种
    assert row["nickname"]  # 收件人名
    assert row["phoneMasked"] == "139****9011"
    assert row["templateName"] == "补发验证券"
    assert row["remark"] == "客服补发"

    # 汇总让"今天发了多少"一眼可见
    assert data["summary"]["total"] >= 2
    assert any(o["count"] >= 2 for o in data["summary"]["byOperator"])


# ============================================================
# 建券时挑适用范围（选目标，而不是手填 ID）
# ============================================================


async def test_search_shops_by_name(client: AsyncClient, session) -> None:
    """★ 券里存的是 shopId，运营手上只有**店名** —— 得能按名字查到店。

    跟"按手机号找用户"是同一类问题（那个是发券，这个是建券），
    所以一样要有个能查的接口，否则运营无从知道自己的店 id 是多少。
    """
    merchant_a = await register(client, phone="13800139101")
    shop_a = await open_shop(client, merchant_a["accessToken"], name="星野数码旗舰店")
    merchant_b = await register(client, phone="13800139102")
    shop_b = await open_shop(client, merchant_b["accessToken"], name="蓝天家居生活馆")
    admin = await _admin(client, session)
    headers = auth_header(admin["accessToken"])

    # 不给关键词：列出最近的，刚建的两家要在里面
    all_shops = await client.get("/api/admin/shops", headers=headers)
    assert all_shops.status_code == 200, all_shops.text
    assert {s["id"] for s in all_shops.json()["data"]} >= {shop_a, shop_b}

    # 按店名收窄 —— 这就是运营实际要做的事
    hit = await client.get("/api/admin/shops", params={"keyword": "家居"}, headers=headers)
    assert hit.status_code == 200, hit.text
    rows = hit.json()["data"]
    assert [r["id"] for r in rows] == [shop_b]
    assert rows[0]["name"] == "蓝天家居生活馆"
    assert rows[0]["status"] == 1

    # 反方向：已经存下来的范围里只有 id，按 id 回看才能知道"这条限了哪几家店"
    by_ids = await client.get(
        "/api/admin/shops", params={"ids": f"{shop_a},{shop_b}"}, headers=headers
    )
    assert by_ids.status_code == 200, by_ids.text
    assert {r["id"] for r in by_ids.json()["data"]} == {shop_a, shop_b}

    # 传进来的 ids 里有垃圾也不该 500 —— 只取认得出的
    junk = await client.get("/api/admin/shops", params={"ids": f"{shop_a},abc"}, headers=headers)
    assert junk.status_code == 200, junk.text
    assert [r["id"] for r in junk.json()["data"]] == [shop_a]


async def test_coupon_scope_accepts_string_ids(client: AsyncClient, session) -> None:
    """★ scopeValue 要接受**字符串** id，而且一个数字都不能变。

    JS 的 number 装不下 2^53 以上的雪花 id：前端要是先 ``Number()`` 再发，
    存进去的就成了另一个数，pricing 里 ``item.sku_id in scope_value`` 永远为假
    —— 券看着完全正常，却一辈子不生效。所以前端按字符串传，后端得原样收下、
    精确落库。
    """
    admin = await _admin(client, session)
    headers = auth_header(admin["accessToken"])
    sku_ids = [next_id(), next_id()]

    resp = await client.post(
        "/api/admin/coupons/templates",
        json={
            "name": "指定商品券",
            "type": 1,
            "discountValue": 1000,
            "totalCount": 100,
            "validType": 2,
            "validDays": 7,
            "scopeType": 2,
            "scopeValue": [str(i) for i in sku_ids],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    tpl_id = int(resp.json()["data"]["id"])

    # 落库的是精确整数（JSONB 存数字，读回来逐位比对）
    stored = await session.scalar(
        select(CouponTemplate.scope_value).where(CouponTemplate.id == tpl_id)
    )
    assert stored == sku_ids
    assert all(isinstance(v, int) for v in stored)

    # 出参又转回字符串（雪花 id 出参一律字符串，避免前端再丢一次精度）
    listed = await client.get("/api/admin/coupons/templates", headers=headers)
    assert listed.status_code == 200, listed.text
    row = next(t for t in listed.json()["data"]["items"] if t["id"] == str(tpl_id))
    assert row["scopeValue"] == [str(i) for i in sku_ids]


async def test_coupon_scope_requires_target(client: AsyncClient, session) -> None:
    """★ 选了「指定商品」却不给目标要当场拒掉。

    空的 scope_value 在 pricing 里恒为假（``item.sku_id in []``），
    等于一张**永远用不出去的券** —— 而它在列表上和正常券长得一模一样，
    只有下单时才发现不生效，属于最难查的那类坏法。
    """
    admin = await _admin(client, session)
    resp = await client.post(
        "/api/admin/coupons/templates",
        json={
            "name": "没有目标的券",
            "type": 1,
            "discountValue": 1000,
            "totalCount": 100,
            "validType": 2,
            "validDays": 7,
            "scopeType": 2,
            "scopeValue": [],
        },
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 400
    assert "目标" in resp.json()["message"]


# ============================================================
# 促销活动：层级的适用方式
# ============================================================
async def _post_activity(
    client: AsyncClient,
    headers: dict,
    *,
    level: int,
    atype: str,
    calc_type: int,
    name: str = "验证活动",
    starts_in: timedelta = timedelta(hours=-1),
    ends_in: timedelta = timedelta(days=7),
    threshold: int = 0,
    scope_type: int | None = None,
    scope_value: list[str] | None = None,
):
    """建一个活动。

    ``type`` 由调用方显式给（而不是按 level 推），这样才构造得出"配歪了"的用例。
    时间窗默认是"已经在进行中"；要验证状态随窗口推进就得自己给偏移量。
    """
    now = datetime.now(UTC)
    payload: dict = {
        "name": name,
        "level": level,
        "type": atype,
        "calcType": calc_type,
        "discountValue": 5000,
        "threshold": threshold,
        "startAt": (now + starts_in).isoformat(),
        "endAt": (now + ends_in).isoformat(),
    }
    if scope_type is not None:
        payload["scopeType"] = scope_type
        payload["scopeValue"] = scope_value
    return await client.post("/api/admin/promotions", json=payload, headers=headers)


async def test_order_level_activity_rejects_fixed_price(client: AsyncClient, session) -> None:
    """★ 店铺级 / 平台级不能建「特价」。

    「特价」的语义是"把单价设成 discount_value"，只有单品级成立。订单级算的是
    "从总额里减一笔"，engine 里 ``_compute_discount`` 只处理直降与折扣 ——
    于是「特价 ¥50」会被当成「减 ¥50」，弹窗上写着"特价即定价"、算出来却是
    另一个数。建的时候就拒掉，别让它悄悄退化。
    """
    admin = await _admin(client, session)
    headers = auth_header(admin["accessToken"])

    for level, atype in ((LEVEL_SHOP, DISCOUNT_SHOP_PROMO), (LEVEL_PLATFORM, DISCOUNT_PLATFORM_PROMO)):
        resp = await _post_activity(client, headers, level=level, atype=atype, calc_type=CALC_FIXED)
        assert resp.status_code == 400, resp.text
        assert "单品级" in resp.json()["message"]

    # 订单级的直降 / 折扣照常可以建
    ok = await _post_activity(
        client, headers, level=LEVEL_SHOP, atype=DISCOUNT_SHOP_PROMO, calc_type=CALC_DIRECT
    )
    assert ok.status_code == 200, ok.text

    # 单品级的特价才是它该在的地方
    ok = await _post_activity(
        client, headers, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, calc_type=CALC_FIXED
    )
    assert ok.status_code == 200, ok.text


async def test_activity_type_must_match_level(client: AsyncClient, session) -> None:
    """★ type 必须跟着 level 走。

    ``level`` 决定"在哪一层算"，``type`` 只用来查冲突组与叠加矩阵。两者配歪了的
    后果两层还不一样：单品层压根不看 ``type``（照样生效），订单层按 ``type`` 过滤
    （静默不生效）—— 同一种错、两种表现，所以这里要卡死。
    """
    admin = await _admin(client, session)
    headers = auth_header(admin["accessToken"])

    # level=店铺级，却用了单品级的类型
    resp = await _post_activity(
        client, headers, level=LEVEL_SHOP, atype=DISCOUNT_ITEM_PROMO, calc_type=CALC_DIRECT
    )
    assert resp.status_code == 400, resp.text
    assert DISCOUNT_SHOP_PROMO in resp.json()["message"]

    # level=平台级，却用了店铺级的类型
    resp = await _post_activity(
        client, headers, level=LEVEL_PLATFORM, atype=DISCOUNT_SHOP_PROMO, calc_type=CALC_DIRECT
    )
    assert resp.status_code == 400, resp.text
    assert DISCOUNT_PLATFORM_PROMO in resp.json()["message"]

    # 三档配对了都照常可建
    for level, atype in (
        (LEVEL_ITEM, DISCOUNT_ITEM_PROMO),
        (LEVEL_SHOP, DISCOUNT_SHOP_PROMO),
        (LEVEL_PLATFORM, DISCOUNT_PLATFORM_PROMO),
    ):
        ok = await _post_activity(
            client, headers, level=level, atype=atype, calc_type=CALC_DIRECT
        )
        assert ok.status_code == 200, ok.text


async def test_item_level_activity_rejects_threshold(client: AsyncClient, session) -> None:
    """★ 单品级不能填「门槛」。

    门槛是**订单级**的概念：引擎只在 Level 1/2 用它过滤候选
    （``_apply_order_level``），单品级压根不读它（``_apply_item_level`` ——
    只按 ``_per_unit_discount`` 算每件减多少）。

    以前这一栏对三档都显示，于是运营填的"满 50 减 10"实际是**每一件都减 10**：
    39 元和 8999 元的商品一视同仁，而列表上还明晃晃写着"满 ¥50.00"。
    填的值没人看，这是最难发现的那种坏法，所以建的时候就拒。
    """
    admin = await _admin(client, session)
    headers = auth_header(admin["accessToken"])

    resp = await _post_activity(
        client,
        headers,
        level=LEVEL_ITEM,
        atype=DISCOUNT_ITEM_PROMO,
        calc_type=CALC_DIRECT,
        threshold=5000,
    )
    assert resp.status_code == 400, resp.text
    assert "订单级" in resp.json()["message"]

    # 订单级（店铺 / 平台）带门槛才是它该在的地方
    for level, atype in ((LEVEL_SHOP, DISCOUNT_SHOP_PROMO), (LEVEL_PLATFORM, DISCOUNT_PLATFORM_PROMO)):
        ok = await _post_activity(
            client,
            headers,
            level=level,
            atype=atype,
            calc_type=CALC_DIRECT,
            threshold=5000,
        )
        assert ok.status_code == 200, ok.text


async def test_activity_scope_needs_a_target(client: AsyncClient, session) -> None:
    """★ 选了适用范围却没挑到目标 = 一个**永远匹配不到任何商品**的活动。

    pricing 里是 ``item.sku_id in scope_value``，空列表恒为假。
    它在列表上和正常活动长得一模一样，只有下单时才发现不生效 ——
    与券那边同一个坑，同一个理由：建的时候就拒。
    """
    admin = await _admin(client, session)
    headers = auth_header(admin["accessToken"])

    resp = await _post_activity(
        client,
        headers,
        level=LEVEL_ITEM,
        atype=DISCOUNT_ITEM_PROMO,
        calc_type=CALC_DIRECT,
        scope_type=SCOPE_SKU,
        scope_value=[],
    )
    assert resp.status_code == 400, resp.text
    assert "目标" in resp.json()["message"]

    ok = await _post_activity(
        client,
        headers,
        level=LEVEL_ITEM,
        atype=DISCOUNT_ITEM_PROMO,
        calc_type=CALC_DIRECT,
        scope_type=SCOPE_SKU,
        scope_value=["1001"],
    )
    assert ok.status_code == 200, ok.text


async def test_scoped_item_activity_only_hits_its_own_sku(client: AsyncClient, session) -> None:
    """★ 限了范围的单品活动只作用于范围内的规格。

    这条守的是"活动表单补上适用范围"这件事：界面上选得出范围，算价就得真的按范围来
    —— 只限定 A 生效时，B 那一行一分也不能少。
    """
    from tests.test_product import _create_and_publish

    async with get_session_factory()() as s:
        spu_id, _merchant_token = await _create_and_publish(client, s)

    detail = await client.get(f"/api/spus/{spu_id}")
    skus = detail.json()["data"]["skus"]
    in_scope, out_of_scope = skus[0], skus[1]

    from app.modules.inventory import service as inv
    from app.modules.inventory.models import Warehouse

    async with get_session_factory()() as s, s.begin():
        wh = Warehouse(id=next_id(), shop_id=SHOP_ID, name="测试仓", is_default=True)
        s.add(wh)
        wh_id = wh.id
    async with get_session_factory()() as s, s.begin():
        for i, sku in enumerate((in_scope, out_of_scope)):
            await inv.init(
                s, sku_id=int(sku["id"]), warehouse_id=wh_id, qty=100, biz_key=f"scope-{i}"
            )

    admin = await _admin(client, session)
    created = await _post_activity(
        client,
        auth_header(admin["accessToken"]),
        level=LEVEL_ITEM,
        atype=DISCOUNT_ITEM_PROMO,
        calc_type=CALC_DIRECT,
        name="只对第一个规格",
        scope_type=SCOPE_SKU,
        scope_value=[in_scope["id"]],
    )
    assert created.status_code == 200, created.text

    buyer = await register(client, phone=BUYER_PHONE)
    resp = await client.post(
        "/api/checkout/calc",
        json={
            "items": [
                {"skuId": in_scope["id"], "num": 1},
                {"skuId": out_of_scope["id"], "num": 1},
            ]
        },
        headers=auth_header(buyer["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    lines = {ln["skuId"]: ln for ln in data["items"]}

    # _post_activity 里 discountValue 固定 5000（50 元）
    assert lines[in_scope["id"]]["discountAmount"] == 5000
    assert lines[in_scope["id"]]["promoPrice"] == in_scope["price"] - 5000

    # ★ 范围外那一行完全没被碰到
    assert lines[out_of_scope["id"]]["discountAmount"] == 0
    assert lines[out_of_scope["id"]]["promoPrice"] == out_of_scope["price"]
    assert data["itemDiscount"] == 5000


async def test_category_scope_includes_subcategories(client: AsyncClient, session) -> None:
    """★ 「指定类目」含该类的全部子类目。

    商品**只能挂在末级类目**（``product._resolve_category``），所以"只匹配所选
    类目本身"在父类目上从来就不可能生效过 —— 选「图书」是一条商品都匹配不到的，
    而活动在列表上和正常的没区别。展开发生在 ``checkout._expand_category_scopes``
    （引擎是纯函数，读不了类目树）。
    """
    from tests.test_product import _make_category, _spu_payload

    admin = await _admin(client, session)
    ah = auth_header(admin["accessToken"])
    # ★ _make_category 要的是裸 token（它自己包 header），不是 header dict
    token = admin["accessToken"]
    root = await _make_category(client, token, name="图书")
    child = await _make_category(client, token, name="小说", parent_id=root)

    merchant = await register(client, phone="13800138021")
    await open_shop(client, merchant["accessToken"], name="书店")
    mh = auth_header(merchant["accessToken"])

    resp = await client.post("/api/merchant/spus", json=_spu_payload(child), headers=mh)
    assert resp.status_code == 200, resp.text
    spu_id = resp.json()["data"]["id"]
    assert (
        await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=mh)
    ).status_code == 200
    assert (
        await client.post(
            f"/api/admin/spus/{spu_id}/audit", json={"approved": True}, headers=ah
        )
    ).status_code == 200

    detail = (await client.get(f"/api/spus/{spu_id}")).json()["data"]
    sku = detail["skus"][0]

    from app.modules.inventory import service as inv
    from app.modules.inventory.models import Warehouse

    async with get_session_factory()() as s, s.begin():
        wh = Warehouse(id=next_id(), shop_id=int(detail["shopId"]), name="书店仓", is_default=True)
        s.add(wh)
        wh_id = wh.id
    async with get_session_factory()() as s, s.begin():
        await inv.init(s, sku_id=int(sku["id"]), warehouse_id=wh_id, qty=10, biz_key="cat-scope")

    # 活动限定**父类目「图书」**，而商品挂在子类目「小说」下
    created = await _post_activity(
        client,
        ah,
        level=LEVEL_ITEM,
        atype=DISCOUNT_ITEM_PROMO,
        calc_type=CALC_DIRECT,
        name="图书直降",
        scope_type=SCOPE_CATEGORY,
        scope_value=[root],
    )
    assert created.status_code == 200, created.text

    buyer = await register(client, phone=BUYER_PHONE)
    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku["id"], "num": 1}]},
        headers=auth_header(buyer["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["items"][0]["discountAmount"] == 5000

    # 反过来：限到**另一个一级类目**（数码）就不该碰到这本书 ——
    # 守住展开不会broaden 成"什么都匹配"
    other = await _make_category(client, token, name="数码")
    await _void(client, ah, f"/api/admin/promotions/{created.json()['data']['id']}/void")
    widened = await _post_activity(
        client,
        ah,
        level=LEVEL_ITEM,
        atype=DISCOUNT_ITEM_PROMO,
        calc_type=CALC_DIRECT,
        name="数码直降",
        scope_type=SCOPE_CATEGORY,
        scope_value=[other],
    )
    assert widened.status_code == 200, widened.text
    resp = await client.post(
        "/api/checkout/calc",
        json={"items": [{"skuId": sku["id"], "num": 1}]},
        headers=auth_header(buyer["accessToken"]),
    )
    assert resp.json()["data"]["items"][0]["discountAmount"] == 0


# ============================================================
# ⑪ 下线：活动与券模板的作废
#
# 补的是一个"功能没做"的缺口：运营侧原先只有"新建"，建完之后**没有任何办法
# 把它关掉**（没有删除，也没有停用）。``status = 4``（已作废）的常量和中文字案
# 一直都在模型里，只是没有任何代码会写这个值。
#
# ★ 缺的是"作废"，不是"删除"：活动一旦产生过订单，
#   ``trade.order_discount_snapshot.source_id`` 就指着它 —— 删行会让那些快照
#   变成查不到来源的孤儿。券模板那边早有同样的结论写在模型注释里："只能作废后新建"。
# ============================================================
async def _void(client: AsyncClient, headers: dict, path: str):
    return await client.post(path, headers=headers)


async def _status_of(session, activity_id: str) -> int:
    """读一列，而不是取实体 —— 免得读到同一 session 里的旧对象。"""
    return int(
        await session.scalar(
            select(PromoActivity.status).where(PromoActivity.id == int(activity_id))
        )
    )


async def _tpl_status_of(session, tpl_id: str) -> int:
    """券模板的同一件事（读列，不取实体）。"""
    return int(
        await session.scalar(
            select(CouponTemplate.status).where(CouponTemplate.id == int(tpl_id))
        )
    )


async def test_void_activity_takes_effect_immediately(client: AsyncClient, session) -> None:
    """★ 作废**当场失效**，不必等窗口结束。

    算价查询要求 ``status = 2``（``repo.list_active_activities``），置 4 之后
    下一次算价就不带它了。这条用例的时间窗还剩 7 天 —— 所以"不生效"只可能来自
    作废本身，不可能是窗口到期。
    """
    admin = await _admin(client, session)
    h = auth_header(admin["accessToken"])

    created = await _post_activity(
        client, h, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, calc_type=CALC_DIRECT
    )
    assert created.status_code == 200, created.text
    activity_id = str(created.json()["data"]["id"])

    now = datetime.now(UTC)
    active = await repo.list_active_activities(session, now=now)
    assert [str(a.id) for a in active] == [activity_id]

    resp = await _void(client, h, f"/api/admin/promotions/{activity_id}/void")
    assert resp.status_code == 200, resp.text

    # ★ 行还在，只是状态变了 —— 订单里的优惠快照指着这一行，不能删
    assert await _status_of(session, activity_id) == ACTIVITY_STATUS_VOID
    assert await repo.list_active_activities(session, now=now) == []

    # 运营列表里带出"已作废"文案
    listed = await client.get("/api/admin/promotions", headers=h)
    row = next(i for i in listed.json()["data"]["items"] if i["id"] == activity_id)
    assert row["statusText"] == "已作废"


async def test_void_activity_rejects_double_void_and_unknown_id(
    client: AsyncClient, session
) -> None:
    """重复作废要被拒（两个运营都点了，第二个会以为自己关掉了），未知 id 报 404。"""
    admin = await _admin(client, session)
    h = auth_header(admin["accessToken"])

    created = await _post_activity(
        client, h, level=LEVEL_SHOP, atype=DISCOUNT_SHOP_PROMO, calc_type=CALC_DIRECT
    )
    activity_id = str(created.json()["data"]["id"])

    assert (
        await _void(client, h, f"/api/admin/promotions/{activity_id}/void")
    ).status_code == 200

    twice = await _void(client, h, f"/api/admin/promotions/{activity_id}/void")
    assert twice.status_code == 400, twice.text
    assert "已经作废" in twice.json()["message"]

    missing = await _void(client, h, f"/api/admin/promotions/{next_id()}/void")
    assert missing.status_code == 404, missing.text


async def test_cron_advances_activity_status_by_time_window(client: AsyncClient, session) -> None:
    """★ 状态必须按时钟推进，否则"未开始"的活动**永远不会开始**。

    活动状态是建的那一刻算一次就写死的（``router.create_activity``），而算价查询
    硬性要求 ``status = 2``。没有 ``refresh_activity_status``，运营写的"零点开抢"
    会永远停在未开始 —— 列表上看着正常，下单却不生效。
    """
    admin = await _admin(client, session)
    h = auth_header(admin["accessToken"])

    async def _make(name: str, starts_in: timedelta, ends_in: timedelta) -> str:
        resp = await _post_activity(
            client,
            h,
            level=LEVEL_ITEM,
            atype=DISCOUNT_ITEM_PROMO,
            calc_type=CALC_DIRECT,
            name=name,
            starts_in=starts_in,
            ends_in=ends_in,
        )
        assert resp.status_code == 200, resp.text
        return str(resp.json()["data"]["id"])

    future = await _make("还没到点", timedelta(days=1), timedelta(days=2))
    live = await _make("正在跑", timedelta(hours=-1), timedelta(hours=1))
    past = await _make("窗口整个过去了", timedelta(days=-2), timedelta(days=-1))
    killed = await _make("已作废", timedelta(hours=-1), timedelta(hours=1))
    await _void(client, h, f"/api/admin/promotions/{killed}/void")

    # 建出来的初始状态：只有落在窗口里的那条是"进行中"
    assert await _status_of(session, future) == ACTIVITY_STATUS_NOT_STARTED
    assert await _status_of(session, live) == ACTIVITY_STATUS_ONGOING
    assert await _status_of(session, past) == ACTIVITY_STATUS_NOT_STARTED
    assert await _status_of(session, killed) == ACTIVITY_STATUS_VOID

    changed = await refresh_promo_status({"session_factory": get_session_factory()})
    # 只碰了"窗口已经到点"的那两条里的**一条** —— live 本来就是进行中，
    # 「只在真的变化时才写」把它排除了（否则 updated_at 会被每分钟重写）
    assert changed["activities"] == 1
    assert await _status_of(session, future) == ACTIVITY_STATUS_NOT_STARTED  # 还没到点，不动
    assert await _status_of(session, live) == ACTIVITY_STATUS_ONGOING
    assert await _status_of(session, past) == ACTIVITY_STATUS_ENDED
    assert await _status_of(session, killed) == ACTIVITY_STATUS_VOID  # 作废不被复活


async def test_cron_advances_coupon_template_status(client: AsyncClient, session) -> None:
    """★ 券模板的状态也要按有效期推进 —— 否则过期券在运营列表里永远是"进行中"。

    与活动同一个毛病（见 ``repo.refresh_coupon_template_status``），后果轻一些：
    能不能领由 ``valid_end > now()`` 和 ``claim_template_quota`` 管，状态列只是给
    运营看的 —— 但运营就是照着这一列判断"这张券还能不能领"。
    """
    admin = await _admin(client, session)
    h = auth_header(admin["accessToken"])

    not_started = await _create_template_via_api(
        client, h, name="还没开始", starts_in=timedelta(days=1), ends_in=timedelta(days=2)
    )
    ongoing = await _create_template_via_api(
        client, h, name="正在发", starts_in=timedelta(days=-1), ends_in=timedelta(days=1)
    )
    ended = await _create_template_via_api(
        client, h, name="早过期了", starts_in=timedelta(days=-3), ends_in=timedelta(days=-2)
    )
    # 「领取后 N 天」型没有 validStart/validEnd，是唯一落在"恒进行中"分支的形状
    days_after = await _create_template_via_api(client, h, name="领取后30天", valid_days=30)
    killed = await _create_template_via_api(client, h, name="已作废", starts_in=timedelta(days=-1))
    await _void(client, h, f"/api/admin/coupons/templates/{killed}/void")

    # 建出来一律是"进行中" —— ``router.create_template`` 就是这么写的，
    # 状态由这个任务在一分钟内校正
    for tpl_id in (not_started, ongoing, ended, days_after):
        assert await _tpl_status_of(session, tpl_id) == COUPON_TPL_ONGOING

    counts = await refresh_promo_status({"session_factory": get_session_factory()})

    # 只有"还没开始"和"早过期了"真的变了；ongoing 本来就在进行中，不重写
    assert counts["templates"] == 2
    assert await _tpl_status_of(session, not_started) == COUPON_TPL_NOT_STARTED
    assert await _tpl_status_of(session, ongoing) == COUPON_TPL_ONGOING
    assert await _tpl_status_of(session, ended) == COUPON_TPL_ENDED
    assert await _tpl_status_of(session, days_after) == COUPON_TPL_ONGOING
    assert await _tpl_status_of(session, killed) == COUPON_TPL_VOID  # 作废不被复活

    # 运营列表里能直接看出过期了
    listed = await client.get("/api/admin/coupons/templates", headers=h)
    rows = {i["id"]: i["statusText"] for i in listed.json()["data"]["items"]}
    assert rows[ended] == "已结束"
    assert rows[not_started] == "未开始"
    assert rows[killed] == "已作废"

    # 「未开始」的券不再出现在券中心 —— 状态列修正顺带把"没到点就能领"也关掉了
    buyer = await register(client, phone=BUYER_PHONE)
    avail = await client.get("/api/coupons/available", headers=auth_header(buyer["accessToken"]))
    ids = {i["template"]["id"] for i in avail.json()["data"]}
    assert not_started not in ids
    assert ongoing in ids


async def test_void_coupon_template_stops_claiming_but_keeps_issued(
    client: AsyncClient, session
) -> None:
    """★ 券模板作废：**止住新的领取**，但不碰已经发出去的券。

    "券一旦发出去就是承诺" —— 这是作废与召回的分界。已领到的券有自己的生命周期
    （未使用 / 已使用 / 已过期），运营反悔不能把它作废掉。
    """
    admin = await _admin(client, session)
    h = auth_header(admin["accessToken"])
    buyer = await register(client, phone=BUYER_PHONE)
    bh = auth_header(buyer["accessToken"])
    tpl_id = await _create_template_via_api(client, h, name="待作废券")

    # 作废前：券中心看得见，也领得到
    avail = await client.get("/api/coupons/available", headers=bh)
    assert any(i["template"]["id"] == tpl_id for i in avail.json()["data"])

    claimed = await client.post(
        f"/api/coupons/{tpl_id}/receive", headers={**bh, "Idempotency-Key": "VOID-BEFORE"}
    )
    assert claimed.status_code == 200, claimed.text
    code_id = claimed.json()["data"]["id"]

    resp = await _void(client, h, f"/api/admin/coupons/templates/{tpl_id}/void")
    assert resp.status_code == 200, resp.text

    # 券中心不再展示
    avail = await client.get("/api/coupons/available", headers=bh)
    assert not any(i["template"]["id"] == tpl_id for i in avail.json()["data"])

    # 直接调领券接口也拿不到，且文案说的是"已下架"而不是"抢光了" ——
    # 后者会让人以为还能等补货
    again = await client.post(
        f"/api/coupons/{tpl_id}/receive", headers={**bh, "Idempotency-Key": "VOID-AFTER"}
    )
    assert again.status_code == 422, again.text
    assert again.json()["code"] == "ACTIVITY_ENDED"
    assert "下架" in again.json()["message"]

    # 已经领到的那张券照旧在"我的券"里，未使用
    mine = await client.get("/api/my/coupons", headers=bh)
    row = next(i for i in mine.json()["data"] if i["id"] == code_id)
    assert row["statusText"] == "未使用"

    # 运营列表里状态文案是"已作废"
    listed = await client.get("/api/admin/coupons/templates", headers=h)
    row = next(i for i in listed.json()["data"]["items"] if i["id"] == tpl_id)
    assert row["statusText"] == "已作废"

    # 重复作废 → 400
    twice = await _void(client, h, f"/api/admin/coupons/templates/{tpl_id}/void")
    assert twice.status_code == 400, twice.text
    assert "已经作废" in twice.json()["message"]


# ============================================================
# 站点主题（运营启用、全站生效）
# ============================================================
async def test_site_theme_defaults_to_neutral_with_options(client: AsyncClient, session) -> None:
    """公开接口：默认中性皮肤，并**一并下发可选清单**（后台选择器用它，省一份清单）。"""
    resp = await client.get("/api/site-theme")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["skin"] == "neutral"
    assert [o["value"] for o in data["options"]] == [
        "neutral",
        "promo-618",
        "promo-double11",
        "promo-spring",
    ]
    assert all(o["label"] for o in data["options"]), "每个选项都要有人看的名字"


async def test_site_theme_update_is_immediately_public(client: AsyncClient, session) -> None:
    """运营改完，公开接口立刻跟着变 —— 买家端下次加载就读到新皮肤。"""
    admin = await make_admin(client, session)
    h = auth_header(admin["accessToken"])

    resp = await client.put("/api/admin/site-theme", json={"skin": "promo-618"}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["skin"] == "promo-618"

    assert (await client.get("/api/site-theme")).json()["data"]["skin"] == "promo-618"
    async with get_session_factory()() as s:
        assert await s.scalar(text("SELECT skin FROM promotion.site_theme")) == "promo-618"


@pytest.mark.parametrize("skin", ["promo-nope", "", "NEUTRAL", "618"])
async def test_site_theme_rejects_unknown_skin(
    client: AsyncClient, session, skin: str
) -> None:
    """白名单外的值在 Pydantic 那层被挡下（400），一个字都不会进库。"""
    admin = await make_admin(client, session)
    resp = await client.put(
        "/api/admin/site-theme", json={"skin": skin}, headers=auth_header(admin["accessToken"])
    )
    assert resp.status_code == 400, f"{skin!r} 该被拒：{resp.text}"
    assert (await client.get("/api/site-theme")).json()["data"]["skin"] == "neutral"


async def test_site_theme_write_requires_admin(client: AsyncClient, session) -> None:
    """买家改不了全站皮肤（公开读、运营写）。"""
    buyer = await register(client, phone=BUYER_PHONE)
    resp = await client.put(
        "/api/admin/site-theme",
        json={"skin": "promo-618"},
        headers=auth_header(buyer["accessToken"]),
    )
    assert resp.status_code == 403


# ============================================================
# 客服联系方式（运营填、公开可读）
# ============================================================
async def test_site_contact_defaults_to_unconfigured(client: AsyncClient) -> None:
    """默认**三项全空**。

    ★ 是 ``null`` 而不是空字符串：未配置要是一个**可判断的状态** —— 买家端据此
      只展示真正填了的项，一项都没有就不承诺一个不存在的渠道。
    """
    resp = await client.get("/api/site-contact")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "serviceEmail": None,
        "servicePhone": None,
        "serviceHours": None,
    }


async def test_site_contact_update_is_immediately_public(client: AsyncClient, session) -> None:
    """运营存完，公开接口立刻读得到 —— 买家端不必等任何缓存过期。

    ★ 这条同时是"更新真的落库了"的回归测试：单行配置如果被误加进测试清理的
      TRUNCATE 列表，UPDATE 会命中 0 行、接口却照样回 200（见 conftest 的说明）。
      断言直查数据库，就是为了让那种静默失效当场暴露。
    """
    admin = await make_admin(client, session)
    h = auth_header(admin["accessToken"])

    resp = await client.put(
        "/api/admin/site-contact",
        json={
            "serviceEmail": "kf@eshop.test",
            "servicePhone": "400-000-1234",
            "serviceHours": "工作日 9:00-18:00",
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["serviceEmail"] == "kf@eshop.test"

    public = (await client.get("/api/site-contact")).json()["data"]
    assert public["servicePhone"] == "400-000-1234"
    assert public["serviceHours"] == "工作日 9:00-18:00"

    async with get_session_factory()() as s:
        stored = await s.scalar(text("SELECT service_email FROM promotion.site_contact WHERE id = 1"))
    assert stored == "kf@eshop.test"


async def test_site_contact_blank_clears_and_overwrites(client: AsyncClient, session) -> None:
    """传空串 = **取消配置那一项**；且整行覆盖（没传的项也一并清掉）。

    这正是接口不做"部分更新"的原因：运营得删得掉一条填错的值。
    另外顺便验证**纯空格**也算空 —— 否则运营敲个空格还以为清掉了。
    """
    admin = await make_admin(client, session)
    h = auth_header(admin["accessToken"])
    await client.put(
        "/api/admin/site-contact",
        json={"serviceEmail": "kf@eshop.test", "servicePhone": "400-000-1234"},
        headers=h,
    )

    resp = await client.put("/api/admin/site-contact", json={"serviceEmail": "   "}, headers=h)
    assert resp.json()["data"] == {
        "serviceEmail": None,
        "servicePhone": None,
        "serviceHours": None,
    }
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT service_email, service_phone FROM promotion.site_contact WHERE id = 1"
                )
            )
        ).one()
    assert row == (None, None)


@pytest.mark.parametrize("bad", ["kf@eshop", "kf eshop@x.com", "@eshop.test", "kf@"])
async def test_site_contact_rejects_bad_email(
    client: AsyncClient, session, bad: str
) -> None:
    """邮箱只做**粗校验**：漏域名、把 @ 打成空格这类手误要挡在库外。

    ★ 不求完备 —— 一个邮箱到底能不能收到信只有发一封才知道。但"没配"和"配了一个
      收不到的信箱"对用户来说后果一样，所以明显的手误必须先拦下来。
    """
    admin = await make_admin(client, session)
    resp = await client.put(
        "/api/admin/site-contact",
        json={"serviceEmail": bad},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 400, f"{bad!r} 该被拒：{resp.text}"


async def test_site_contact_write_requires_admin(client: AsyncClient, session) -> None:
    """买家改不了平台联系方式（公开读、运营写）。"""
    buyer = await register(client, phone=BUYER_PHONE)
    resp = await client.put(
        "/api/admin/site-contact",
        json={"serviceEmail": "hacker@evil.test"},
        headers=auth_header(buyer["accessToken"]),
    )
    assert resp.status_code == 403


# ============================================================
# 进行中的活动（买家端顶部公告）
# ============================================================
async def test_active_promotions_only_platform_and_ongoing(
    client: AsyncClient, session
) -> None:
    """★ 公告**只播平台级、只播正在进行**的活动。

    店铺级活动的名字是**商家**起的，播到全站公告上等于平台替商家打广告 ——
    所以它必须被过滤掉，哪怕它正在进行。
    """
    admin = await make_admin(client, session)
    h = auth_header(admin["accessToken"])

    async def post(level: int, atype: str, name: str, starts_in: timedelta, ends_in: timedelta):
        resp = await _post_activity(
            client,
            h,
            level=level,
            atype=atype,
            calc_type=CALC_DIRECT,
            name=name,
            starts_in=starts_in,
            ends_in=ends_in,
        )
        assert resp.status_code == 200, resp.text

    await post(LEVEL_PLATFORM, DISCOUNT_PLATFORM_PROMO, "全平台直降", -timedelta(hours=1), timedelta(days=3))
    await post(LEVEL_PLATFORM, DISCOUNT_PLATFORM_PROMO, "还没开始", timedelta(days=1), timedelta(days=3))
    await post(LEVEL_PLATFORM, DISCOUNT_PLATFORM_PROMO, "已经结束", -timedelta(days=3), -timedelta(days=1))
    await post(LEVEL_SHOP, DISCOUNT_SHOP_PROMO, "本店满减", -timedelta(hours=1), timedelta(days=3))

    resp = await client.get("/api/promotions/active")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    rows = data["items"]
    assert [r["name"] for r in rows] == ["全平台直降"]
    assert rows[0]["level"] == LEVEL_PLATFORM
    assert rows[0]["endAt"]
    assert data["total"] == 1


async def test_active_promotions_sorted_by_end_at(client: AsyncClient, session) -> None:
    """最快结束的排最前 —— 公告要先说最紧急的那件。"""
    admin = await make_admin(client, session)
    h = auth_header(admin["accessToken"])
    for name, ends_in in (("慢的", timedelta(days=5)), ("快的", timedelta(days=1))):
        resp = await _post_activity(
            client,
            h,
            level=LEVEL_PLATFORM,
            atype=DISCOUNT_PLATFORM_PROMO,
            calc_type=CALC_DIRECT,
            name=name,
            starts_in=-timedelta(hours=1),
            ends_in=ends_in,
        )
        assert resp.status_code == 200, resp.text

    data = (await client.get("/api/promotions/active")).json()["data"]
    assert [r["name"] for r in data["items"]] == ["快的", "慢的"]


async def test_active_promotions_total_counts_beyond_the_cap(
    client: AsyncClient, session
) -> None:
    """活动数超过上限时，``total`` 仍是**真实条数**，不能等于 ``len(items)``。

    ★ 这条盯的是一个具体的错误算法：公告带写的是"还有 N 个活动进行中"，
      如果 N 用**截断后**的列表长度去算，超出的活动就既看不到、也不计数 ——
      7 个进行中会显示成"还有 4 个"，凭空少了两个。
    """
    admin = await make_admin(client, session)
    h = auth_header(admin["accessToken"])
    overflow = 2
    for i in range(ACTIVE_ACTIVITY_LIMIT + overflow):
        resp = await _post_activity(
            client,
            h,
            level=LEVEL_PLATFORM,
            atype=DISCOUNT_PLATFORM_PROMO,
            calc_type=CALC_DIRECT,
            name=f"活动{i}",
            starts_in=-timedelta(hours=1),
            ends_in=timedelta(days=i + 1),
        )
        assert resp.status_code == 200, resp.text

    data = (await client.get("/api/promotions/active")).json()["data"]
    assert len(data["items"]) == ACTIVE_ACTIVITY_LIMIT
    assert data["total"] == ACTIVE_ACTIVITY_LIMIT + overflow


async def test_active_promotions_empty_when_nothing_running(
    client: AsyncClient, session
) -> None:
    """没有进行中的活动 → **空列表 + total 0**（买家端据此整条不渲染，而不是显示一句空话）。"""
    resp = await client.get("/api/promotions/active")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"items": [], "total": 0}
