"""account 模块的集成测试。连真实 PostgreSQL + Redis。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.core.crypto import phone_decrypt, phone_hash
from app.modules.account.models import RefreshToken, User, UserAddress
from app.modules.freight.models import (
    DEFAULT_TPL_FIRST_PRICE,
    DEFAULT_TPL_FREE_THRESHOLD,
    DEFAULT_TPL_NAME,
)
from tests.conftest import (
    TEST_PASSWORD,
    TEST_PHONE,
    api_code,
    auth_header,
    make_admin,
    register,
)

# ============================================================
# 注册
# ============================================================


async def test_register_returns_tokens_and_persists_user(client: AsyncClient, session) -> None:
    data = await register(client)

    assert data["accessToken"]
    assert data["refreshToken"]
    assert data["tokenType"] == "Bearer"

    user = await session.scalar(select(User).where(User.phone_hash == phone_hash(TEST_PHONE)))
    assert user is not None
    assert user.nickname == "测试用户"
    assert user.role == "buyer"
    assert user.status == 1


async def test_password_is_hashed_and_phone_is_encrypted(client: AsyncClient, session) -> None:
    """★ 安全底线：库里不能出现明文密码或明文手机号。"""
    await register(client)
    user = await session.scalar(select(User).where(User.phone_hash == phone_hash(TEST_PHONE)))
    assert user is not None

    # 密码是 Argon2id 哈希，不是明文
    assert TEST_PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2")

    # 手机号：哈希长度固定，密文可解密回原文
    assert len(user.phone_hash) == 64
    assert phone_decrypt(user.phone_cipher) == TEST_PHONE

    # 掩码是脱敏的
    assert user.phone_masked == "138****8000"

    # 明文手机号不能出现在任何一个字段里（密文是二进制，不会被误命中；
    # 这里主要防"有人图省事把手机号直接塞进 nickname 或 remark"）
    for column in (user.nickname, user.phone_masked, user.phone_hash, user.password_hash):
        assert TEST_PHONE not in column


async def test_register_duplicate_phone_rejected(client: AsyncClient) -> None:
    await register(client)
    resp = await client.post("/api/auth/register", json={"phone": TEST_PHONE, "password": TEST_PASSWORD})
    assert resp.status_code == 400
    assert api_code(resp.json()) == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("password", "why"),
    [
        ("short1!", "太短"),
        ("alllowercase", "只有一类字符"),
        ("12345678", "常见弱密码"),
    ],
)
async def test_register_rejects_weak_password(client: AsyncClient, password: str, why: str) -> None:
    resp = await client.post("/api/auth/register", json={"phone": "13900139000", "password": password})
    assert resp.status_code == 400, f"应当拒绝（{why}）: {resp.text}"


async def test_register_rejects_bad_phone(client: AsyncClient) -> None:
    resp = await client.post("/api/auth/register", json={"phone": "12345", "password": TEST_PASSWORD})
    assert resp.status_code == 400


# ============================================================
# 登录
# ============================================================


async def test_login_success(client: AsyncClient) -> None:
    await register(client)
    resp = await client.post("/api/auth/login", json={"phone": TEST_PHONE, "password": TEST_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["data"]["accessToken"]


async def test_wrong_password_and_unknown_account_are_indistinguishable(client: AsyncClient) -> None:
    """★ 不能通过错误信息区分"账号不存在"和"密码错误"，否则可枚举用户。"""
    await register(client)

    wrong_pw = await client.post("/api/auth/login", json={"phone": TEST_PHONE, "password": "WrongPass-123!"})
    unknown = await client.post(
        "/api/auth/login", json={"phone": "13700137000", "password": "WrongPass-123!"}
    )

    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json()["code"] == unknown.json()["code"] == "LOGIN_FAILED"
    assert wrong_pw.json()["message"] == unknown.json()["message"]


async def test_account_locks_after_repeated_failures(client: AsyncClient) -> None:
    await register(client)

    for _ in range(5):
        resp = await client.post("/api/auth/login", json={"phone": TEST_PHONE, "password": "WrongPass-123!"})
        assert resp.status_code == 401

    # 第 6 次即使密码正确也要被拦
    resp = await client.post("/api/auth/login", json={"phone": TEST_PHONE, "password": TEST_PASSWORD})
    assert resp.status_code == 423
    assert api_code(resp.json()) == "ACCOUNT_LOCKED"


async def test_successful_login_resets_failure_counter(client: AsyncClient, session) -> None:
    await register(client)
    for _ in range(3):
        await client.post("/api/auth/login", json={"phone": TEST_PHONE, "password": "WrongPass-123!"})

    # ★ 先确认失败计数**真的落库了** —— 否则下面的"清零"断言是假通过。
    #   登录失败是"写完再抛异常"，如果计数写在会被回滚的事务里，这里就会是 0。
    failed = await session.scalar(
        text("SELECT failed_logins FROM account.user WHERE phone_hash = :h"),
        {"h": phone_hash(TEST_PHONE)},
    )
    assert failed == 3

    await client.post("/api/auth/login", json={"phone": TEST_PHONE, "password": TEST_PASSWORD})

    failed_after = await session.scalar(
        text("SELECT failed_logins FROM account.user WHERE phone_hash = :h"),
        {"h": phone_hash(TEST_PHONE)},
    )
    assert failed_after == 0


# ============================================================
# 令牌
# ============================================================


async def test_refresh_rotates_token(client: AsyncClient) -> None:
    """刷新会轮换 refresh token，旧令牌立即失效。"""
    tokens = await register(client)
    old_refresh = tokens["refreshToken"]

    resp = await client.post("/api/auth/refresh", json={"refreshToken": old_refresh})
    assert resp.status_code == 200
    new_refresh = resp.json()["data"]["refreshToken"]
    assert new_refresh != old_refresh

    # 旧令牌不能再用
    reuse = await client.post("/api/auth/refresh", json={"refreshToken": old_refresh})
    assert reuse.status_code == 401

    # 新令牌可用
    again = await client.post("/api/auth/refresh", json={"refreshToken": new_refresh})
    assert again.status_code == 200


async def test_logout_revokes_refresh_token(client: AsyncClient) -> None:
    tokens = await register(client)
    resp = await client.post("/api/auth/logout", json={"refreshToken": tokens["refreshToken"]})
    assert resp.status_code == 200

    after = await client.post("/api/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    assert after.status_code == 401


async def test_refresh_token_is_stored_hashed(client: AsyncClient, session) -> None:
    """库里存的是哈希，不是令牌原文。"""
    tokens = await register(client)
    row = await session.scalar(select(RefreshToken))
    assert row is not None
    assert row.token_hash != tokens["refreshToken"]
    assert len(row.token_hash) == 64


# ============================================================
# 当前用户
# ============================================================


async def test_me_requires_token(client: AsyncClient) -> None:
    assert (await client.get("/api/me")).status_code == 401


async def test_me_rejects_garbage_token(client: AsyncClient) -> None:
    resp = await client.get("/api/me", headers=auth_header("not-a-real-token"))
    assert resp.status_code == 401


async def test_me_returns_masked_phone(client: AsyncClient) -> None:
    tokens = await register(client)
    resp = await client.get("/api/me", headers=auth_header(tokens["accessToken"]))
    assert resp.status_code == 200

    data = resp.json()["data"]
    assert data["phone"] == "138****8000"
    assert data["role"] == "buyer"
    assert data["shopId"] is None
    # id 必须是字符串（雪花 ID 超过 JS 安全整数范围）
    assert isinstance(data["id"], str)


async def test_update_profile(client: AsyncClient) -> None:
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])

    resp = await client.put("/api/me", json={"nickname": "新昵称", "gender": 1}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["nickname"] == "新昵称"
    assert resp.json()["data"]["gender"] == 1


# ============================================================
# 收货地址
# ============================================================

_ADDRESS = {
    "receiverName": "张三",
    "phone": "13900139000",
    "province": "上海市",
    "city": "上海市",
    "district": "浦东新区",
    "detail": "张江路 1 号",
    "regionCode": "310115",
}


async def test_first_address_becomes_default(client: AsyncClient) -> None:
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])

    resp = await client.post("/api/me/addresses", json=_ADDRESS, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["isDefault"] is True
    # 地址里的手机号也要脱敏返回
    assert resp.json()["data"]["phone"] == "139****9000"


async def test_only_one_default_address(client: AsyncClient) -> None:
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])

    first = (await client.post("/api/me/addresses", json=_ADDRESS, headers=headers)).json()["data"]
    second = (
        await client.post("/api/me/addresses", json={**_ADDRESS, "isDefault": True}, headers=headers)
    ).json()["data"]

    listed = (await client.get("/api/me/addresses", headers=headers)).json()["data"]
    defaults = [a for a in listed if a["isDefault"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == second["id"]
    assert first["id"] != second["id"]


async def test_cannot_touch_another_users_address(client: AsyncClient) -> None:
    """★ 越权：返回 404 而不是 403，避免被用来探测资源是否存在。"""
    alice = await register(client, phone="13800138001")
    bob = await register(client, phone="13800138002")

    addr = (
        await client.post("/api/me/addresses", json=_ADDRESS, headers=auth_header(alice["accessToken"]))
    ).json()["data"]

    bob_headers = auth_header(bob["accessToken"])
    assert (await client.get("/api/me/addresses", headers=bob_headers)).json()["data"] == []

    update = await client.put(f"/api/me/addresses/{addr['id']}", json=_ADDRESS, headers=bob_headers)
    assert update.status_code == 404

    delete = await client.delete(f"/api/me/addresses/{addr['id']}", headers=bob_headers)
    assert delete.status_code == 404


async def test_delete_address_is_soft(client: AsyncClient, session) -> None:
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])
    addr = (await client.post("/api/me/addresses", json=_ADDRESS, headers=headers)).json()["data"]

    assert (await client.delete(f"/api/me/addresses/{addr['id']}", headers=headers)).status_code == 200
    assert (await client.get("/api/me/addresses", headers=headers)).json()["data"] == []

    # 行还在，只是 status = 2（历史订单要能查回当时的地址）
    row = await session.get(UserAddress, int(addr["id"]))
    assert row is not None
    assert row.status == 2


# ============================================================
# 店铺
# ============================================================


async def test_create_shop_upgrades_role_to_merchant(client: AsyncClient) -> None:
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])

    resp = await client.post("/api/merchant/shop", json={"name": "测试旗舰店"}, headers=headers)
    assert resp.status_code == 200
    shop_id = resp.json()["data"]["id"]

    me = (await client.get("/api/me", headers=headers)).json()["data"]
    assert me["role"] == "merchant"
    assert me["shopId"] == shop_id


async def test_cannot_create_second_shop(client: AsyncClient) -> None:
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])
    await client.post("/api/merchant/shop", json={"name": "第一个店"}, headers=headers)

    resp = await client.post("/api/merchant/shop", json={"name": "第二个店"}, headers=headers)
    assert resp.status_code == 400


async def test_new_shop_comes_with_a_default_freight_template(client: AsyncClient) -> None:
    """★ 开店顺手送一条默认运费模板。

    没有它，新店的商品**一件都上不了架**（上架要求每个规格都算得出运费），
    而商家要绕到"发布 → 提交审核 → 平台点通过被判 400"才发现 —— 卡在运营那一步，
    运营只能打回去，白跑一趟。
    """
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])
    await client.post("/api/merchant/shop", json={"name": "新店"}, headers=headers)

    rows = (await client.get("/api/merchant/freight/templates", headers=headers)).json()["data"]
    assert len(rows) == 1
    tpl = rows[0]
    assert tpl["name"] == DEFAULT_TPL_NAME
    assert tpl["isDefault"] is True
    # 参数是"起点"（首重 ¥10 / 满 ¥99 包邮），商家该按真实运费改
    assert tpl["firstPrice"] == DEFAULT_TPL_FIRST_PRICE
    assert tpl["freeThreshold"] == DEFAULT_TPL_FREE_THRESHOLD

    # 带一条「全国默认」区域规则：商家以后想加"上海另计"时不会被校验挡住
    rules = (
        await client.get(f"/api/merchant/freight/templates/{tpl['id']}/regions", headers=headers)
    ).json()["data"]
    assert [r["regionCode"] for r in rules] == ["0"]


async def test_public_shop_info_needs_no_login(client: AsyncClient) -> None:
    """商品详情页要显示店铺，但买家未必登录 —— 这条必须匿名可读。"""
    tokens = await register(client)
    shop = (
        await client.post(
            "/api/merchant/shop",
            json={"name": "演示旗舰店", "description": "只卖好东西"},
            headers=auth_header(tokens["accessToken"]),
        )
    ).json()["data"]

    resp = await client.get(f"/api/shops/{shop['id']}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == "演示旗舰店"
    assert data["description"] == "只卖好东西"
    # ★ 别把店主 ID 这类内部字段漏出去
    assert "ownerUserId" not in data


async def test_public_shop_info_unknown_id_404(client: AsyncClient) -> None:
    resp = await client.get("/api/shops/999999999")
    assert resp.status_code == 404


async def test_batch_public_shops(client: AsyncClient) -> None:
    """商品列表页一次要显示 N 个商品，逐个请求就是 N+1 —— 批量接口是给这个用的。"""
    first = await register(client)
    second = await register(client, phone="13900139099")
    shop_a = (
        await client.post(
            "/api/merchant/shop", json={"name": "A 店"}, headers=auth_header(first["accessToken"])
        )
    ).json()["data"]
    shop_b = (
        await client.post(
            "/api/merchant/shop", json={"name": "B 店"}, headers=auth_header(second["accessToken"])
        )
    ).json()["data"]

    # 重复参数形状：?ids=1&ids=2
    resp = await client.get(f"/api/shops?ids={shop_a['id']}&ids={shop_b['id']}")
    assert resp.status_code == 200, resp.text
    assert {s["name"] for s in resp.json()["data"]} == {"A 店", "B 店"}


async def test_batch_public_shops_without_ids_returns_empty(client: AsyncClient) -> None:
    """★ 不传 ids 要返回空数组，而不是把整张店铺表捞出来。"""
    resp = await client.get("/api/shops")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


# ============================================================
# 店铺设置（改名 / LOGO / 简介）
# ============================================================


async def test_update_shop_settings(client: AsyncClient) -> None:
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])
    await client.post("/api/merchant/shop", json={"name": "旧店名"}, headers=headers)

    resp = await client.put(
        "/api/merchant/shop",
        json={"name": "新店名", "description": "只卖好东西"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["name"] == "新店名"
    assert resp.json()["data"]["description"] == "只卖好东西"

    # 落库：再从"我的店铺"读一次
    shop = (await client.get("/api/merchant/shop", headers=headers)).json()["data"]
    assert shop["name"] == "新店名"

    # 公开接口跟着变 —— 商品详情页/列表卡片显示的就是它
    public = await client.get(f"/api/shops/{shop['id']}")
    assert public.json()["data"]["name"] == "新店名"


async def test_update_shop_is_partial(client: AsyncClient) -> None:
    """★ 只传 name 时，不能顺手把 logo 和简介清掉。"""
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])
    await client.post(
        "/api/merchant/shop",
        json={"name": "老店", "logo": "/media/shops/1/a.webp", "description": "简介"},
        headers=headers,
    )

    resp = await client.put("/api/merchant/shop", json={"name": "改名了"}, headers=headers)
    assert resp.status_code == 200, resp.text

    shop = (await client.get("/api/merchant/shop", headers=headers)).json()["data"]
    assert shop["name"] == "改名了"
    assert shop["logo"] == "/media/shops/1/a.webp"
    assert shop["description"] == "简介"


async def test_update_shop_can_clear_optional_fields(client: AsyncClient) -> None:
    """★ 显式传 null 是**清空**，跟"没传"不是一回事 —— 这正是 model_fields_set 的意义。"""
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])
    await client.post(
        "/api/merchant/shop",
        json={"name": "老店", "logo": "/media/shops/1/a.webp", "description": "简介"},
        headers=headers,
    )

    resp = await client.put(
        "/api/merchant/shop", json={"logo": None, "description": None}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    shop = (await client.get("/api/merchant/shop", headers=headers)).json()["data"]
    assert shop["logo"] is None
    assert shop["description"] is None
    # 没传 name，名字不动
    assert shop["name"] == "老店"


async def test_update_shop_without_shop_404(client: AsyncClient) -> None:
    tokens = await register(client)
    resp = await client.put(
        "/api/merchant/shop", json={"name": "新店名"}, headers=auth_header(tokens["accessToken"])
    )
    assert resp.status_code == 404


async def test_update_shop_rejects_short_name(client: AsyncClient) -> None:
    tokens = await register(client)
    headers = auth_header(tokens["accessToken"])
    await client.post("/api/merchant/shop", json={"name": "正常店名"}, headers=headers)

    resp = await client.put("/api/merchant/shop", json={"name": "x"}, headers=headers)
    assert resp.status_code == 400


async def test_update_shop_requires_login(client: AsyncClient) -> None:
    assert (await client.put("/api/merchant/shop", json={"name": "新店名"})).status_code == 401


async def test_platform_role_cannot_open_shop(client: AsyncClient, session) -> None:
    """★ 平台账号不开店：接口直接 403，不只是前端藏了按钮。

    两道理由：商家菜单本来就按店铺归属分（运营不该有店铺）；运营在自己的平台上
    卖货是利益冲突（商品审核也只放给 admin）。只挡前端等于接口还开着。
    """
    admin = await make_admin(client, session, phone="13900139081")
    denied = await client.post(
        "/api/merchant/shop",
        json={"name": "运营的店"},
        headers=auth_header(admin["accessToken"]),
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "FORBIDDEN"

    # 反证：买家开店照常成功 —— 上面挡的是角色，不是这条路整个坏了
    buyer = await register(client, phone="13800138081")
    ok = await client.post(
        "/api/merchant/shop",
        json={"name": "正常小店"},
        headers=auth_header(buyer["accessToken"]),
    )
    assert ok.status_code == 200, ok.text
