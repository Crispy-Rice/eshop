"""product 模块的集成测试。连真实 PostgreSQL + Redis。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import auth_header, make_admin, open_shop, register

# ============================================================
# 测试数据构造
# ============================================================


async def _make_category(
    client: AsyncClient, admin_token: str, name: str, parent_id: str | None = None
) -> str:
    resp = await client.post(
        "/api/admin/categories",
        json={"name": name, "parentId": parent_id},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def _setup_merchant(client: AsyncClient, session) -> tuple[dict, str, str]:
    """造好：管理员 + 类目树 + 商家 + 店铺。返回 (商家 token, 末级类目 id, 一级类目 id)。"""
    admin = await make_admin(client, session)
    level1 = await _make_category(client, admin["accessToken"], "数码")
    level2 = await _make_category(client, admin["accessToken"], "手机", parent_id=level1)
    level3 = await _make_category(client, admin["accessToken"], "智能手机", parent_id=level2)

    merchant = await register(client, phone="13800138010")
    await open_shop(client, merchant["accessToken"])
    return merchant, level3, level1


def _spu_payload(category_id: str) -> dict:
    """两个规格组 × 两个取值，但只上架 3 个 SKU。

    "原色钛 + 512G" 故意不做 —— 这就是 docs/02 §3 说的"无效组合"，
    前端规格选择器必须能让它点不出来。
    """
    return {
        "categoryId": category_id,
        "title": "iPhone 16 Pro",
        "subTitle": "A18 Pro 芯片",
        "mainImage": "/media/ip16.webp",
        "specGroups": [
            {
                "name": "颜色",
                "values": [
                    {"key": "black", "value": "暗夜黑"},
                    {"key": "natural", "value": "原色钛"},
                ],
            },
            {
                "name": "容量",
                "values": [
                    {"key": "256", "value": "256G"},
                    {"key": "512", "value": "512G"},
                ],
            },
        ],
        "skus": [
            {
                "skuCode": "IP16-B-256",
                "specValueKeys": ["black", "256"],
                "price": 799900,
                "coverImage": "/media/1.webp",
                "weightG": 199,
            },
            {
                "skuCode": "IP16-B-512",
                "specValueKeys": ["black", "512"],
                "price": 899900,
                "coverImage": "/media/2.webp",
                "weightG": 199,
            },
            {
                "skuCode": "IP16-N-256",
                "specValueKeys": ["natural", "256"],
                "price": 699900,
                "coverImage": "/media/3.webp",
                "weightG": 199,
            },
        ],
    }


async def _create_and_publish(client: AsyncClient, session) -> tuple[str, dict]:
    """完整走一遍：建类目 → 发布商品 → 提交审核 → 平台通过。

    返回 (spu_id, 商家 token)。
    """
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")

    merchant = await register(client, phone="13800138010")
    await open_shop(client, merchant["accessToken"])
    merchant_headers = auth_header(merchant["accessToken"])

    resp = await client.post("/api/merchant/spus", json=_spu_payload(category), headers=merchant_headers)
    assert resp.status_code == 200, resp.text
    spu_id = resp.json()["data"]["id"]

    assert (
        await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=merchant_headers)
    ).status_code == 200
    assert (
        await client.post(
            f"/api/admin/spus/{spu_id}/audit",
            json={"approved": True},
            headers=auth_header(admin["accessToken"]),
        )
    ).status_code == 200

    return spu_id, merchant


# ============================================================
# 类目
# ============================================================


async def test_admin_creates_category_tree(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    level1 = await _make_category(client, admin["accessToken"], "数码")
    level2 = await _make_category(client, admin["accessToken"], "手机", parent_id=level1)

    tree = (await client.get("/api/categories")).json()["data"]
    assert len(tree) == 1
    assert tree[0]["name"] == "数码"
    assert tree[0]["level"] == 1
    assert tree[0]["children"][0]["name"] == "手机"
    assert tree[0]["children"][0]["level"] == 2
    assert tree[0]["children"][0]["parentId"] == level1
    assert tree[0]["children"][0]["id"] == level2


async def test_non_admin_cannot_create_category(client: AsyncClient) -> None:
    buyer = await register(client)
    resp = await client.post(
        "/api/admin/categories", json={"name": "违规类目"}, headers=auth_header(buyer["accessToken"])
    )
    assert resp.status_code == 403


async def test_duplicate_category_name_rejected(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    await _make_category(client, admin["accessToken"], "数码")
    resp = await client.post(
        "/api/admin/categories", json={"name": "数码"}, headers=auth_header(admin["accessToken"])
    )
    assert resp.status_code == 400


async def _db_category(session, category_id: str) -> dict:
    """直接读库拿 path/level。

    接口出参里没有这两个字段，而"移动"的正确性几乎全在 path 上（子孙是不是
    跟着重写了），只断言接口返回是看不出来的。
    """
    row = (
        await session.execute(
            text("SELECT parent_id, level, path FROM product.category WHERE id = :cid"),
            {"cid": int(category_id)},
        )
    ).one()
    return {
        "parent_id": None if row[0] is None else str(row[0]),
        "level": row[1],
        "path": row[2],
    }


async def _admin_tree(client: AsyncClient, token: str) -> list[dict]:
    resp = await client.get("/api/admin/categories", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------- 改名 / 排序 / 启停 ----------


async def test_update_category_rename_and_sort(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "数码")

    resp = await client.put(
        f"/api/admin/categories/{category}",
        json={"name": "数码产品", "sort": 5},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["name"] == "数码产品"
    assert resp.json()["data"]["sort"] == 5


async def test_rename_to_own_name_is_ok(client: AsyncClient, session) -> None:
    """★ 改回自己现在的名字不该被自己那一行判成重名（所以需要 exclude_id）。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "数码")
    resp = await client.put(
        f"/api/admin/categories/{category}",
        json={"name": "数码"},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200, resp.text


async def test_rename_to_sibling_name_rejected(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    await _make_category(client, admin["accessToken"], "数码")
    other = await _make_category(client, admin["accessToken"], "家电")

    resp = await client.put(f"/api/admin/categories/{other}", json={"name": "数码"}, headers=headers)
    assert resp.status_code == 400
    assert "同名" in resp.json()["message"]
    # ★ 必须是预检挡下来的，不能是撞了唯一约束 —— 那会中断整个事务
    assert [c["name"] for c in (await client.get("/api/categories")).json()["data"]] == ["数码", "家电"]


async def test_sort_changes_tree_order(client: AsyncClient, session) -> None:
    """★ 锁住"sort 是假的"这个缺陷。

    原来 ORDER BY 把 path 放在最前，而兄弟节点的 path 各自含自己的 id，
    等价于按 id 排 —— 运营改了 sort，顺序纹丝不动。
    """
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    jia = await _make_category(client, admin["accessToken"], "甲")
    yi = await _make_category(client, admin["accessToken"], "乙")

    # 都是 sort=0，按 id 排，"甲"在前
    assert [c["name"] for c in (await client.get("/api/categories")).json()["data"]] == ["甲", "乙"]

    assert (await client.put(f"/api/admin/categories/{jia}", json={"sort": 2}, headers=headers)).status_code == 200
    assert (await client.put(f"/api/admin/categories/{yi}", json={"sort": 1}, headers=headers)).status_code == 200

    assert [c["name"] for c in (await client.get("/api/categories")).json()["data"]] == ["乙", "甲"], (
        "改了 sort 顺序必须跟着变"
    )


async def test_disable_category_hides_it_from_public_tree(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    parent = await _make_category(client, admin["accessToken"], "数码")
    leaf = await _make_category(client, admin["accessToken"], "手机", parent_id=parent)

    resp = await client.put(f"/api/admin/categories/{leaf}", json={"status": 2}, headers=headers)
    assert resp.status_code == 200, resp.text

    tree = (await client.get("/api/categories")).json()["data"]
    assert [c["name"] for c in tree] == ["数码"]
    assert tree[0]["children"] == [], "停用的类目不该出现在公开树里"

    # 管理端树仍然看得见它，而且带着状态
    node = (await _admin_tree(client, admin["accessToken"]))[0]["children"][0]
    assert node["name"] == "手机"
    assert node["status"] == 2


async def test_disable_parent_with_active_child_rejected(client: AsyncClient, session) -> None:
    """★ 停用父类目会让子类目在公开树里"冒"成一级（父不在树里就归入 roots）。"""
    admin = await make_admin(client, session)
    parent = await _make_category(client, admin["accessToken"], "数码")
    await _make_category(client, admin["accessToken"], "手机", parent_id=parent)

    resp = await client.put(
        f"/api/admin/categories/{parent}", json={"status": 2}, headers=auth_header(admin["accessToken"])
    )
    assert resp.status_code == 400
    assert "子类目" in resp.json()["message"]


async def test_disabled_category_cannot_take_new_spu(client: AsyncClient, session) -> None:
    """停用后商家选它发商品要报"类目不存在或已停用"。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")
    assert (
        await client.put(
            f"/api/admin/categories/{category}",
            json={"status": 2},
            headers=auth_header(admin["accessToken"]),
        )
    ).status_code == 200

    merchant = await register(client, phone="13800138012")
    await open_shop(client, merchant["accessToken"])
    resp = await client.post(
        "/api/merchant/spus",
        json=_spu_payload(category),
        headers=auth_header(merchant["accessToken"]),
    )
    assert resp.status_code == 400
    assert "停用" in resp.json()["message"]


# ---------- 移动 ----------


async def test_move_leaf_between_parents(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    a = await _make_category(client, admin["accessToken"], "数码")
    b = await _make_category(client, admin["accessToken"], "家电")
    leaf = await _make_category(client, admin["accessToken"], "手机", parent_id=a)

    resp = await client.post(f"/api/admin/categories/{leaf}/move", json={"parentId": b}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["parentId"] == b
    assert resp.json()["data"]["level"] == 2

    row = await _db_category(session, leaf)
    assert row["parent_id"] == b
    assert row["path"] == f"/{b}/{leaf}/"


async def test_move_rewrites_whole_subtree(client: AsyncClient, session) -> None:
    """★ 核心用例：移动带子树的类目，子孙的 path 前缀和 level 都得跟着重写。"""
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    a = await _make_category(client, admin["accessToken"], "数码")
    b = await _make_category(client, admin["accessToken"], "家电")
    mid = await _make_category(client, admin["accessToken"], "手机", parent_id=a)
    leaf = await _make_category(client, admin["accessToken"], "智能手机", parent_id=mid)
    assert (await _db_category(session, leaf))["path"] == f"/{a}/{mid}/{leaf}/"

    resp = await client.post(f"/api/admin/categories/{mid}/move", json={"parentId": b}, headers=headers)
    assert resp.status_code == 200, resp.text

    assert (await _db_category(session, mid))["path"] == f"/{b}/{mid}/"
    moved = await _db_category(session, leaf)
    assert moved["path"] == f"/{b}/{mid}/{leaf}/", "子孙的 path 前缀必须跟着换"
    assert moved["level"] == 3
    assert moved["parent_id"] == mid, "子孙的父子关系本身没变"

    roots = {c["name"]: c for c in (await client.get("/api/categories")).json()["data"]}
    assert roots["数码"]["children"] == []
    assert roots["家电"]["children"][0]["name"] == "手机"
    assert roots["家电"]["children"][0]["children"][0]["name"] == "智能手机"


async def test_move_into_own_descendant_rejected(client: AsyncClient, session) -> None:
    """环：把类目移到它自己或它的后代下必须挡住，否则子树会挂到自己底下。"""
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    a = await _make_category(client, admin["accessToken"], "数码")
    mid = await _make_category(client, admin["accessToken"], "手机", parent_id=a)
    leaf = await _make_category(client, admin["accessToken"], "智能手机", parent_id=mid)

    for target in (a, mid, leaf):
        resp = await client.post(f"/api/admin/categories/{a}/move", json={"parentId": target}, headers=headers)
        assert resp.status_code == 400, f"移到 {target} 应当被拒"

    assert (await _db_category(session, mid))["path"] == f"/{a}/{mid}/", "被拒后树不能被动过"


async def test_move_exceeding_depth_rejected(client: AsyncClient, session) -> None:
    """深了要 400，而不是撞 level 的 CHECK 约束变成 500。"""
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    a = await _make_category(client, admin["accessToken"], "数码")
    b = await _make_category(client, admin["accessToken"], "家电")
    b2 = await _make_category(client, admin["accessToken"], "电视", parent_id=b)  # level 2
    mid = await _make_category(client, admin["accessToken"], "手机", parent_id=a)  # level 2
    await _make_category(client, admin["accessToken"], "智能手机", parent_id=mid)  # level 3

    # mid 子树最深 3 级，挂到 level 2 的 b2 下 → 最深会变成 4
    resp = await client.post(f"/api/admin/categories/{mid}/move", json={"parentId": b2}, headers=headers)
    assert resp.status_code == 400
    assert "层级" in resp.json()["message"]
    assert (await _db_category(session, mid))["parent_id"] == a, "失败后不能留下半个移动"


async def test_move_to_same_parent_is_noop(client: AsyncClient, session) -> None:
    """移到当前父节点：幂等成功 —— 重名预检不能把自己算进去。"""
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    a = await _make_category(client, admin["accessToken"], "数码")
    mid = await _make_category(client, admin["accessToken"], "手机", parent_id=a)

    resp = await client.post(f"/api/admin/categories/{mid}/move", json={"parentId": a}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert (await _db_category(session, mid))["path"] == f"/{a}/{mid}/"


async def test_move_to_name_conflict_rejected(client: AsyncClient, session) -> None:
    """不同父下同名是合法的，但移动后就会撞车 —— 同样要预检。"""
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    a = await _make_category(client, admin["accessToken"], "数码")
    b = await _make_category(client, admin["accessToken"], "家电")
    await _make_category(client, admin["accessToken"], "手机", parent_id=a)
    moveme = await _make_category(client, admin["accessToken"], "手机", parent_id=b)

    resp = await client.post(f"/api/admin/categories/{moveme}/move", json={"parentId": a}, headers=headers)
    assert resp.status_code == 400
    assert "同名" in resp.json()["message"]


async def test_move_to_root(client: AsyncClient, session) -> None:
    """parentId 传 null = 升为一级类目。"""
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    a = await _make_category(client, admin["accessToken"], "数码")
    mid = await _make_category(client, admin["accessToken"], "手机", parent_id=a)

    resp = await client.post(f"/api/admin/categories/{mid}/move", json={"parentId": None}, headers=headers)
    assert resp.status_code == 200, resp.text

    row = await _db_category(session, mid)
    assert row["parent_id"] is None
    assert row["level"] == 1
    assert row["path"] == f"/{mid}/"


# ---------- 删除 ----------


async def test_delete_leaf_category(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    headers = auth_header(admin["accessToken"])
    category = await _make_category(client, admin["accessToken"], "数码")

    resp = await client.delete(f"/api/admin/categories/{category}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert (await client.get("/api/categories")).json()["data"] == []


async def test_delete_category_with_children_rejected(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    parent = await _make_category(client, admin["accessToken"], "数码")
    await _make_category(client, admin["accessToken"], "手机", parent_id=parent)

    resp = await client.delete(
        f"/api/admin/categories/{parent}", headers=auth_header(admin["accessToken"])
    )
    assert resp.status_code == 400
    assert "子类目" in resp.json()["message"]
    assert len((await client.get("/api/categories")).json()["data"]) == 1


async def test_delete_category_with_spu_rejected(client: AsyncClient, session) -> None:
    """★ spu.category_id 没有外键，删了不会级联也不会被数据库拦住 —— 必须应用层挡。"""
    admin = await make_admin(client, session)
    admin_headers = auth_header(admin["accessToken"])
    category = await _make_category(client, admin["accessToken"], "智能手机")

    merchant = await register(client, phone="13800138013")
    await open_shop(client, merchant["accessToken"])
    assert (
        await client.post(
            "/api/merchant/spus",
            json=_spu_payload(category),
            headers=auth_header(merchant["accessToken"]),
        )
    ).status_code == 200

    resp = await client.delete(f"/api/admin/categories/{category}", headers=admin_headers)
    assert resp.status_code == 400
    assert "1 件商品" in resp.json()["message"]


async def test_non_admin_cannot_mutate_categories(client: AsyncClient) -> None:
    buyer = await register(client)
    headers = auth_header(buyer["accessToken"])
    assert (await client.get("/api/admin/categories", headers=headers)).status_code == 403
    assert (
        await client.put("/api/admin/categories/1", json={"name": "x"}, headers=headers)
    ).status_code == 403
    assert (await client.post("/api/admin/categories/1/move", json={}, headers=headers)).status_code == 403
    assert (await client.delete("/api/admin/categories/1", headers=headers)).status_code == 403


async def test_admin_tree_reports_spu_count(client: AsyncClient, session) -> None:
    """管理端树带商品数 —— 删除被挡住时运营一眼能看到原因。"""
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "智能手机")

    assert (await _admin_tree(client, admin["accessToken"]))[0]["spuCount"] == 0

    merchant = await register(client, phone="13800138014")
    await open_shop(client, merchant["accessToken"])
    assert (
        await client.post(
            "/api/merchant/spus",
            json=_spu_payload(category),
            headers=auth_header(merchant["accessToken"]),
        )
    ).status_code == 200

    node = (await _admin_tree(client, admin["accessToken"]))[0]
    assert node["spuCount"] == 1
    assert node["status"] == 1


# ============================================================
# 发布商品
# ============================================================


async def test_create_spu_persists_specs_and_skus(client: AsyncClient, session) -> None:
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])

    resp = await client.post("/api/merchant/spus", json=_spu_payload(category), headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["specGroups"][0]["name"] == "颜色"
    assert {v["value"] for v in data["specGroups"][0]["values"]} == {"暗夜黑", "原色钛"}
    assert len(data["skus"]) == 3

    # spec_text 按规格组顺序拼，格式统一
    by_code = {s["skuCode"]: s for s in data["skus"]}
    assert by_code["IP16-B-256"]["specText"] == "暗夜黑;256G"
    assert by_code["IP16-B-512"]["specText"] == "暗夜黑;512G"
    assert by_code["IP16-N-256"]["specText"] == "原色钛;256G"


async def test_spu_detail_provides_spec_selector_data(client: AsyncClient, session) -> None:
    """详情接口要能支撑前端推导"哪些规格组合可选"（docs/02 §3）。"""
    merchant, category, _ = await _setup_merchant(client, session)
    created = (
        await client.post(
            "/api/merchant/spus", json=_spu_payload(category), headers=auth_header(merchant["accessToken"])
        )
    ).json()["data"]

    value_id = {v["value"]: v["id"] for g in created["specGroups"] for v in g["values"]}
    combos = {frozenset(s["specValueIds"]) for s in created["skus"]}

    black, natural = value_id["暗夜黑"], value_id["原色钛"]
    c256, c512 = value_id["256G"], value_id["512G"]

    assert frozenset({black, c256}) in combos
    assert frozenset({black, c512}) in combos
    assert frozenset({natural, c256}) in combos
    # ★ 原色钛 + 512G 不存在，前端应当把这个组合置灰
    assert frozenset({natural, c512}) not in combos


async def test_price_range_is_computed_from_skus(client: AsyncClient, session) -> None:
    merchant, category, _ = await _setup_merchant(client, session)
    data = (
        await client.post(
            "/api/merchant/spus", json=_spu_payload(category), headers=auth_header(merchant["accessToken"])
        )
    ).json()["data"]

    prices = [s["price"] for s in data["skus"]]
    assert data["priceMin"] == min(prices) == 699900
    assert data["priceMax"] == max(prices) == 899900


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda p: p["skus"][0].update(specValueKeys=["black", "256", "512"]), "选多了一个规格组"),
        (lambda p: p["skus"][0].update(specValueKeys=["black"]), "少选了规格组"),
        (lambda p: p["skus"][0].update(specValueKeys=["black", "nope"]), "引用了不存在的 key"),
        (lambda p: p["skus"][1].update(specValueKeys=["black", "256"]), "两个 SKU 规格组合重复"),
        (lambda p: p["skus"][1].update(skuCode="IP16-B-256"), "商家编码重复"),
        (lambda p: p["specGroups"][1]["values"][0].update(key="black"), "规格值 key 跨组重复"),
    ],
)
async def test_create_spu_rejects_invalid_specs(client: AsyncClient, session, mutate, why: str) -> None:
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])

    payload = _spu_payload(category)
    mutate(payload)

    resp = await client.post("/api/merchant/spus", json=payload, headers=headers)
    assert resp.status_code == 400, f"应当拒绝（{why}）: {resp.text}"


async def test_create_spu_requires_leaf_category(client: AsyncClient, session) -> None:
    merchant, _leaf, level1 = await _setup_merchant(client, session)
    resp = await client.post(
        "/api/merchant/spus", json=_spu_payload(level1), headers=auth_header(merchant["accessToken"])
    )
    assert resp.status_code == 400
    assert "末级类目" in resp.json()["message"]


async def test_create_spu_rejects_negative_price(client: AsyncClient, session) -> None:
    merchant, category, _ = await _setup_merchant(client, session)
    payload = _spu_payload(category)
    payload["skus"][0]["price"] = -1

    resp = await client.post("/api/merchant/spus", json=payload, headers=auth_header(merchant["accessToken"]))
    assert resp.status_code == 400


async def test_creating_product_requires_a_shop(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "手机")
    buyer = await register(client, phone="13800138099")

    resp = await client.post(
        "/api/merchant/spus", json=_spu_payload(category), headers=auth_header(buyer["accessToken"])
    )
    assert resp.status_code == 403


# ============================================================
# 上下架与可见性
# ============================================================


async def test_draft_is_invisible_to_buyers(client: AsyncClient, session) -> None:
    merchant, category, _ = await _setup_merchant(client, session)
    spu_id = (
        await client.post(
            "/api/merchant/spus", json=_spu_payload(category), headers=auth_header(merchant["accessToken"])
        )
    ).json()["data"]["id"]

    # 买家看不到草稿
    assert (await client.get(f"/api/spus/{spu_id}")).status_code == 404
    # 商家自己看得到
    assert (
        await client.get(f"/api/merchant/spus/{spu_id}", headers=auth_header(merchant["accessToken"]))
    ).status_code == 200


async def test_publish_flow_makes_product_visible(client: AsyncClient, session) -> None:
    spu_id, _merchant = await _create_and_publish(client, session)

    detail = await client.get(f"/api/spus/{spu_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == 2

    # 默认搜索里能搜到
    search = await client.get("/api/search", params={"keyword": "iPhone"})
    assert [i["id"] for i in search.json()["data"]["items"]] == [spu_id]


async def test_sku_code_is_optional(client: AsyncClient, session) -> None:
    """商家编码是选填的：不传 / 传空串 / 传纯空白，都算"没填"，都能发布。

    ★ 这条盯的是**空值存 NULL 而不是空串**。唯一约束 ``(spu_id, sku_code)`` 里
      PostgreSQL 认为每个 NULL 互不相同，所以同一个商品下能有多个没编码的 SKU；
      写入侧要是把空值归一成空串，第二个 SKU 就会撞约束。
    """
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "手机")
    merchant = await register(client, phone="13800138021")
    await open_shop(client, merchant["accessToken"])
    headers = auth_header(merchant["accessToken"])

    payload = _spu_payload(category)
    payload["title"] = "无编码商品"
    for sku, variant in zip(payload["skus"], (None, "", "   "), strict=True):
        if variant is None:
            sku.pop("skuCode")  # 完全不传
        else:
            sku["skuCode"] = variant

    resp = await client.post("/api/merchant/spus", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text

    detail = resp.json()["data"]
    assert len(detail["skus"]) == 3
    # 读侧统一成空串（不是 null）：消费方不必逐个处理 None
    assert all(s["skuCode"] == "" for s in detail["skus"])


async def test_off_shelf_hides_from_search(client: AsyncClient, session) -> None:
    spu_id, merchant = await _create_and_publish(client, session)
    headers = auth_header(merchant["accessToken"])

    assert (await client.post(f"/api/merchant/spus/{spu_id}/off-shelf", headers=headers)).status_code == 200

    assert (await client.get(f"/api/spus/{spu_id}")).status_code == 404
    search = await client.get("/api/search", params={"keyword": "iPhone"})
    assert search.json()["data"]["items"] == []


async def test_merchant_cannot_touch_another_shops_product(client: AsyncClient, session) -> None:
    spu_id, _merchant = await _create_and_publish(client, session)

    intruder = await register(client, phone="13800138020")
    await open_shop(client, intruder["accessToken"], name="别人的店")
    headers = auth_header(intruder["accessToken"])

    # 一律 404，不区分"不存在"和"不是你的"
    assert (await client.get(f"/api/merchant/spus/{spu_id}", headers=headers)).status_code == 404
    assert (await client.post(f"/api/merchant/spus/{spu_id}/off-shelf", headers=headers)).status_code == 404
    assert (
        await client.put(f"/api/merchant/spus/{spu_id}", json={"title": "改个名"}, headers=headers)
    ).status_code == 404


# ============================================================
# 搜索
# ============================================================


async def test_search_matches_title_and_spec_values(client: AsyncClient, session) -> None:
    await _create_and_publish(client, session)

    # 标题命中
    assert len((await client.get("/api/search", params={"keyword": "iPhone"})).json()["data"]["items"]) == 1
    # 规格值命中（search_text 里含"暗夜黑 256G"等）
    assert len((await client.get("/api/search", params={"keyword": "256G"})).json()["data"]["items"]) == 1
    # 类目名命中
    assert len((await client.get("/api/search", params={"keyword": "智能手机"})).json()["data"]["items"]) == 1
    # 多个词是 AND 语义
    assert (
        len((await client.get("/api/search", params={"keyword": "iPhone 256G"})).json()["data"]["items"]) == 1
    )
    assert (
        len(
            (await client.get("/api/search", params={"keyword": "iPhone 不存在的词"})).json()["data"]["items"]
        )
        == 0
    )


async def test_search_price_range_uses_interval_overlap(client: AsyncClient, session) -> None:
    """价格区间与商品的 [priceMin, priceMax] 相交即命中。"""
    await _create_and_publish(client, session)  # 区间 [699900, 899900]

    hit_high = await client.get("/api/search", params={"priceFrom": 800000})
    assert len(hit_high.json()["data"]["items"]) == 1  # 与上限 899900 相交

    miss_high = await client.get("/api/search", params={"priceFrom": 900000})
    assert miss_high.json()["data"]["items"] == []

    hit_low = await client.get("/api/search", params={"priceTo": 700000})
    assert len(hit_low.json()["data"]["items"]) == 1  # 与下限 699900 相交


async def test_search_by_parent_category_includes_descendants(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    level1 = await _make_category(client, admin["accessToken"], "数码")
    level2 = await _make_category(client, admin["accessToken"], "手机", parent_id=level1)

    merchant = await register(client, phone="13800138010")
    await open_shop(client, merchant["accessToken"])
    spu_id = (
        await client.post(
            "/api/merchant/spus",
            json=_spu_payload(level2),
            headers=auth_header(merchant["accessToken"]),
        )
    ).json()["data"]["id"]

    # 直接提交并上架（走审核流程在别的用例里已覆盖）
    headers = auth_header(merchant["accessToken"])
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=headers)
    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": True},
        headers=auth_header(admin["accessToken"]),
    )

    # 用一级类目搜，要能搜到挂在二级类目下的商品
    resp = await client.get("/api/search", params={"categoryId": level1})
    assert [i["id"] for i in resp.json()["data"]["items"]] == [spu_id]


async def test_search_keyword_escaping(client: AsyncClient, session) -> None:
    """用户输入的 % 和 _ 必须被转义，否则会变成通配符把全表带出来。"""
    await _create_and_publish(client, session)

    resp = await client.get("/api/search", params={"keyword": "%"})
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []


async def test_search_cursor_pagination(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "手机")
    merchant = await register(client, phone="13800138010")
    await open_shop(client, merchant["accessToken"])
    headers = auth_header(merchant["accessToken"])

    spu_ids = []
    for index in range(3):
        payload = _spu_payload(category)
        payload["title"] = f"商品{index}"
        # 每个商品的 SKU 编码要不同
        for sku_index, sku in enumerate(payload["skus"]):
            sku["skuCode"] = f"P{index}-{sku_index}"
        created = (await client.post("/api/merchant/spus", json=payload, headers=headers)).json()["data"]
        await client.post(f"/api/merchant/spus/{created['id']}/submit", headers=headers)
        await client.post(
            f"/api/admin/spus/{created['id']}/audit",
            json={"approved": True},
            headers=auth_header(admin["accessToken"]),
        )
        spu_ids.append(created["id"])

    first = (await client.get("/api/search", params={"keyword": "商品", "limit": 2})).json()["data"]
    assert len(first["items"]) == 2
    assert first["hasMore"] is True
    assert first["nextCursor"]
    # ★ 总数**只在第一页**算：商城要显示"共 N 件"，但没必要每翻一页都重数一遍
    assert first["total"] == 3

    second = (
        await client.get("/api/search", params={"keyword": "商品", "limit": 2, "cursor": first["nextCursor"]})
    ).json()["data"]
    assert len(second["items"]) == 1
    assert second["hasMore"] is False
    assert second["total"] is None, "翻页响应不该再带总数"

    # 两页合起来正好是全部 3 个，且没有重复
    seen = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert sorted(seen) == sorted(spu_ids)

    # 商家列表没有"共 N 件商品"这个文案，不该替它白跑一次 COUNT
    mine = (await client.get("/api/merchant/spus", headers=headers)).json()["data"]
    assert mine["total"] is None


async def test_search_total_is_same_scope_as_list(client: AsyncClient, session) -> None:
    """★ "共 N 件"的 N 必须和列表**同口径** —— 两边共用 ``_spu_filters``。

    价格过滤是最容易写成两套条件的：列表按「区间相交」判断
    （``price_max >= from AND price_min <= to``），计数要是顺手写成
    「价格落在区间内」，区间价商品上就会对不上数。
    """
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "手机")
    merchant = await register(client, phone="13800138011")
    await open_shop(client, merchant["accessToken"])
    headers = auth_header(merchant["accessToken"])

    # 三件商品，**第三件价格有跨度**（SKU 从 3000 到 7000 元）。
    #   ★ 必须有跨度才分得出两种口径：单价商品 min == max，怎么写都一样，
    #     拿它测等于没测（第一版就是这么写的，把计数故意写错也没变红）。
    prices_per_spu = [
        (100000, 100000, 100000),  # 1000 元
        (500000, 500000, 500000),  # 5000 元
        (300000, 700000, 700000),  # 3000~7000 元（有跨度）
    ]
    for index, prices in enumerate(prices_per_spu):
        payload = _spu_payload(category)
        payload["title"] = f"价格商品{index}"
        for sku_index, sku in enumerate(payload["skus"]):
            sku["skuCode"] = f"PR{index}-{sku_index}"
            sku["price"] = prices[sku_index]
        created = (await client.post("/api/merchant/spus", json=payload, headers=headers)).json()["data"]
        await client.post(f"/api/merchant/spus/{created['id']}/submit", headers=headers)
        await client.post(
            f"/api/admin/spus/{created['id']}/audit",
            json={"approved": True},
            headers=auth_header(admin["accessToken"]),
        )

    data = (await client.get("/api/search", params={"keyword": "价格商品", "limit": 50})).json()["data"]
    assert data["total"] == len(data["items"]), "总数必须和列表条数对得上"
    assert data["total"] == 3

    # 带价格过滤时同样要对得上。判据是**区间相交**：5000 那件和 3000~7000 那件
    # 都跨过 4000，所以是 2 件。若计数误用"价格落在区间内"，3000~7000 那件会被漏掉。
    filtered = (
        await client.get("/api/search", params={"keyword": "价格商品", "priceFrom": 400000, "limit": 50})
    ).json()["data"]
    assert filtered["total"] == 2
    assert filtered["total"] == len(filtered["items"])


async def test_invalid_cursor_rejected(client: AsyncClient) -> None:
    resp = await client.get("/api/search", params={"cursor": "not-a-valid-cursor"})
    assert resp.status_code == 400


# ============================================================
# SKU 批量查询与改价
# ============================================================


async def test_batch_skus_excludes_unpublished(client: AsyncClient, session) -> None:
    spu_id, merchant = await _create_and_publish(client, session)
    detail = (await client.get(f"/api/spus/{spu_id}")).json()["data"]
    sku_ids = [s["id"] for s in detail["skus"]]

    resp = await client.post("/api/skus/batch", json={"skuIds": sku_ids})
    assert len(resp.json()["data"]) == 3
    assert resp.json()["data"][0]["title"] == "iPhone 16 Pro"

    # 下架后批量查询不再返回（购物车会据此提示用户商品已失效）
    await client.post(f"/api/merchant/spus/{spu_id}/off-shelf", headers=auth_header(merchant["accessToken"]))
    assert (await client.post("/api/skus/batch", json={"skuIds": sku_ids})).json()["data"] == []


async def test_update_sku_price_refreshes_spu_range(client: AsyncClient, session) -> None:
    spu_id, merchant = await _create_and_publish(client, session)
    headers = auth_header(merchant["accessToken"])

    detail = (await client.get(f"/api/spus/{spu_id}")).json()["data"]
    cheapest = min(detail["skus"], key=lambda s: s["price"])

    resp = await client.put(f"/api/merchant/skus/{cheapest['id']}", json={"price": 199900}, headers=headers)
    assert resp.status_code == 200

    updated = (await client.get(f"/api/merchant/spus/{spu_id}", headers=headers)).json()["data"]
    assert updated["priceMin"] == 199900
    assert updated["priceMax"] == 899900


async def test_update_spu_title_rebuilds_search_text(client: AsyncClient, session) -> None:
    spu_id, merchant = await _create_and_publish(client, session)
    headers = auth_header(merchant["accessToken"])

    await client.put(f"/api/merchant/spus/{spu_id}", json={"title": "小米 15 Ultra"}, headers=headers)

    assert (await client.get("/api/search", params={"keyword": "iPhone"})).json()["data"]["items"] == []
    assert len((await client.get("/api/search", params={"keyword": "小米"})).json()["data"]["items"]) == 1


# ============================================================
# 审核
# ============================================================


async def test_audit_rejection_marks_product_rejected(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "手机")
    merchant = await register(client, phone="13800138010")
    await open_shop(client, merchant["accessToken"])
    headers = auth_header(merchant["accessToken"])

    spu_id = (await client.post("/api/merchant/spus", json=_spu_payload(category), headers=headers)).json()[
        "data"
    ]["id"]
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=headers)

    resp = await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": False, "remark": "主图不合规"},
        headers=auth_header(admin["accessToken"]),
    )
    assert resp.status_code == 200

    detail = (await client.get(f"/api/merchant/spus/{spu_id}", headers=headers)).json()["data"]
    # ★ 6 而不是 1：被驳回要和"从没提交过的草稿"分开，否则商家列表里两种货色长得一样
    assert detail["status"] == 6
    assert (await client.get(f"/api/spus/{spu_id}")).status_code == 404


async def test_rejected_product_can_resubmit_but_not_skip_audit(client: AsyncClient, session) -> None:
    """被驳回的商品：改完能重新提交，但**不能跳过审核直接上架**。

    ★ 「已驳回」独立成状态 6 之后，这两条边界必须盯住 ——
      ``submit_for_audit`` 漏了 6，商家改完就卡死；``on_shelf`` 漏了 6，
      等于驳回没有约束力。
    """
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "手机")
    merchant = await register(client, phone="13800138019")
    await open_shop(client, merchant["accessToken"])
    merchant_headers = auth_header(merchant["accessToken"])
    admin_headers = auth_header(admin["accessToken"])

    spu_id = (
        await client.post(
            "/api/merchant/spus", json=_spu_payload(category), headers=merchant_headers
        )
    ).json()["data"]["id"]
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=merchant_headers)
    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": False, "remark": "价格不对"},
        headers=admin_headers,
    )

    # 没过审核，不给直接上架
    refused = await client.post(f"/api/merchant/spus/{spu_id}/on-shelf", headers=merchant_headers)
    assert refused.status_code == 400

    # 但可以重新提交审核
    again = await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=merchant_headers)
    assert again.status_code == 200, again.text
    detail = (await client.get(f"/api/merchant/spus/{spu_id}", headers=merchant_headers)).json()["data"]
    assert detail["status"] == 5


async def test_merchant_list_can_filter_by_rejected_status(client: AsyncClient, session) -> None:
    """★ 商家列表要能按「已驳回」筛。

    这条盯的是**路由层的 status 上界**（``Query(le=...)``）。加了新状态却忘了放宽上界，
    接口会直接 422 —— 前端拿到错误只会渲染一个空列表，看不出是坏了还是真没有，
    是最难查的一种坏法。
    """
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "手机")
    merchant = await register(client, phone="13800138020")
    await open_shop(client, merchant["accessToken"])
    merchant_headers = auth_header(merchant["accessToken"])

    spu_id = (
        await client.post(
            "/api/merchant/spus", json=_spu_payload(category), headers=merchant_headers
        )
    ).json()["data"]["id"]
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=merchant_headers)
    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": False, "remark": "类目挂错了"},
        headers=auth_header(admin["accessToken"]),
    )

    rejected = await client.get("/api/merchant/spus", params={"status": 6}, headers=merchant_headers)
    assert rejected.status_code == 200, rejected.text
    assert spu_id in [i["id"] for i in rejected.json()["data"]["items"]]

    # 也不能同时挂在「草稿」下 —— 两档必须是分开的
    drafts = await client.get("/api/merchant/spus", params={"status": 1}, headers=merchant_headers)
    assert spu_id not in [i["id"] for i in drafts.json()["data"]["items"]]


async def test_admin_can_view_pending_product_detail(client: AsyncClient, session) -> None:
    """★ 平台要能看**待审核**商品的详情 —— 否则审核页只有一个缩略图就要点"通过"。

    既有的两条路径都到不了这里：``/api/merchant/spus/{id}`` 按店铺归属判权
    （运营没有店铺，必 403），公开的 ``/api/spus/{id}`` 只出已上架的。
    """
    admin = await make_admin(client, session)
    category = await _make_category(client, admin["accessToken"], "手机")
    merchant = await register(client, phone="13800138012")
    await open_shop(client, merchant["accessToken"])
    headers = auth_header(merchant["accessToken"])

    spu_id = (await client.post("/api/merchant/spus", json=_spu_payload(category), headers=headers)).json()[
        "data"
    ]["id"]
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=headers)

    # 提交后就该看到，不必先审
    detail = (await client.get(f"/api/admin/spus/{spu_id}", headers=auth_header(admin["accessToken"]))).json()
    assert detail["code"] == "OK", detail
    data = detail["data"]
    assert data["status"] == 5, "审核队列里的就是这个状态"
    assert len(data["skus"]) == 3, "审核要看得到全部 SKU，不是只有标题和缩略图"
    assert data["specGroups"], "规格组也要能看到"

    # 对照：同一件商品，买家那条路走不通
    assert (await client.get(f"/api/spus/{spu_id}")).status_code == 404

    # 非平台角色不能走这条接口 —— 它不做归属过滤，放给商家等于能看别家的商品
    denied = await client.get(f"/api/admin/spus/{spu_id}", headers=headers)
    assert denied.status_code == 403, denied.text

    # 驳回后再看：平台自己写的理由要能看到（复提交时是审核的上下文）
    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": False, "remark": "主图太模糊"},
        headers=auth_header(admin["accessToken"]),
    )
    again = (await client.get(f"/api/admin/spus/{spu_id}", headers=auth_header(admin["accessToken"]))).json()
    assert again["data"]["auditRemark"] == "主图太模糊"


async def test_cannot_shelf_before_audit(client: AsyncClient, session) -> None:
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    spu_id = (await client.post("/api/merchant/spus", json=_spu_payload(category), headers=headers)).json()[
        "data"
    ]["id"]

    resp = await client.post(f"/api/merchant/spus/{spu_id}/on-shelf", headers=headers)
    assert resp.status_code == 400


async def test_only_admin_can_audit(client: AsyncClient, session) -> None:
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    spu_id = (await client.post("/api/merchant/spus", json=_spu_payload(category), headers=headers)).json()[
        "data"
    ]["id"]
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=headers)

    resp = await client.post(f"/api/admin/spus/{spu_id}/audit", json={"approved": True}, headers=headers)
    assert resp.status_code == 403


# ============================================================
# 数据完整性
# ============================================================


async def test_spec_text_and_search_text_persisted(client: AsyncClient, session) -> None:
    spu_id, _merchant = await _create_and_publish(client, session)

    row = await session.scalar(
        text("SELECT search_text FROM product.spu WHERE id = :id"), {"id": int(spu_id)}
    )
    # 标题、类目名、规格值都应该在搜索文本里
    assert "iPhone 16 Pro" in row
    assert "智能手机" in row
    assert "暗夜黑" in row
    assert "512G" in row


async def test_create_spu_is_atomic(client: AsyncClient, session) -> None:
    """SKU 编码冲突时要整体回滚，不能留下半拉子商品。"""
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])

    await client.post("/api/merchant/spus", json=_spu_payload(category), headers=headers)

    payload = _spu_payload(category)
    payload["title"] = "第二个商品"
    for sku in payload["skus"]:
        sku["skuCode"] = f"X-{sku['skuCode']}"  # 编码不冲突，应该成功
    assert (await client.post("/api/merchant/spus", json=payload, headers=headers)).status_code == 200

    count = await session.scalar(text("SELECT count(*) FROM product.spu"))
    assert count == 2
    # 规格组没有被重复写入
    groups = await session.scalar(text("SELECT count(*) FROM product.spec_group"))
    assert groups == 4  # 两个商品各 2 个规格组


# ============================================================
# 平台商品列表与审核意见
# ============================================================


async def _category_tree(client: AsyncClient, admin_token: str) -> str:
    """建「数码 / 手机 / 智能手机」三层，返回末级类目 id。"""
    level1 = await _make_category(client, admin_token, "数码")
    level2 = await _make_category(client, admin_token, "手机", parent_id=level1)
    return await _make_category(client, admin_token, "智能手机", parent_id=level2)


async def _merchant_submits(client: AsyncClient, category_id: str, phone: str) -> tuple[dict, str]:
    """开一家新店、发布商品并**提交审核**（停在待审核）。返回 (商家 token, spu_id)。"""
    merchant = await register(client, phone=phone)
    await open_shop(client, merchant["accessToken"])
    headers = auth_header(merchant["accessToken"])

    resp = await client.post("/api/merchant/spus", json=_spu_payload(category_id), headers=headers)
    assert resp.status_code == 200, resp.text
    spu_id = resp.json()["data"]["id"]

    submitted = await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=headers)
    assert submitted.status_code == 200, submitted.text
    return merchant, spu_id


async def _admin_list(client: AsyncClient, admin_token: str, **params) -> list[dict]:
    resp = await client.get("/api/admin/spus", params=params, headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["items"]


# ============================================================
# 删除商品（软删）
# ============================================================


async def _create_spu(client: AsyncClient, headers: dict[str, str], category_id: str) -> str:
    resp = await client.post("/api/merchant/spus", json=_spu_payload(category_id), headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def _make_on_shelf(client: AsyncClient, session, phone: str = "13900139099") -> tuple[dict, dict, str, str]:
    """造一个**已上架**的商品。返回 (商家, 商品 headers, spu_id, admin token)。"""
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    admin = await make_admin(client, session, phone=phone)
    spu_id = await _create_spu(client, headers, category)

    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=headers)
    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": True},
        headers=auth_header(admin["accessToken"]),
    )
    return merchant, headers, spu_id, admin["accessToken"]


async def _db_deleted(session, spu_id: str) -> bool:
    return bool(
        await session.scalar(text("SELECT deleted FROM product.spu WHERE id = :id"), {"id": int(spu_id)})
    )


async def _db_stock_sku_ids(session, sku_ids: set[str]) -> set[str]:
    """这些 SKU 在 ``inventory.sku_stock`` 里真实存在的行。

    读侧现在会把已删商品过滤掉，光看接口返回分不清"没有行"和"有行但被藏了"，
    所以关键断言要落到库上。
    """
    if not sku_ids:
        return set()
    rows = await session.scalars(
        text("SELECT sku_id FROM inventory.sku_stock WHERE sku_id = ANY(:ids)"),
        {"ids": [int(s) for s in sku_ids]},
    )
    return {str(r) for r in rows}


async def test_delete_draft_product(client: AsyncClient, session) -> None:
    """草稿可以直接删。删的是**软删** —— 行还在，只是打了 deleted 标记。"""
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    spu_id = await _create_spu(client, headers, category)

    resp = await client.delete(f"/api/merchant/spus/{spu_id}", headers=headers)
    assert resp.status_code == 200, resp.text

    listed = (await client.get("/api/merchant/spus", headers=headers)).json()["data"]["items"]
    assert all(i["id"] != spu_id for i in listed)

    # ★ 行还在。物理删除会让历史订单指向空气，而且误删救不回来
    assert await _db_deleted(session, spu_id) is True


async def test_delete_off_shelf_product(client: AsyncClient, session) -> None:
    """已下架的商品可以删 —— "先下架、再删除"就是设计好的两步。"""
    _, headers, spu_id, _ = await _make_on_shelf(client, session)
    assert (await client.post(f"/api/merchant/spus/{spu_id}/off-shelf", headers=headers)).status_code == 200

    assert (await client.delete(f"/api/merchant/spus/{spu_id}", headers=headers)).status_code == 200


async def test_cannot_delete_on_shelf_product(client: AsyncClient, session) -> None:
    """★ 在售的不让直接删：商城正卖着的东西忽然消失，买家点进去就是 404。"""
    _, headers, spu_id, _ = await _make_on_shelf(client, session)

    resp = await client.delete(f"/api/merchant/spus/{spu_id}", headers=headers)
    assert resp.status_code == 400, resp.text
    assert "先下架" in resp.json()["message"]
    assert await _db_deleted(session, spu_id) is False


async def test_cannot_delete_pending_audit_product(client: AsyncClient, session) -> None:
    """★ 审核中的不让删：删了平台正在看的那条会变成"商品不存在"，运营一头雾水。"""
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    spu_id = await _create_spu(client, headers, category)
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=headers)

    resp = await client.delete(f"/api/merchant/spus/{spu_id}", headers=headers)
    assert resp.status_code == 400, resp.text
    assert "审核" in resp.json()["message"]


async def test_cannot_delete_another_shops_product(client: AsyncClient, session) -> None:
    """★ 越权返回 404 而不是 403：不区分"不存在"和"不是你的"。"""
    merchant, category, _ = await _setup_merchant(client, session)
    spu_id = await _create_spu(client, auth_header(merchant["accessToken"]), category)

    other = await register(client, phone="13800138077")
    await open_shop(client, other["accessToken"], name="别家店")

    resp = await client.delete(
        f"/api/merchant/spus/{spu_id}", headers=auth_header(other["accessToken"])
    )
    assert resp.status_code == 404


async def test_deleted_product_leaves_platform_list(client: AsyncClient, session) -> None:
    """删完平台侧也看不到了 —— 否则运营会对着一件不存在的商品点审核。"""
    _, headers, spu_id, admin_token = await _make_on_shelf(client, session)
    await client.post(f"/api/merchant/spus/{spu_id}/off-shelf", headers=headers)

    before = {i["id"] for i in await _admin_list(client, admin_token, limit=60)}
    assert spu_id in before

    await client.delete(f"/api/merchant/spus/{spu_id}", headers=headers)

    after = {i["id"] for i in await _admin_list(client, admin_token, limit=60)}
    assert spu_id not in after


async def test_delete_twice_is_404(client: AsyncClient, session) -> None:
    """删过就查不到了（get_spu 过滤 deleted），再删是 404 而不是"又删成功一次"。"""
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    spu_id = await _create_spu(client, headers, category)

    assert (await client.delete(f"/api/merchant/spus/{spu_id}", headers=headers)).status_code == 200
    assert (await client.delete(f"/api/merchant/spus/{spu_id}", headers=headers)).status_code == 404


async def test_deleted_product_gets_no_stock_rows(client: AsyncClient, session) -> None:
    """★ 删掉的商品不该在库存页凭空冒出库存行。

    库存页会"给还没有库存记录的 SKU 补行"（``inventory.ensure_stock_rows``），
    而它拿的 SKU 列表来自 ``product.list_skus_by_shop`` —— 那个查询必须排掉
    已软删的商品，否则商家刚删掉一个从没建过库存的商品，一打开库存页就多出
    几行 0 库存的残留行，像是没删干净。

    断言直接查库，不只查接口返回 —— 接口侧现在也过滤已删商品，
    光看返回会把"补行没跑"和"补行跑了但被过滤了"混为一谈。
    """
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    # 有仓才会触发补行，否则这个用例会空过
    wh = await client.post("/api/merchant/warehouses", json={"name": "测试仓"}, headers=headers)
    assert wh.status_code == 200, wh.text

    async def skus_of(spu_id: str) -> set[str]:
        detail = await client.get(f"/api/merchant/spus/{spu_id}", headers=headers)
        return {s["id"] for s in detail.json()["data"]["skus"]}

    doomed = await _create_spu(client, headers, category)
    doomed_skus = await skus_of(doomed)
    assert (await client.delete(f"/api/merchant/spus/{doomed}", headers=headers)).status_code == 200

    # 一件没被删的，用来证明"补行"确实在工作 —— 否则下面的断言是空过
    alive = await _create_spu(client, headers, category)
    alive_skus = await skus_of(alive)

    items = (
        await client.get("/api/merchant/inventory", params={"limit": 100}, headers=headers)
    ).json()["data"]["items"]
    stocked = {i["skuId"] for i in items}

    assert alive_skus <= stocked, "没删的商品应该被补上库存行"
    assert not (doomed_skus & stocked), "已删除的商品不该被补库存行"
    assert not await _db_stock_sku_ids(session, doomed_skus), "库里也不该有它的库存行"


async def test_inventory_hides_stock_rows_of_deleted_product(
    client: AsyncClient, session
) -> None:
    """★ 已经建过库存行的商品被删掉后，库存页不该再显示那一行。

    行必须**留在库里**：上面可能挂着未发货订单的预占，删了发货/解锁会对不上账。
    所以过滤只能做在读侧（``inventory.repo.list_stock`` 的 ``exclude_sku_ids``）。
    """
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    await client.post("/api/merchant/warehouses", json={"name": "测试仓"}, headers=headers)
    # 有仓才会触发补行

    spu_id = await _create_spu(client, headers, category)
    skus = {
        s["id"]
        for s in (
            await client.get(f"/api/merchant/spus/{spu_id}", headers=headers)
        ).json()["data"]["skus"]
    }

    # 先逛一次库存页，让它的 SKU 都拿到库存行
    before = (
        await client.get("/api/merchant/inventory", params={"limit": 100}, headers=headers)
    ).json()["data"]["items"]
    assert skus <= {i["skuId"] for i in before}, "这一步没补上行，下面的断言会空过"

    assert (await client.delete(f"/api/merchant/spus/{spu_id}", headers=headers)).status_code == 200

    after = (
        await client.get("/api/merchant/inventory", params={"limit": 100}, headers=headers)
    ).json()["data"]["items"]
    assert not (skus & {i["skuId"] for i in after}), "已删商品的库存行不该出现在列表里"
    assert skus == await _db_stock_sku_ids(session, skus), "行还在库里（不能物理删）"


async def test_admin_lists_pending_spus(client: AsyncClient, session) -> None:
    """★ 这条路径以前根本不存在：平台侧一个能列出商品的接口都没有，
    审核接口只能靠调用方自己知道 spu_id —— 所以一直没有可用的审核页。"""
    admin = await make_admin(client, session)
    category = await _category_tree(client, admin["accessToken"])
    _, spu_id = await _merchant_submits(client, category, "13800138011")

    items = await _admin_list(client, admin["accessToken"], status=5)
    assert [i["id"] for i in items] == [spu_id]
    assert items[0]["status"] == 5


async def test_admin_spu_list_is_cross_shop(client: AsyncClient, session) -> None:
    """平台看到的是**所有店铺**的商品 —— 这正是它和 /api/merchant/spus 的区别。"""
    admin = await make_admin(client, session)
    category = await _category_tree(client, admin["accessToken"])
    _, first = await _merchant_submits(client, category, "13800138012")
    _, second = await _merchant_submits(client, category, "13800138013")

    items = await _admin_list(client, admin["accessToken"], status=5, limit=60)
    assert {first, second} <= {i["id"] for i in items}
    # 两件商品来自两个不同的店
    assert len({i["shopId"] for i in items}) == 2


async def test_admin_spu_list_filters_by_status(client: AsyncClient, session) -> None:
    admin = await make_admin(client, session)
    category = await _category_tree(client, admin["accessToken"])
    _, spu_id = await _merchant_submits(client, category, "13800138014")

    audited = await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": True},
        headers=auth_header(admin["accessToken"]),
    )
    assert audited.status_code == 200, audited.text

    assert await _admin_list(client, admin["accessToken"], status=5) == []
    assert [i["id"] for i in await _admin_list(client, admin["accessToken"], status=2)] == [spu_id]


async def test_admin_spu_list_requires_admin(client: AsyncClient) -> None:
    merchant = await register(client, phone="13800138015")
    resp = await client.get("/api/admin/spus", headers=auth_header(merchant["accessToken"]))
    assert resp.status_code == 403


async def test_reject_reason_reaches_merchant(client: AsyncClient, session) -> None:
    """★ 驳回理由要落到商家看得见的地方。

    这个接口从第一天就收 ``remark``，但**没有任何地方存它** —— 驳回后商品
    只是悄悄回到草稿，商家不知道该改什么。
    """
    admin = await make_admin(client, session)
    category = await _category_tree(client, admin["accessToken"])
    merchant, spu_id = await _merchant_submits(client, category, "13800138016")

    rejected = await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": False, "remark": "主图太模糊，换一张"},
        headers=auth_header(admin["accessToken"]),
    )
    assert rejected.status_code == 200, rejected.text

    detail = (
        await client.get(
            f"/api/merchant/spus/{spu_id}", headers=auth_header(merchant["accessToken"])
        )
    ).json()["data"]
    assert detail["status"] == 6  # 转「已驳回」，改完可以再交
    assert detail["auditRemark"] == "主图太模糊，换一张"


async def test_approve_clears_previous_reject_reason(client: AsyncClient, session) -> None:
    """改完再提交并通过之后，不该还挂着上一次的驳回理由。"""
    admin = await make_admin(client, session)
    category = await _category_tree(client, admin["accessToken"])
    merchant, spu_id = await _merchant_submits(client, category, "13800138017")
    admin_headers = auth_header(admin["accessToken"])
    merchant_headers = auth_header(merchant["accessToken"])

    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": False, "remark": "旧理由"},
        headers=admin_headers,
    )
    await client.post(f"/api/merchant/spus/{spu_id}/submit", headers=merchant_headers)
    await client.post(f"/api/admin/spus/{spu_id}/audit", json={"approved": True}, headers=admin_headers)

    detail = (await client.get(f"/api/merchant/spus/{spu_id}", headers=merchant_headers)).json()["data"]
    assert detail["status"] == 2
    assert detail["auditRemark"] is None


async def test_buyer_never_sees_audit_remark(client: AsyncClient, session) -> None:
    """★ 审核意见是平台↔商家之间的内部说明，买家不该看到。

    正常流程下"通过"会把理由清掉，所以这里直接把状态推成已上架来构造这个
    组合 —— 防的是以后有人把清空的顺序改掉。
    """
    admin = await make_admin(client, session)
    category = await _category_tree(client, admin["accessToken"])
    merchant, spu_id = await _merchant_submits(client, category, "13800138018")

    await client.post(
        f"/api/admin/spus/{spu_id}/audit",
        json={"approved": False, "remark": "内部说明"},
        headers=auth_header(admin["accessToken"]),
    )
    await session.execute(text("UPDATE product.spu SET status = 2 WHERE id = :id"), {"id": int(spu_id)})
    await session.commit()

    public = (await client.get(f"/api/spus/{spu_id}")).json()["data"]
    assert public["auditRemark"] is None

    # 店主自己仍然看得到
    mine = (
        await client.get(
            f"/api/merchant/spus/{spu_id}", headers=auth_header(merchant["accessToken"])
        )
    ).json()["data"]
    assert mine["auditRemark"] == "内部说明"


async def test_create_product_without_images(client: AsyncClient, session) -> None:
    """主图与规格封面都可以为空 —— "先发布、后补图"是合法路径。

    以前后台用一段灰色占位图 data URI 来满足"非空"校验，结果占位图被当成真实图片
    存进库，一路传染到购物车、结算页和订单快照（它是一张能成功加载的灰图，
    前端的 onImageError 兜底根本不触发）。空就是空，兜底交给展示层。
    """
    merchant, category, _ = await _setup_merchant(client, session)
    headers = auth_header(merchant["accessToken"])
    payload = _spu_payload(category)
    payload["mainImage"] = ""
    for sku in payload["skus"]:
        sku["coverImage"] = ""

    resp = await client.post("/api/merchant/spus", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text

    spu_id = resp.json()["data"]["id"]
    detail = (
        await client.get(f"/api/merchant/spus/{spu_id}", headers=headers)
    ).json()["data"]
    assert detail["mainImage"] == ""
    assert all(s["coverImage"] == "" for s in detail["skus"])
    # 库里也不能留下任何占位图形态的值
    assert (
        await session.scalar(text("SELECT count(*) FROM product.sku WHERE cover_image LIKE 'data:%'"))
        == 0
    )
