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
    CODE_UNUSED,
    ISSUE_MAX_PER_USER_TPL,
    CouponCode,
    CouponTemplate,
)
from tests.conftest import auth_header, make_admin, register

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
    client: AsyncClient, headers: dict, *, name: str = "满200减30", total: int = 500
) -> str:
    resp = await client.post(
        "/api/admin/coupons/templates",
        json={
            "name": name,
            "type": 1,
            "discountValue": 3000,
            "threshold": 20000,
            "totalCount": total,
            "perUserLimit": 2,
            "validType": 1,
            "validStart": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "validEnd": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "scopeType": 1,
        },
        headers=headers,
    )
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
