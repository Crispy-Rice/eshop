"""给演示环境造一批数据：类目树、平台管理员、商家、几个已上架商品、券、运费。

设计上的三个要求：

1. **不依赖 docker CLI**。提权管理员直接用应用自己的数据库连接串改，
   而不是 ``docker exec ... psql`` —— 脚本应当只依赖 Python 依赖。
2. **可重复执行**。类目按名字复用、商品按标题去重，
   重复跑不会因为"同名类目已存在"而中断。
3. **演示图是画出来的**。用 Pillow 生成"影棚渐变 + 商品名"，再走**真实的上传接口**
   传上去 —— 不依赖外网占位服务，也顺带把上传流水线真的跑一遍。

用法::

    cd backend; uv run python scripts/seed_demo.py

对**已部署的环境**灌数据，要在 api 容器里跑，不能从本机指过去 ——
提权管理员那一步是**直连数据库**改的（``promote_to_admin``），从本机跑会改到
本地库、远端那个账号拿不到 admin 角色。容器里则两样都通：数据库在同一个
compose 网络里，``127.0.0.1:8000`` 正好是应用自己。

    scp backend/scripts/seed_demo.py <服务器>:/tmp/
    ssh <服务器>
      docker cp /tmp/seed_demo.py eshop-api-1:/app/scripts/seed_demo.py
      cd /opt/eshop/deploy
      docker compose -f docker-compose.yml -f docker-compose.demo.yml \\
        exec -T api python scripts/seed_demo.py

``SEED_API_BASE`` 仍可覆盖 API 基址，用于"库能连、应用在别处"的场景。
"""

from __future__ import annotations

import asyncio
import colorsys
import copy
import hashlib
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# 直接 `python scripts/seed_demo.py` 运行时，Python 只把 scripts/ 放进
# 模块搜索路径，`app` 包会找不到。把 backend/ 补进去。
# 这段必须在 import app.* 之前。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings

API = os.environ.get("SEED_API_BASE", "http://127.0.0.1:8000/api")
# 演示账号的密码，硬编码是刻意的：它只是本地数据，不是任何真实凭证
PASSWORD = "Str0ng-Pass!2026"  # noqa: S105

# 账号用固定手机号，重复执行时复用而不是每次新建一个
ADMIN_PHONE = "13900000001"
MERCHANT_PHONE = "13800000001"
# 演示买家：商城那条链路（逛商品、加购、下单、评价、售后）全靠它
BUYER_PHONE = "13900000002"

# ============================================================
# 演示图生成
#
# ★ 不用在线占位服务（picsum 之类）：种子脚本要能离线跑，也不该把演示环境
#   绑在外网上。Pillow 本来就是项目依赖（上传图片的压缩流水线用它），直接画。
#
# 画出来的是"影棚背景 + 商品名"的设计稿风格：对角渐变、低饱和、白字居中。
# 比灰方块像样得多，也不会被误认成真图。
# ============================================================
IMAGE_SIZE = 800


def _hsl(h: float, s: float, lightness: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h, lightness, s)
    return round(r * 255), round(g * 255), round(b * 255)


def _font(size: int) -> Any:
    """Pillow ≥10.1 的默认字体是**可缩放的 TrueType**（所以 ``anchor`` 也能用）。

    不去找系统 TTF：Windows 有 arial、容器里没有，路径写死会在别的机器上炸。
    pyproject 里钉的是 pillow>=11，这个签名一定存在。
    """
    return ImageFont.load_default(size=size)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int) -> Any:
    """把字号缩到能塞进 max_width 为止（长标题不该溢出画布）。"""
    size = start
    while size > 14:
        f = _font(size)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size -= 4
    return _font(14)


def _seed_of(title: str) -> str:
    """标题 → 稳定的十六进制种子。决定色相，也当上传用的文件名。

    用 sha256 而不是 md5：这里不是安全用途，但没必要给静态扫描留口实。
    """
    return hashlib.sha256(title.encode("utf-8")).hexdigest()


