"""从 Pixabay 拉演示素材：商品图落到 ``demo_assets/products/``，
轮播图落到 ``demo_assets/banners/``（**下载时裁成 10:3**）。

**这是一次性的选型工具，不在部署或 seed 时运行。** 只有在想换图时才需要重跑。

为什么要留脚本而不是直接把图扔进仓库：

- **可复现** —— 图是哪张（Pixabay 的 photo id）、谁拍的、来源页在哪，都写在这儿
- **可审计** —— 顺带生成 ``ATTRIBUTION.md``，逐张留档
- **可替换** —— 换图只改 ``PICKS`` / ``BANNER_PICKS``，不用手工重下

★ 认的是 **photo id，不是搜索词**。Pixabay 的 ``order=popular`` 排序会随时间浮动，
按"词 + 第几张"取等于把结果交给运气 —— 重跑一次可能换一张完全不同的图。
这 30 个 id 是人眼从联系表里挑出来的，钉死才算选型。

许可：Pixabay Content License（免费商用、无需署名）。严格说不是 CC0，
但演示用途没问题；``ATTRIBUTION.md`` 仍逐张留了出处。

key 不落仓库、不进环境变量 —— 从 ``%TEMP%/pixabay_key.txt`` 读，用完即弃。

    uv run python scripts/fetch_demo_images.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.parse
from io import BytesIO
from pathlib import Path

from PIL import Image

# ★ 为什么用 curl 而不是 urllib / httpx：
#   这台机器上 Python 的 TLS 握手连 pixabay 会超时（`_ssl.c:1063 handshake timed out`），
#   而 curl 一直是通的。试过 urllib 之后换的，不是随手选的。
#   命令全部是字面量，url 只进参数位、不过 shell，所以下面几处 S603/S607 是误报。
HERE = Path(__file__).resolve().parent
ASSETS = HERE / "demo_assets" / "products"
BANNERS = HERE / "demo_assets" / "banners"
API = "https://pixabay.com/api/"

# 落盘尺寸，与上传管道一致（storage.LONG_EDGE）—— 传上去只做一次重编码
LONG_EDGE = 1280
QUALITY = 82

# 轮播图是 10:3 的宽幅（商城首页整幅横幅）。和商品图不一样：
# ★ 它**要在下载时裁好**，不能指望 CSS。卡片是 1:1 居中裁切，横幅是 10:3 ——
#   拿一张普通横图直接用，`object-fit: cover` 会拦腰截掉大半，
#   挑图时必须按这个比例先看一眼（见 BANNER_PICKS 的注释）。
BANNER_SIZE = (1200, 360)

# 轮播图 slug -> Pixabay photo id
#   new        1868496   白桌面 + 一体机，干净，配「新品首发」
#   digital    2982270   笔记本/耳机/相机的平铺，配「数码好物」
#   freeship   4543999   一排牛皮纸购物袋，配「全场包邮」
BANNER_PICKS: dict[str, int] = {
    "new": 1868496,
    "digital": 2982270,
    "freeship": 4543999,
}

# slug -> Pixabay photo id
#
# 挑图时的检索词与取舍记在这里，方便日后想换图时有个出发点：
#   hoodie       hoodie sweatshirt / fashion     白底卫衣
#   nuts         nuts snack bowl / food          白底杏仁碗
#   dumbbell     dumbbell fitness / sports       影棚哑铃
#   fridge       refrigerator（**不带分类**）     人在冰箱前，货架清楚
#   desktop      desktop computer pc / computer  一体机 + 桌面
#   novel        books library / education         书堆占满画面（原 5937823 主体偏右，
#                                                    居中裁切后卡片里只剩白底，换掉）
#   phonecase    smartphone case / computer      白底手机壳
#   textbook     textbook study / education      书脊特写
#   iphone       smartphone / computer           手持手机主屏
#   xiaomi14     mobile phone android / computer 手持手机
#   xiaomi15     smartphone screen / computer    桌面手机
#   picturebook  children book / education       彩色童书一摞
#   laptop       laptop notebook / computer      白桌面笔记本电脑
#   headphones   headphones / computer           白底头戴耳机
#   runningshoes running shoes / sports          白底跑鞋
#   dress        dress fashion / fashion         碎花连衣裙
#   speaker      bluetooth speaker / computer    户外便携音箱
#   coffee       coffee beans / food             咖啡豆与杯
#   cardigan     cardigan knit / fashion         针织开衫扣子特写
#   shirtdress   elegant dress woman / fashion   模特全身裙装
#   cookies      cookies biscuits / food         石板上的曲奇
#   greentea     green tea leaves / food         散装茶叶
#   canvasshoes  sneakers shoes / fashion        街拍白球鞋
#   yogamat      yoga mat / sports               成摞瑜伽垫
#   classics     old books vintage / education   旧皮面书
#   popupbook    open book pages / education     翻开的书
#   vocab        dictionary book / education     词典与读者
#   microwave    microwave oven（**不带分类**）   白底银色微波炉
#   washer       washing machine（**不带分类**）  深底滚筒洗衣机
#   ricecooker   rice bowl cooked（**不带分类**） 一碗白米饭（见下）
#
# ★ 家电三个槽位的分类是**故意留空**的：Pixabay 的 `industry` 是工业机械，
#   搜 "microwave oven" 会返回面包和教堂，去掉分类才搜得到家电本体。
#
# ★ 电饭煲：Pixabay 上换 6 个词全是稻田，没有可用的"电饭煲"照片。
#   退而用一碗白米饭 —— 商城拿食物图当厨电主图是常见做法，总比塞一张稻田强。
PICKS: dict[str, int] = {
    "hoodie": 2182849,
    "nuts": 1740176,
    "dumbbell": 940375,
    "fridge": 8614127,
    "desktop": 820390,
    "novel": 1246674,
    "phonecase": 2562333,
    "textbook": 5077895,
    "iphone": 410311,
    "xiaomi14": 1814556,
    "xiaomi15": 1875813,
    "picturebook": 1246675,
    "laptop": 3139127,
    "headphones": 3661771,
    "runningshoes": 371625,
    "dress": 5679284,
    "speaker": 5476085,
    "coffee": 1576537,
    "cardigan": 1617328,
    "shirtdress": 6115105,
    "cookies": 1886760,
    "greentea": 1797125,
    "canvasshoes": 5226091,
    "yogamat": 4650150,
    "classics": 436498,
    "popupbook": 1100254,
    "vocab": 2771936,
    "microwave": 5144884,
    "washer": 1786385,
    "ricecooker": 10370575,
}


def read_key() -> str:
    """从临时目录读 key。**不写进仓库、不打日志。**"""
    p = Path(tempfile.gettempdir()) / "pixabay_key.txt"
    if not p.exists():
        sys.exit(f"找不到 {p} —— 把 Pixabay 的 key 写进这个文件再跑")
    return p.read_text(encoding="utf-8").strip()


def fetch_by_id(key: str, photo_id: int) -> dict:
    """按 id 取一张。失败重试 1 次，不做长退避。"""
    url = API + "?" + urllib.parse.urlencode({"key": key, "id": str(photo_id)})
    for attempt in (1, 2):
        r = subprocess.run(  # noqa: S603
            ["curl", "-s", "-m", "20", url],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if r.stdout.strip().startswith("{"):
            hits = json.loads(r.stdout).get("hits", [])
            if hits:
                return hits[0]
            raise RuntimeError(f"id={photo_id} 查不到（可能已下架）")
        if attempt == 1:
            time.sleep(1)
    raise RuntimeError(f"id={photo_id} 两次请求都没返回 JSON")


def download(url: str) -> bytes:
    r = subprocess.run(["curl", "-s", "-m", "60", "-L", url], capture_output=True)  # noqa: S603,S607
    if r.returncode != 0 or len(r.stdout) < 3000:
        raise RuntimeError(f"下载失败（{len(r.stdout)} 字节）")
    return r.stdout


def to_webp(raw: bytes) -> bytes:
    """商品图：长边归一化。宽高比不动，卡片那边 CSS 自己居中裁成 1:1。"""
    im = Image.open(BytesIO(raw)).convert("RGB")
    if max(im.size) > LONG_EDGE:
        im.thumbnail((LONG_EDGE, LONG_EDGE), Image.Resampling.LANCZOS)
    out = BytesIO()
    im.save(out, "WEBP", quality=QUALITY, method=4)
    return out.getvalue()


def to_banner_webp(raw: bytes) -> bytes:
    """轮播图：**下载时就裁成 10:3**。

    不交给 CSS 是因为横幅和卡片不一样：卡片 1:1，居中裁掉的多半是背景；
    横幅 10:3，拿一张 3:2 的图进去 `object-fit: cover` 会拦腰截掉一半，
    主体很容易被切没。在这里裁，挑图时看到的就是上线的样子。
    """
    im = Image.open(BytesIO(raw)).convert("RGB")
    w, h = im.size
    target = BANNER_SIZE[0] / BANNER_SIZE[1]
    if w / h > target:  # 太宽 → 裁两侧
        nw = int(h * target)
        left = (w - nw) // 2
        im = im.crop((left, 0, left + nw, h))
    else:  # 太窄 → 裁上下，取中线**偏上**（横幅主体通常在偏上；0.35 会把袋口这类顶部切掉）
        nh = int(w / target)
        top = max(0, int((h - nh) * 0.22))
        im = im.crop((0, top, w, top + nh))
    im = im.resize(BANNER_SIZE, Image.Resampling.LANCZOS)
    out = BytesIO()
    im.save(out, "WEBP", quality=QUALITY, method=4)
    return out.getvalue()


def fetch_set(
    key: str,
    picks: dict[str, int],
    out_dir: Path,
    transform,
    label: str,
) -> list[tuple[str, str, str, str]]:
    """拉一批图，返回 ``(slug, 相对文件, 作者, 来源页)``。

    ★ 元数据**总是**要取，哪怕文件已存在。第一次跑如果中断在"写盘之后、记录之前"，
      之后每次都会被"已存在"跳过，那一张的出处就永远补不上（hoodie 丢过一次）。
      取元数据是几十字节的请求，比事后手工补便宜得多。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, str, str, str]] = []
    rel = out_dir.name  # products / banners

    for i, (slug, photo_id) in enumerate(picks.items(), 1):
        dest = out_dir / f"{slug}.webp"
        try:
            hit = fetch_by_id(key, photo_id)
        except Exception as exc:
            print(f"[{label} {i:2}/{len(picks)}] {slug:12} FAIL {exc}")
            continue

        if dest.exists():
            print(f"[{label} {i:2}/{len(picks)}] {slug:12} 已存在，只补记录")
        else:
            try:
                dest.write_bytes(transform(download(hit["largeImageURL"])))
            except Exception as exc:
                print(f"[{label} {i:2}/{len(picks)}] {slug:12} FAIL {exc}")
                continue
            print(
                f"[{label} {i:2}/{len(picks)}] {slug:12} ok "
                f"{dest.stat().st_size / 1024:6.1f} KB  {hit.get('user', '')}"
            )

        rows.append((slug, f"{rel}/{dest.name}", hit.get("user", "?"), hit.get("pageURL", "")))
        time.sleep(0.3)
    return rows


