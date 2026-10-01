"""给本地环境造一批演示数据：管理员、类目树、商家、几个商品。

只用于本地/演示环境，重复执行会因手机号已存在而报错（用时间戳规避）。
"""

from __future__ import annotations

import subprocess
import sys
import time

import httpx

API = "http://127.0.0.1:8000/api"
PASSWORD = "Str0ng-Pass!2026"
SUFFIX = str(int(time.time()))[-6:]


def psql(sql: str) -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "eshop-postgres",
            "psql",
            "-U",
            "eshop_owner",
            "-d",
            "eshop",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
    )


def unwrap(resp: httpx.Response) -> dict:
    body = resp.json()
    if body.get("code") != "OK":
        raise SystemExit(f"接口失败 {resp.request.url}: {body}")
    return body["data"]


def main() -> None:
    client = httpx.Client(base_url=API, timeout=20)

    # ---------- 管理员 ----------
    # 角色只能由数据库决定，所以先注册再用 SQL 提权，然后重新登录拿带 admin 的 token
    admin_phone = f"139{SUFFIX}1".ljust(11, "0")[:11]
    tokens = unwrap(
        client.post(
            "/auth/register",
            json={"phone": admin_phone, "password": PASSWORD, "nickname": "平台运营"},
        )
    )
    me = unwrap(client.get("/me", headers={"Authorization": f"Bearer {tokens['accessToken']}"}))
    psql(f"UPDATE account.user SET role='admin' WHERE id = {me['id']};")

    admin = unwrap(client.post("/auth/login", json={"phone": admin_phone, "password": PASSWORD}))
    admin_h = {"Authorization": f"Bearer {admin['accessToken']}"}

    # ---------- 类目树 ----------
    tree = [
        ("数码", None),
        ("手机", "数码"),
        ("智能手机", "手机"),
        ("电脑", "数码"),
        ("图书", None),
        ("小说", "图书"),
    ]
    ids: dict[str, str] = {}
    for name, parent in tree:
        data = unwrap(
            client.post(
                "/admin/categories",
                json={"name": name, "parentId": ids.get(parent) if parent else None},
                headers=admin_h,
            )
        )
        ids[name] = data["id"]
    print(f"类目已建：{len(ids)} 个，末级={'智能手机', '小说'}")

    # ---------- 商家 ----------
    merchant_phone = f"138{SUFFIX}2".ljust(11, "0")[:11]
    merchant = unwrap(
        client.post(
            "/auth/register",
            json={"phone": merchant_phone, "password": PASSWORD, "nickname": "演示商家"},
        )
    )
    merchant_h = {"Authorization": f"Bearer {merchant['accessToken']}"}
    unwrap(client.post("/merchant/shop", json={"name": "演示旗舰店"}, headers=merchant_h))

    # ---------- 商品 ----------
    products = [
        {
            "categoryId": ids["智能手机"],
            "title": "iPhone 16 Pro",
            "subTitle": "A18 Pro 芯片 · 钛金属机身",
            "mainImage": "/media/placeholder.svg",
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
            # 故意不建"原色钛 + 512G"：演示无效组合会被前端置灰
            "skus": [
                {"skuCode": f"IP16-B256-{SUFFIX}", "specValueKeys": ["black", "256"], "price": 799900, "coverImage": "/media/placeholder.svg", "weightG": 199},
                {"skuCode": f"IP16-B512-{SUFFIX}", "specValueKeys": ["black", "512"], "price": 899900, "coverImage": "/media/placeholder.svg", "weightG": 199},
                {"skuCode": f"IP16-N256-{SUFFIX}", "specValueKeys": ["natural", "256"], "price": 799900, "coverImage": "/media/placeholder.svg", "weightG": 199},
            ],
        },
        {
            "categoryId": ids["智能手机"],
            "title": "小米 15 Ultra",
            "subTitle": "徕卡四摄 · 骁龙 8 至尊版",
            "mainImage": "/media/placeholder.svg",
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
                {"skuCode": f"MI15-STD-{SUFFIX}", "specValueKeys": ["std"], "price": 599900, "coverImage": "/media/placeholder.svg", "weightG": 220},
                {"skuCode": f"MI15-PRO-{SUFFIX}", "specValueKeys": ["pro"], "price": 699900, "coverImage": "/media/placeholder.svg", "weightG": 225},
            ],
        },
        {
            "categoryId": ids["小说"],
            "title": "三体（全集）",
            "subTitle": "刘慈欣 · 地球往事三部曲",
            "mainImage": "/media/placeholder.svg",
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
                {"skuCode": f"ST-PAPER-{SUFFIX}", "specValueKeys": ["paper"], "price": 6900, "coverImage": "/media/placeholder.svg", "weightG": 1200},
                {"skuCode": f"ST-HARD-{SUFFIX}", "specValueKeys": ["hard"], "price": 12900, "coverImage": "/media/placeholder.svg", "weightG": 1600},
            ],
        },
    ]

    for payload in products:
        created = unwrap(client.post("/merchant/spus", json=payload, headers=merchant_h))
        unwrap(client.post(f"/merchant/spus/{created['id']}/submit", headers=merchant_h))
        unwrap(client.post(f"/admin/spus/{created['id']}/audit", json={"approved": True}, headers=admin_h))
        print(f"已上架：{payload['title']}（{len(created['skus'])} 个 SKU）")

    print()
    print("=" * 56)
    print(f"管理员账号：{admin_phone} / {PASSWORD}")
    print(f"商家账号：  {merchant_phone} / {PASSWORD}")
    print("=" * 56)


if __name__ == "__main__":
    sys.exit(main())
