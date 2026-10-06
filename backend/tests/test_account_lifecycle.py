"""账号生命周期：改密 / 运营重置 / 封禁解封 / 注销。

连真实 PostgreSQL + Redis。造订单那部分复用 ``test_trade`` / ``test_aftersale``
的辅助函数（项目既有做法），否则这里要再抄一整套下单链路。

★ 几处**必须保住**的语义，都是容易在重构里被改坏的：

- 注销后**订单的收货人快照不能动** —— 它是法律/对账凭证（docs/07）
- 注销后同号在冷静期内不能注册、过了冷静期能注册
- 注销后登录的响应与"手机号不存在"**完全一致**，不给账号枚举留口子
- 封禁的提示要带上运营填的理由
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.crypto import phone_decrypt, phone_hash
from app.core.db import get_session_factory
from app.core.redis import get_redis
from app.core.security import validate_password_strength
from tests.conftest import (
    TEST_PASSWORD,
    TEST_PHONE,
    api_code,
    auth_header,
    make_admin,
    open_shop,
    register,
)
from tests.test_aftersale import _finished_order
from tests.test_trade import _create_order, _prepare

ADMIN_PHONE = "13900139001"
OTHER_ADMIN_PHONE = "13900139002"

BUYER = "13800138101"
BUYER_B = "13800138102"
MERCHANT = "13800138103"

NEW_PASSWORD = "Str0ng-New!2026"


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
# 内部小工具
# ============================================================
async def _row(phone: str, *, closed: bool | None = None):
    """按手机号取用户行。``closed=None`` 时只取未注销的那条。

    ★ 必须显式选：同号被重新注册后，同一个 phone_hash 下会同时存在
      "活跃的新号"与"已注销的旧号"两行。
    """
    where = "phone_hash = :h"
    if closed is True:
        where += " AND status = 3"
    elif closed is False:
        where += " AND status <> 3"
    async with get_session_factory()() as s:
        result = await s.execute(
            text(
                "SELECT id, status, phone_hash, phone_cipher, phone_masked, nickname,"
                f" closed_at FROM account.user WHERE {where} ORDER BY id DESC"
            ),
            {"h": phone_hash(phone)},
        )
        return result.mappings().all()


async def _live(phone: str) -> dict:
    rows = await _row(phone, closed=False)
    assert len(rows) == 1, f"{phone} 的未注销记录应当恰有一条，实际 {len(rows)}"
    return dict(rows[0])


async def _flows(user_id: int) -> list[dict]:
    async with get_session_factory()() as s:
        result = await s.execute(
            text(
                "SELECT event, from_status, to_status, operator_type, operator_id, remark"
                " FROM account.user_state_flow WHERE user_id = :u ORDER BY id"
            ),
            {"u": user_id},
        )
        return [dict(r) for r in result.mappings().all()]


async def _login(client: AsyncClient, phone: str, password: str = TEST_PASSWORD):
    return await client.post("/api/auth/login", json={"phone": phone, "password": password})


async def _refresh(client: AsyncClient, refresh_token: str):
    return await client.post("/api/auth/refresh", json={"refreshToken": refresh_token})


async def _close(client: AsyncClient, headers: dict, password: str = TEST_PASSWORD):
    return await client.post("/api/me/deactivate", json={"password": password}, headers=headers)


async def _status_of(user_id: int) -> int:
    async with get_session_factory()() as s:
        return int(
            await s.scalar(text("SELECT status FROM account.user WHERE id = :i"), {"i": user_id})
        )


# ============================================================
# 改密码
# ============================================================
async def test_change_password_rotates_tokens_and_kills_other_sessions(
    client: AsyncClient, session
) -> None:
    data = await register(client)
    old_refresh = data["refreshToken"]

    resp = await client.put(
        "/api/me/password",
        json={"oldPassword": TEST_PASSWORD, "newPassword": NEW_PASSWORD},
        headers=auth_header(data["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    tokens = resp.json()["data"]
    assert tokens["accessToken"] and tokens["refreshToken"]

    # 改密前签发的 refresh token 必须失效（"别的设备在用我的号"是改密最常见的动机）
    assert (await _refresh(client, old_refresh)).status_code == 401
    # 当前会话拿到的新令牌可用，用户不用重新登录
    assert (
        await client.get("/api/me", headers=auth_header(tokens["accessToken"]))
    ).status_code == 200
    # 新密码能登，旧密码不能
    assert (await _login(client, TEST_PHONE, NEW_PASSWORD)).status_code == 200
    assert api_code((await _login(client, TEST_PHONE)).json()) == "LOGIN_FAILED"


async def test_change_password_rejects_wrong_old_password(client: AsyncClient, session) -> None:
    data = await register(client, phone=BUYER)
    resp = await client.put(
        "/api/me/password",
        json={"oldPassword": "Wrong-Pass!2026", "newPassword": NEW_PASSWORD},
        headers=auth_header(data["accessToken"]),
    )
    assert resp.status_code == 400
    assert api_code(resp.json()) == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("new_password", "why"),
    [
        ("short1!", "长度不足 8"),
        ("12345678", "只有数字一类，且是弱口令"),
        ("password", "弱口令"),
    ],
)
async def test_change_password_rejects_weak_new_password(
    client: AsyncClient, session, new_password: str, why: str
) -> None:
    data = await register(client, phone=BUYER)
    resp = await client.put(
        "/api/me/password",
        json={"oldPassword": TEST_PASSWORD, "newPassword": new_password},
        headers=auth_header(data["accessToken"]),
    )
    assert resp.status_code == 400, f"{why} 应当被拒：{resp.text}"


# ============================================================
# 运营重置密码
# ============================================================
async def test_admin_reset_password_returns_usable_temp_and_kicks_sessions(
    client: AsyncClient, session
) -> None:
    admin = await make_admin(client, session)
    buyer = await register(client, phone=BUYER)
    uid = (await _live(BUYER))["id"]
    admin_h = auth_header(admin["accessToken"])

    resp = await client.post(f"/api/admin/users/{uid}/reset-password", headers=admin_h)
    assert resp.status_code == 200, resp.text
    temp = resp.json()["data"]["tempPassword"]

    # 生成的口令必须自己能过强度校验（否则用户拿到的是一条设不上的口令）
    validate_password_strength(temp)
    # 重置的意义就是"把当前登录者请出去"
    assert (await _refresh(client, buyer["refreshToken"])).status_code == 401
    assert (await _login(client, BUYER, temp)).status_code == 200


async def test_admin_reset_password_rejects_platform_account(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    await make_admin(client, session, phone=OTHER_ADMIN_PHONE)
    other_id = (await _live(OTHER_ADMIN_PHONE))["id"]

    resp = await client.post(
        f"/api/admin/users/{other_id}/reset-password", headers=auth_header(admin["accessToken"])
    )
    assert resp.status_code == 403
    assert api_code(resp.json()) == "FORBIDDEN"


# ============================================================
# 封禁 / 解封
# ============================================================
async def test_ban_blocks_login_and_refresh_then_unban_restores(
    client: AsyncClient, session
) -> None:
    admin = await make_admin(client, session)
    buyer = await register(client, phone=BUYER)
    uid = (await _live(BUYER))["id"]
    admin_h = auth_header(admin["accessToken"])

    resp = await client.post(
        f"/api/admin/users/{uid}/ban", json={"reason": "疑似刷单"}, headers=admin_h
    )
    assert resp.status_code == 200, resp.text

    # 登录被拒，并且**能看到理由**（理由就是写给用户看的）
    resp = await _login(client, BUYER)
    assert resp.status_code == 403
    assert api_code(resp.json()) == "ACCOUNT_BANNED"
    body = resp.json()
    assert "疑似刷单" in body["message"]
    # ★ 理由同时走**结构化 data**：前端据此分三行排版（标题 / 原因 / 联系方式），
    #   不用去切 message 里那个中文冒号。这条盯的就是原来那句拼出来的文案 ——
    #   「账号已被封禁，请联系客服：疑似刷单」把"原因"读成了"客服的联系方式"。
    assert body["data"]["reason"] == "疑似刷单"
    # ★ 买家看到的措辞是「冻结」而不是「封禁」（运营后台仍旧叫封禁），
    #   并且**不再指名一个并不存在的客服** —— 真要给联系方式，是登录页那条面板
    #   按运营配置的 promotion.site_contact 渲染的。
    assert "冻结" in body["message"]
    assert "请联系客服" not in body["message"]
    # 刷新令牌同样被拒，而且回的是**封禁**、不是"登录已过期"。
    #
    # ★ 这是一处**行为变更**：封禁会吊销该用户全部 refresh token，而账号状态的判断
    #   现在排在"令牌是否已吊销"**之前**。按原来的顺序，被封的人刷新时只会拿到
    #   401「登录已过期，请重新登录」—— 他会照着做，然后再失败一次，始终不知道
    #   号已被冻结、该找谁。反正他一样续不了期，但至少要被告知真实原因。
    refreshed = await _refresh(client, buyer["refreshToken"])
    assert refreshed.status_code == 403
    assert api_code(refreshed.json()) == "ACCOUNT_BANNED"

    resp = await client.post(f"/api/admin/users/{uid}/unban", headers=admin_h)
    assert resp.status_code == 200, resp.text
    assert (await _login(client, BUYER)).status_code == 200
    # 解封**不恢复**旧令牌：它当初被吊销过，所以仍然换不到新的 access token
    assert (await _refresh(client, buyer["refreshToken"])).status_code == 401


async def test_ban_rejects_self_and_platform_account(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    await make_admin(client, session, phone=OTHER_ADMIN_PHONE)
    admin_h = auth_header(admin["accessToken"])

    me_id = (await _live(ADMIN_PHONE))["id"]
    resp = await client.post(
        f"/api/admin/users/{me_id}/ban", json={"reason": "手滑"}, headers=admin_h
    )
    assert resp.status_code == 400

    other_id = (await _live(OTHER_ADMIN_PHONE))["id"]
    resp = await client.post(
        f"/api/admin/users/{other_id}/ban", json={"reason": "越权"}, headers=admin_h
    )
    assert resp.status_code == 403


async def test_unban_requires_banned_state(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    await register(client, phone=BUYER)
    uid = (await _live(BUYER))["id"]

    resp = await client.post(
        f"/api/admin/users/{uid}/unban", headers=auth_header(admin["accessToken"])
    )
    assert resp.status_code == 400


async def test_ban_writes_audit_flow_with_operator_and_reason(
    client: AsyncClient, session
) -> None:
    admin = await make_admin(client, session)
    await register(client, phone=BUYER)
    uid = (await _live(BUYER))["id"]
    admin_h = auth_header(admin["accessToken"])

    await client.post(f"/api/admin/users/{uid}/ban", json={"reason": "刷单"}, headers=admin_h)
    await client.post(f"/api/admin/users/{uid}/unban", headers=admin_h)

    flows = await _flows(uid)
    assert [f["event"] for f in flows] == ["BAN", "UNBAN"]
    ban = flows[0]
    assert (ban["from_status"], ban["to_status"]) == (1, 2)
    assert ban["operator_type"] == 4  # OperatorType.PLATFORM
    assert ban["operator_id"] == str((await _live(ADMIN_PHONE))["id"])
    assert ban["remark"] == "刷单"
    # 解封时清掉理由，否则用户下次被封时看到的还是上一次的原因
    assert (await _live(BUYER))["status"] == 1


# ============================================================
# 用户列表
# ============================================================
async def test_admin_user_list_filters_and_searches(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    admin_h = auth_header(admin["accessToken"])

    a = await register(client, phone=BUYER)
    await client.put(
        "/api/me", json={"nickname": "张三丰"}, headers=auth_header(a["accessToken"])
    )
    await register(client, phone=BUYER_B)
    b_id = (await _live(BUYER_B))["id"]

    # 昵称模糊
    resp = await client.get("/api/admin/users", params={"keyword": "张三"}, headers=admin_h)
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert [i["nickname"] for i in items] == ["张三丰"]
    # 手机号**只回掩码**（形如 138****8101），后台不提供解密查看
    assert "****" in items[0]["phone"]
    assert BUYER not in items[0]["phone"]

    # 手机号全匹配（走 phone_hash 等值，不是 LIKE）
    resp = await client.get("/api/admin/users", params={"keyword": BUYER_B}, headers=admin_h)
    assert [i["id"] for i in resp.json()["data"]["items"]] == [str(b_id)]

    # 状态过滤
    await client.post(f"/api/admin/users/{b_id}/ban", json={"reason": "测试"}, headers=admin_h)
    resp = await client.get("/api/admin/users", params={"status": 2}, headers=admin_h)
    assert [i["id"] for i in resp.json()["data"]["items"]] == [str(b_id)]
    assert resp.json()["data"]["items"][0]["statusText"] == "已封禁"


async def test_admin_user_list_requires_admin(client: AsyncClient, session) -> None:
    buyer = await register(client, phone=BUYER)
    resp = await client.get("/api/admin/users", headers=auth_header(buyer["accessToken"]))
    assert resp.status_code == 403


# ============================================================
# 注销：匿名化
# ============================================================
async def test_close_account_anonymizes_pii(client: AsyncClient, session) -> None:
    data = await register(client, phone=BUYER)
    resp = await _close(client, auth_header(data["accessToken"]))
    assert resp.status_code == 200, resp.text

    row = dict((await _row(BUYER, closed=True))[0])
    assert row["status"] == 3
    assert row["closed_at"] is not None
    assert row["nickname"] == "已注销用户"
    assert row["phone_masked"] == "已注销"
    # 密文是"能被解出真号"的那一份，必须不再是真号
    assert phone_decrypt(row["phone_cipher"]) == "__closed__"
    assert BUYER not in phone_decrypt(row["phone_cipher"])
    # 旧口令不可用：库里换成了一条谁也不知道的随机串
    assert api_code((await _login(client, BUYER)).json()) == "LOGIN_FAILED"

    # ★ phone_hash **刻意保留原值**：注销后还要能用手机号查出这条记录，
    #   才能判断"冷静期内不许同号重注册"。它是 HMAC，不可反查出号码。
    assert row["phone_hash"] == phone_hash(BUYER)


async def test_close_account_scrubs_and_soft_deletes_addresses(
    client: AsyncClient, session
) -> None:
    data = await register(client, phone=BUYER)
    headers = auth_header(data["accessToken"])
    resp = await client.post(
        "/api/me/addresses",
        json={
            "receiverName": "张三",
            "phone": BUYER,
            "province": "北京市",
            "city": "北京市",
            "district": "朝阳区",
            "detail": "某某路 1 号 2 单元 301",
            "regionCode": "110105",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    assert (await _close(client, headers)).status_code == 200

    uid = dict((await _row(BUYER, closed=True))[0])["id"]
    async with get_session_factory()() as s:
        result = await s.execute(
            text(
                "SELECT status, receiver_name, phone, detail FROM account.user_address"
                " WHERE user_id = :u"
            ),
            {"u": uid},
        )
        addr = result.mappings().one()
    assert addr["status"] == 2
    # 三条强 PII 都被覆写，不留明文
    assert (addr["receiver_name"], addr["phone"], addr["detail"]) == ("", "", "")


async def test_close_account_revokes_refresh_tokens(client: AsyncClient, session) -> None:
    data = await register(client, phone=BUYER)
    resp = await _close(client, auth_header(data["accessToken"]))
    assert resp.status_code == 200
    assert (await _refresh(client, data["refreshToken"])).status_code in (401, 403)


async def test_close_account_requires_correct_password(client: AsyncClient, session) -> None:
    data = await register(client, phone=BUYER)
    resp = await _close(client, auth_header(data["accessToken"]), password="Wrong-Pass!2026")
    assert resp.status_code == 400
    # 没被注销
    assert (await _live(BUYER))["status"] == 1


async def test_close_account_writes_audit_flow(client: AsyncClient, session) -> None:
    data = await register(client, phone=BUYER)
    uid = (await _live(BUYER))["id"]
    assert (await _close(client, auth_header(data["accessToken"]))).status_code == 200

    flows = await _flows(uid)
    assert len(flows) == 1
    assert flows[0]["event"] == "CLOSE"
    assert (flows[0]["from_status"], flows[0]["to_status"]) == (1, 3)
    assert flows[0]["operator_type"] == 1  # OperatorType.USER
    assert flows[0]["operator_id"] == str(uid)


# ============================================================
# 注销：前置守卫
# ============================================================
async def test_close_account_blocked_by_unfinished_order(client: AsyncClient, session) -> None:
    ctx = await _prepare(client, session)
    await _create_order(client, ctx)  # 未支付 → 待付款（未完成）

    resp = await _close(client, ctx["buyer_headers"])
    assert resp.status_code == 409
    assert api_code(resp.json()) == "ACCOUNT_HAS_UNFINISHED"
    assert "未完成订单" in resp.json()["message"]
    # 没被注销
    assert await _status_of(ctx["buyer_id"]) == 1


async def test_close_account_blocked_by_shop_owner(client: AsyncClient, session) -> None:
    merchant = await register(client, phone=MERCHANT)
    await open_shop(client, merchant["accessToken"])

    resp = await _close(client, auth_header(merchant["accessToken"]))
    assert resp.status_code == 409
    assert api_code(resp.json()) == "SHOP_OWNER_CANNOT_DEACTIVATE"
    assert (await _live(MERCHANT))["status"] == 1


# ============================================================
# 注销：前置检查（先问"能不能注销、被什么挡着"）
# ============================================================
async def _check(client: AsyncClient, headers: dict[str, str]):
    return await client.get("/api/me/deactivation", headers=headers)


async def test_deactivation_check_reports_blockers(client: AsyncClient, session) -> None:
    """★ 这个端点存在的意义：**别让用户白输一次密码**。

    原先只有提交之后才知道被挡，而那时密码已经输过了，提示还是个转瞬即逝的
    toast、也不说去哪处理。所以它必须**只报事实**（数量），让前端能按被挡的项
    各给一个跳转按钮 —— 拼好的一句话拆不出这些动作。
    """
    ctx = await _prepare(client, session)
    await _create_order(client, ctx)  # 未支付 → 待付款（未完成）

    resp = await _check(client, ctx["buyer_headers"])
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["canDeactivate"] is False
    assert data["orderCount"] == 1
    assert data["refundCount"] == 0
    assert data["hasShop"] is False


async def test_deactivation_check_clears_after_handling(client: AsyncClient, session) -> None:
    """把订单取消掉之后就该放行 —— 预检与真正注销共用同一份守卫，
    所以在这里"绿"了，提交时就不会再被挡。"""
    ctx = await _prepare(client, session)
    order = await _create_order(client, ctx)

    resp = await client.post(
        f"/api/orders/{order['orderMainNo']}/cancel", headers=ctx["buyer_headers"]
    )
    assert resp.status_code == 200, resp.text

    resp = await _check(client, ctx["buyer_headers"])
    data = resp.json()["data"]
    assert data["canDeactivate"] is True
    assert data["orderCount"] == 0

    # 而且真的能注销了
    assert (await _close(client, ctx["buyer_headers"])).status_code == 200
    assert await _status_of(ctx["buyer_id"]) == 3


async def test_deactivation_check_reports_shop_owner(client: AsyncClient, session) -> None:
    merchant = await register(client, phone=MERCHANT)
    await open_shop(client, merchant["accessToken"])

    resp = await _check(client, auth_header(merchant["accessToken"]))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["canDeactivate"] is False
    assert data["hasShop"] is True


async def test_deactivation_check_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/me/deactivation")).status_code == 401


async def test_close_account_keeps_order_receiver_snapshot(client: AsyncClient, session) -> None:
    """★ 注销**不能**动 trade.order_main 的收货人快照 —— 它是法律/对账凭证。"""
    ctx = await _prepare(client, session)
    order = await _finished_order(client, ctx)  # 已完成 → 不拦注销
    main_no = order["orderMainNo"]

    async def receiver() -> tuple[str, str]:
        async with get_session_factory()() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT receiver_name, receiver_phone FROM trade.order_main"
                        " WHERE order_main_no = :n"
                    ),
                    {"n": main_no},
                )
            ).one()
            return row[0], row[1]

    before = await receiver()
    assert before[0] and before[1]  # 有真实快照

    resp = await _close(client, ctx["buyer_headers"])
    assert resp.status_code == 200, resp.text
    assert await receiver() == before


# ============================================================
# 注销：手机号冷静期（惰性释放）
# ============================================================
async def test_closed_phone_cannot_register_within_cooldown(
    client: AsyncClient, session
) -> None:
    data = await register(client, phone=BUYER)
    assert (await _close(client, auth_header(data["accessToken"]))).status_code == 200

    resp = await client.post(
        "/api/auth/register", json={"phone": BUYER, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 409
    assert api_code(resp.json()) == "PHONE_IN_COOLDOWN"


async def test_closed_phone_can_register_after_cooldown(client: AsyncClient, session) -> None:
    """冷静期一过，同号可以重新注册。

    ★ 这里同时验证了"惰性释放"的机制：**不需要**改写旧行 ——
      ``uk_user_phone_hash`` 是 ``WHERE status <> 3`` 的部分唯一索引，
      已注销的行本来就不在索引里，新行带着同样的 phone_hash 也能插进去。
      所以旧行的 phone_hash 应当**保持原值不变**（冷静期查询还指着它）。
    """
    data = await register(client, phone=BUYER)
    uid = (await _live(BUYER))["id"]
    assert (await _close(client, auth_header(data["accessToken"]))).status_code == 200

    async with get_session_factory()() as s, s.begin():
        await s.execute(
            text(
                "UPDATE account.user SET closed_at = now() - make_interval(0, 0, 0, 31)"
                " WHERE id = :i"
            ),
            {"i": uid},
        )

    resp = await client.post(
        "/api/auth/register", json={"phone": BUYER, "password": NEW_PASSWORD, "nickname": "新用户"}
    )
    assert resp.status_code == 200, resp.text

    # 旧行还在、hash 未变；新行是活跃的、能用新口令登录
    old = dict((await _row(BUYER, closed=True))[0])
    assert old["id"] == uid
    assert old["phone_hash"] == phone_hash(BUYER)
    live = await _live(BUYER)
    assert live["id"] != uid
    assert live["nickname"] == "新用户"
    assert (await _login(client, BUYER, NEW_PASSWORD)).status_code == 200


async def test_closed_account_login_is_indistinguishable_from_unknown_phone(
    client: AsyncClient, session
) -> None:
    """注销后登录的响应必须与"手机号从未注册过"**完全一致**。

    否则任何人报一个手机号就能问出"这个号曾经注册过"（账号枚举）。
    """
    data = await register(client, phone=BUYER)
    assert (await _close(client, auth_header(data["accessToken"]))).status_code == 200

    closed_resp = await _login(client, BUYER)
    unknown_resp = await _login(client, "13800138999")
    assert api_code(closed_resp.json()) == api_code(unknown_resp.json()) == "LOGIN_FAILED"
    assert closed_resp.json()["message"] == unknown_resp.json()["message"]


# ============================================================
# 临时口令生成（纯函数）
# ============================================================
def test_generate_temp_password_always_strong() -> None:
    """运营重置用的口令必须**必然**过得了自己的强度校验。

    抽 200 次而不是 1 次：这条规则的实现是"四类各取一个再补足"，一旦有人
    改成"随机生成再试"，单次抽样可能碰巧通过，而线上会偶尔发给用户一条
    设不上的口令。
    """
    from app.core.security import generate_temp_password

    for _ in range(200):
        validate_password_strength(generate_temp_password())