def write_attribution(rows: list[tuple[str, str, str, str]]) -> None:
    """逐张记出处。Pixabay 不强制署名，但留档比丢了好。

    ★ **整体重写**，不做追加。每次跑完 rows 就是全量（下载或跳过都产生一行）；
      再叠加旧内容只会写出重复行。按 slug 排序，输出才稳定、diff 才干净。
    """
    body = "".join(f"| `{s}` | `{f}` | {u} | {p} |\n" for s, f, u, p in sorted(rows))
    (ASSETS.parent / "ATTRIBUTION.md").write_text(
        "# 演示素材来源\n\n"
        "来源 [Pixabay](https://pixabay.com/)，适用 **Pixabay Content License**\n"
        "（免费商用、无需署名）。此处仍逐张留档以便追溯。\n\n"
        "由 `backend/scripts/fetch_demo_images.py` 生成；**不要在部署流程里运行它**。\n\n"
        "| slug | 文件 | 作者 | 来源页 |\n|---|---|---|---|\n" + body,
        encoding="utf-8",
    )


def main() -> None:
    key = read_key()
    rows = fetch_set(key, PICKS, ASSETS, to_webp, "商品")
    rows += fetch_set(key, BANNER_PICKS, BANNERS, to_banner_webp, "轮播")
    write_attribution(rows)

    for d in (ASSETS, BANNERS):
        files = sorted(d.glob("*.webp"))
        total = sum(p.stat().st_size for p in files)
        print(f"\n{d.name}: {len(files)} 张，合计 {total / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