def demo_png(title: str, caption: str = "") -> bytes:
    """生成一张 800×800 的演示图（PNG 字节）。同名的图每次都一样（种子取色）。"""
    # 标题定色相 —— 同一个商品重跑拿到同一个颜色，不会每次变
    hue = int(_seed_of(title)[:8], 16) % 360 / 360
    top = _hsl(hue, 0.30, 0.26)
    bottom = _hsl((hue + 0.07) % 1.0, 0.36, 0.50)

    im = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE))
    draw = ImageDraw.Draw(im)

    for y in range(IMAGE_SIZE):
        t = y / (IMAGE_SIZE - 1)
        draw.line(
            [(0, y), (IMAGE_SIZE, y)],
            fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )

    # 一圈内描边，让它看起来是"设计过的卡片"而不是纯色块。
    # 用"底色掺白"算出来而不是写 rgba —— 画布是 RGB，alpha 会被直接忽略。
    edge = tuple(round(c + (255 - c) * 0.28) for c in top)
    inset = IMAGE_SIZE // 12
    draw.rounded_rectangle(
        [inset, inset, IMAGE_SIZE - inset, IMAGE_SIZE - inset],
        radius=IMAGE_SIZE // 16,
        outline=edge,
        width=2,
    )

    text = title.strip()
    font = _fit_font(draw, text, IMAGE_SIZE - inset * 3, IMAGE_SIZE // 8)
    ty = IMAGE_SIZE // 2 - (IMAGE_SIZE // 24 if caption else 0)
    draw.text((IMAGE_SIZE // 2, ty), text, font=font, fill=(255, 255, 255), anchor="mm")

    if caption:
        small = _fit_font(draw, caption, IMAGE_SIZE - inset * 3, IMAGE_SIZE // 20)
        draw.text(
            (IMAGE_SIZE // 2, ty + IMAGE_SIZE // 10),
            caption,
            font=small,
            fill=(255, 255, 255),
            anchor="mm",
        )

    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def upload_demo_image(
    client: httpx.Client, headers: dict[str, str], *, title: str, caption: str = ""
) -> str:
    """画一张图并走真实上传接口传上去，返回可直接入库的 url。

    走接口而不是直接写文件：这样种子数据走的是和商家上传**完全一样**的路径
    （判型、重编码、缩略图、路径白名单都会被真的走一遍）。
    """
    resp = client.post(
        "/files/images",
        params={"biz": "products"},
        files={"file": (f"{_seed_of(title)[:8]}.png", demo_png(title, caption), "image/png")},
        headers=headers,
    )
    return unwrap(resp)["url"]


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


def ensure_coupons(client: httpx.Client, admin_h: dict[str, str]) -> int:
    """按名字建几个演示券模板，已存在的跳过。

    券模板一旦有券发出就**不可修改**（docs/04 §11），所以这里只判断"同名模板
    是否已存在"，存在就跳过 —— 重复执行不会因为建不出来而中断。
    """
    existing = set()
    for item in unwrap(client.get("/coupons/available", headers=admin_h)):
        existing.add(item["template"]["name"])

    # (名字, 门槛分, 减免分, 总量, 每人限领)
    plans = [
        ("满 100 减 20", 10000, 2000, 1000, 1),
        ("满 500 减 80", 50000, 8000, 500, 2),
        ("新人无门槛减 10", 0, 1000, 2000, 1),
    ]

    created = 0
    for name, threshold, value, total, per_user in plans:
        if name in existing:
            continue
        unwrap(
            client.post(
                "/admin/coupons/templates",
                json={
                    "name": name,
                    "type": 3 if threshold == 0 else 1,
                    "discountValue": value,
                    "threshold": threshold,
                    "totalCount": total,
                    "perUserLimit": per_user,
                    "validType": 1,
                    "validStart": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    "validEnd": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                    "scopeType": 1,
                },
                headers=admin_h,
            )
        )
        created += 1
    return created


def ensure_buyer(client: httpx.Client) -> dict[str, str]:
    """建一个带收货地址的演示买家，返回它的请求头。

    ★ 不能只建管理员和商家：**商城那条链路才是主要演示面**，而逛商品要登录、
    下单要收货地址 —— 少了这两样，打开商城第一件事就是自己去注册填地址。
    """
    headers = login_or_register(client, BUYER_PHONE, "演示买家")
    if not unwrap(client.get("/me/addresses", headers=headers)):
        unwrap(
            client.post(
                "/me/addresses",
                json={
                    "receiverName": "张三",
                    "phone": BUYER_PHONE,
                    "province": "上海市",
                    "city": "上海市",
                    "district": "浦东新区",
                    "detail": "世纪大道 100 号",
                    # 有区域规则的地区码，这样运费能真的算出来
                    "regionCode": "310115",
                    "isDefault": True,
                },
                headers=headers,
            )
        )
    return headers


def ensure_warehouse(client: httpx.Client, merchant_h: dict[str, str]) -> str:
    """保证商家有一个仓，返回它的 id。

    ★ 这一步不能省：**库存行是按 ``(sku, 仓库)`` 建的**，而"补库存"是去库存列表里
    挑 ``available=0`` 的加 —— 没有仓库就没有库存行，补库存会**静默地一条也不做**。
    运费绑定同样需要 ``warehouseId``。

    全新库里漏掉这一步的表现很隐蔽：商品上架了、图也有了，**但下不了单**。
    """
    warehouses = unwrap(client.get("/merchant/warehouses", headers=merchant_h))
    if warehouses:
        return warehouses[0]["id"]
    # 该店铺的第一个仓库会被自动设为默认仓（见 inventory/repository.create_warehouse）
    wh = unwrap(
        client.post(
            "/merchant/warehouses",
            json={"name": "默认仓库", "regionCode": ""},
            headers=merchant_h,
        )
    )
    return wh["id"]


def ensure_stock(client: httpx.Client, merchant_h: dict[str, str], qty: int = 100) -> int:
    """给还没上库存的 SKU 补货。

    ★ 没有库存的商品**下不了单** —— 演示数据里这一步不能省。
    只补 available 为 0 的，重复执行不会把已经卖掉的库存又加回去。

    前置：商家得有仓（见 :func:`ensure_warehouse`）。调 ``/merchant/inventory`` 时
    服务端会**自动为该仓补齐缺记录的 SKU**，所以这里直接列出来就是全量。
    """
    items = unwrap(
        client.get("/merchant/inventory", params={"limit": 100}, headers=merchant_h)
    )["items"]
    filled = 0
    for i, item in enumerate(items):
        if item["available"] > 0:
            continue
        unwrap(
            client.post(
                "/merchant/inventory/adjust",
                json={
                    "skuId": item["skuId"],
                    "warehouseId": item["warehouseId"],
                    "delta": qty,
                    "remark": "演示数据初始化",
                },
                headers={
                    **merchant_h,
                    # 每一条用不同的幂等键，否则第二条会被当成重放而跳过
                    "Idempotency-Key": f"seed-stock-{item['skuId']}-{i}",
                },
            )
        )
        filled += 1
    return filled


def ensure_freight(client: httpx.Client, merchant_h: dict[str, str]) -> int:
    """建一个运费模板并把商家的 SKU 全绑上。

    ★ 不绑模板的商品**下不了单** —— 算不出运费。所以演示数据里这一步不能省。

    模板参数：首重 1000g / 10 元，续重 500g / 3 元，满 99 元包邮。
    """
    templates = unwrap(client.get("/merchant/freight/templates", headers=merchant_h))
    if templates:
        # ★ 已存在时**不能直接返回**：全新库里完全可能出现"模板建好了、但一条 SKU
        #   都没绑"（上一次跑到绑定时才失败）。那种情况下商品算不出运费、下不了单。
        #   复用模板继续往下走 —— 绑定的接口是 upsert，重复执行安全。
        tpl = templates[0]
    else:
        tpl = unwrap(
            client.post(
                "/merchant/freight/templates",
                json={
                    "name": "默认快递模板",
                    "chargeType": 1,
                    "firstUnit": 1000,
                    "firstPrice": 1000,
                    "addUnit": 500,
                    "addPrice": 300,
                    "freeShipping": False,
                    "freeThreshold": 9900,
                    "freeNum": 0,
                    "mergeType": 1,
                },
                headers=merchant_h,
            )
        )
        # 必须有"全国默认"，否则其他地区一条规则都匹配不到
        unwrap(
            client.put(
                f"/merchant/freight/templates/{tpl['id']}/regions",
                json={
                    "rules": [
                        {
                            "regionCode": "0",
                            "regionLevel": 1,
                            "firstUnit": 1000,
                            "firstPrice": 1000,
                            "addUnit": 500,
                            "addPrice": 300,
                            "priority": 0,
                        }
                    ]
                },
                headers=merchant_h,
            )
        )

    # 把商家的 SKU 全绑到这个模板。仓库用默认仓（由 ensure_warehouse 保证存在）
    warehouses = unwrap(client.get("/merchant/warehouses", headers=merchant_h))
    if not warehouses:
        print("没有仓库，跳过运费绑定")
        return 0
    wh_id = warehouses[0]["id"]

    bound = 0
    for item in unwrap(
        client.get("/merchant/inventory", params={"limit": 100}, headers=merchant_h)
    )["items"]:
        unwrap(
            client.post(
                "/merchant/freight/bind",
                json={
                    "skuId": item["skuId"],
                    "templateId": tpl["id"],
                    "warehouseId": wh_id,
                    "priority": 0,
                },
                headers=merchant_h,
            )
        )
        bound += 1
    print(f"运费模板就绪，绑定 {bound} 个 SKU")
    return bound


def build_products(category_ids: dict[str, str]) -> list[dict]:
    """商品规格。**图片留空**，由 :func:`with_images` 在上架前填。

    为什么不在这里直接传图：这个函数会把**所有**演示商品都构造出来，
    而 :func:`main` 只挑不存在的那些去创建。在这里传图的话，每跑一次
    seed 都会给已存在的商品白传一遍图。
    """
    img = None
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


def _spec_caption(spec: dict, sku: dict) -> str:
    """把 SKU 的规格 key 翻成展示文案，如「暗夜黑 · 256G」。"""
    keys = set(sku.get("specValueKeys") or [])
    return " · ".join(
        v["value"]
        for g in spec.get("specGroups", [])
        for v in g.get("values", [])
        if v["key"] in keys
    )


def with_images(spec: dict, client: httpx.Client, headers: dict[str, str]) -> dict:
    """上架前给规格补图：主图用商品名，SKU 封面用「商品名 + 规格」。

    每个 SKU 一张不同的图是有意义的 —— 商城详情页是
    ``currentSku.coverImage || spu.mainImage``，选中规格时会换图，
    这样演示时能直接看到"规格切换 → 图跟着换"。
    """
    out = copy.deepcopy(spec)
    title = out["title"]
    out["mainImage"] = upload_demo_image(client, headers, title=title)
    for sku in out["skus"]:
        sku["coverImage"] = upload_demo_image(
            client, headers, title=title, caption=_spec_caption(out, sku)
        )
    return out


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

        coupon_count = ensure_coupons(client, admin_h)
        print(f"券模板就绪：新建 {coupon_count} 个（已存在的跳过）")

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
        for spec in build_products(category_ids):
            if spec["title"] in existing_titles:
                print(f"跳过（已存在）：{spec['title']}")
                continue

            # 图在这里才生成并上传：上面 continue 掉的商品不会白传一遍
            payload = with_images(spec, client, merchant_h)

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
            print(f"已上架：{payload['title']}（{len(created['skus'])} 个 SKU，含主图与封面）")

        # ---------- 仓库 → 库存 → 运费 ----------
        # ★ 顺序不能反：库存行按 (sku, 仓库) 建，运费绑定也要 warehouseId。
        #   少了仓库这一步，后面两步都会静默地一条不做 —— 商品上架了却买不了。
        ensure_warehouse(client, merchant_h)
        filled = ensure_stock(client, merchant_h)
        print(f"仓库就绪，补库存：{filled} 个 SKU 各 +100 件")
        ensure_freight(client, merchant_h)

        # ---------- 演示买家 ----------
        ensure_buyer(client)
        print("演示买家就绪（含默认收货地址）")

    print()
    print("=" * 56)
    print(f"管理员账号：{ADMIN_PHONE} / {PASSWORD}   （运营后台）")
    print(f"商家账号：  {MERCHANT_PHONE} / {PASSWORD}   （商家后台）")
    print(f"买家账号：  {BUYER_PHONE} / {PASSWORD}   （商城，已带默认收货地址）")
    print(f"本次新上架 {published} 个商品")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
