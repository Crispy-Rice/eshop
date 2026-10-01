"""给本地环境造一批演示数据：类目树、平台管理员、商家、几个已上架商品。

设计上的两个要求：

1. **不依赖 docker CLI**。提权管理员直接用应用自己的数据库连接串改，
   而不是 ``docker exec ... psql`` —— 脚本应当只依赖 Python 依赖。
2. **可重复执行**。类目按名字复用、商品按标题去重，
   重复跑不会因为"同名类目已存在"而中断。

用法::

    cd backend; uv run python scripts/seed_demo.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# 直接 `python scripts/seed_demo.py` 运行时，Python 只把 scripts/ 放进
# 模块搜索路径，`app` 包会找不到。把 backend/ 补进去。
# 这段必须在 import app.* 之前。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings

API = "http://127.0.0.1:8000/api"
# 演示账号的密码，硬编码是刻意的：它只是本地数据，不是任何真实凭证
PASSWORD = "Str0ng-Pass!2026"  # noqa: S105

# 账号用固定手机号，重复执行时复用而不是每次新建一个
ADMIN_PHONE = "13900000001"
MERCHANT_PHONE = "13800000001"

# SKU 编码带时间戳，避免和之前跑出来的商品撞码
SUFFIX = str(int(time.time()))[-6:]

# (类目名, 上级类目名)
CATEGORIES: list[tuple[str, str | None]] = [
    ("数码", None),
    ("手机", "数码"),
    ("智能手机", "手机"),
    ("电脑", "数码"),
    ("图书", None),
    ("小说", "图书"),
]


def unwrap(resp: httpx.Response) -> Any:
    body = resp.json()
    if body.get("code") != "OK":
        raise SystemExit(f"接口失败 {resp.request.method} {resp.request.url}\n  {body}")
    return body["data"]


def login_or_register(client: httpx.Client, phone: str, nickname: str) -> dict[str, str]:
    """已存在就登录，不存在才注册。返回请求头。"""
    resp = client.post("/auth/login", json={"phone": phone, "password": PASSWORD})
    if resp.json().get("code") != "OK":
        unwrap(
            client.post(
                "/auth/register",
                json={"phone": phone, "password": PASSWORD, "nickname": nickname},
            )
        )
        resp = client.post("/auth/login", json={"phone": phone, "password": PASSWORD})

    return {"Authorization": f"Bearer {unwrap(resp)['accessToken']}"}


async def promote_to_admin(user_id: int) -> None:
    """把用户角色改成 admin。

    角色的唯一来源是数据库（JWT 里带 role），所以只能直接改库；
    这里用应用自己的连接串，避免依赖 docker CLI。
    """
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE account.user SET role = 'admin' WHERE id = :uid"),
                {"uid": user_id},
            )
    finally:
        await engine.dispose()


def find_category(nodes: list[dict], name: str, parent_id: str | None) -> str | None:
    """在类目树里按 (名字, 上级) 找。

    ★ /api/categories 返回的是嵌套树而不是扁平列表，必须递归进 children，
    否则子类目永远找不到，脚本会重复创建并撞上"同级下已存在同名类目"。
    """
    for node in nodes:
        if node["name"] == name and node["parentId"] == parent_id:
            return node["id"]
        found = find_category(node.get("children") or [], name, parent_id)
        if found:
            return found
    return None


def ensure_categories(client: httpx.Client, admin_h: dict[str, str]) -> dict[str, str]:
    """按需建类目，已存在的直接复用，保证脚本可以重复执行。"""
    ids: dict[str, str] = {}
    for name, parent_name in CATEGORIES:
        parent_id = ids.get(parent_name) if parent_name else None

        tree = unwrap(client.get("/categories"))
        existing = find_category(tree, name, parent_id)
        if existing:
            ids[name] = existing
            continue

        created = unwrap(
            client.post("/admin/categories", json={"name": name, "parentId": parent_id}, headers=admin_h)
        )
        ids[name] = created["id"]
    return ids


def build_products(category_ids: dict[str, str]) -> list[dict]:
    img = "/media/placeholder.svg"
    return [
        {
            "categoryId": category_ids["智能手机"],
            "title": "iPhone 16 Pro",
            "subTitle": "A18 Pro 芯片 · 钛金属机身",
            "mainImage": img,
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
            # 故意不建"原色钛 + 512G"：用来演示无效组合会被前端置灰
            "skus": [
                {
                    "skuCode": f"IP16-B256-{SUFFIX}",
                    "specValueKeys": ["black", "256"],
                    "price": 799900,
                    "coverImage": img,
                    "weightG": 199,
                },
                {
                    "skuCode": f"IP16-B512-{SUFFIX}",
                    "specValueKeys": ["black", "512"],
                    "price": 899900,
                    "coverImage": img,
                    "weightG": 199,
                },
                {
                    "skuCode": f"IP16-N256-{SUFFIX}",
                    "specValueKeys": ["natural", "256"],
                    "price": 799900,
                    "coverImage": img,
                    "weightG": 199,
                },
            ],
        },
        {
            "categoryId": category_ids["智能手机"],
            "title": "小米 15 Ultra",
            "subTitle": "徕卡四摄 · 骁龙 8 至尊版",
            "mainImage": img,
            "specGroups": [
                {
                    "name": "版本",
                    "values": [
                        {"key": "std", "value": "标准版"},
                        {"key": "pro", "value": "Pro 版"},
                    ],
                }
            ],
            "skus": [
                {
                    "skuCode": f"MI15-STD-{SUFFIX}",
                    "specValueKeys": ["std"],
                    "price": 599900,
                    "coverImage": img,
                    "weightG": 220,
                },
                {
                    "skuCode": f"MI15-PRO-{SUFFIX}",
                    "specValueKeys": ["pro"],
                    "price": 699900,
                    "coverImage": img,
                    "weightG": 225,
                },
            ],
        },
        {
            "categoryId": category_ids["小说"],
            "title": "三体（全集）",
            "subTitle": "刘慈欣 · 地球往事三部曲",
            "mainImage": img,
            "specGroups": [
                {
                    "name": "装帧",
                    "values": [
                        {"key": "paper", "value": "平装"},
                        {"key": "hard", "value": "精装"},
                    ],
                }
            ],
            "skus": [
                {
                    "skuCode": f"ST-PAPER-{SUFFIX}",
                    "specValueKeys": ["paper"],
                    "price": 6900,
                    "coverImage": img,
                    "weightG": 1200,
                },
                {
                    "skuCode": f"ST-HARD-{SUFFIX}",
                    "specValueKeys": ["hard"],
                    "price": 12900,
                    "coverImage": img,
                    "weightG": 1600,
                },
            ],
        },
    ]


def main() -> int:
    """脚本主体是同步的（httpx 用同步客户端），只有改库那一步需要跑一次协程。"""
    with httpx.Client(base_url=API, timeout=30) as client:
        # 后端没起来时给一句人话，而不是抛一堆连接异常
        try:
            client.get("/categories")
        except httpx.TransportError:
            print(f"连不上后端 {API}，请先启动：cd backend; uv run uvicorn app.main:app --reload --port 8000")
            return 1

        # ---------- 管理员 ----------
        client_h = login_or_register(client, ADMIN_PHONE, "平台运营")
        me = unwrap(client.get("/me", headers=client_h))
        if me["role"] != "admin":
            # ★ 接口返回的 id 是字符串（雪花 ID 超出 JS 安全整数范围，
            #   约定以字符串传），写进 BIGINT 列前必须转回 int
            asyncio.run(promote_to_admin(int(me["id"])))
            # 角色变了要重新登录，才能拿到带 admin 的 token
            client_h = login_or_register(client, ADMIN_PHONE, "平台运营")

        admin_h = client_h
        category_ids = ensure_categories(client, admin_h)
        print(f"类目就绪：{len(category_ids)} 个（已存在的复用，不重复创建）")

        # ---------- 商家 ----------
        merchant_h = login_or_register(client, MERCHANT_PHONE, "演示商家")

        shop_resp = client.post("/merchant/shop", json={"name": "演示旗舰店"}, headers=merchant_h)
        if shop_resp.json().get("code") != "OK":
            print("店铺已存在，跳过开店")

        # ---------- 商品 ----------
        existing_titles = {
            item["title"]
            for item in unwrap(client.get("/merchant/spus", params={"limit": 60}, headers=merchant_h))[
                "items"
            ]
        }

        published = 0
        for payload in build_products(category_ids):
            if payload["title"] in existing_titles:
                print(f"跳过（已存在）：{payload['title']}")
                continue

            created = unwrap(client.post("/merchant/spus", json=payload, headers=merchant_h))
            unwrap(client.post(f"/merchant/spus/{created['id']}/submit", headers=merchant_h))
            unwrap(
                client.post(
                    f"/admin/spus/{created['id']}/audit",
                    json={"approved": True},
                    headers=admin_h,
                )
            )
            published += 1
            print(f"已上架：{payload['title']}（{len(created['skus'])} 个 SKU）")

    print()
    print("=" * 56)
    print(f"管理员账号：{ADMIN_PHONE} / {PASSWORD}")
    print(f"商家账号：  {MERCHANT_PHONE} / {PASSWORD}")
    print(f"本次新上架 {published} 个商品")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
