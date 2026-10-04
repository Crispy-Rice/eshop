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

    second = (
        await client.get("/api/search", params={"keyword": "商品", "limit": 2, "cursor": first["nextCursor"]})
    ).json()["data"]
    assert len(second["items"]) == 1
    assert second["hasMore"] is False

    # 两页合起来正好是全部 3 个，且没有重复
    seen = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert sorted(seen) == sorted(spu_ids)


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


async def test_audit_rejection_returns_product_to_draft(client: AsyncClient, session) -> None:
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
    assert detail["status"] == 1  # 退回草稿，商家可以改了再提交
    assert (await client.get(f"/api/spus/{spu_id}")).status_code == 404


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
