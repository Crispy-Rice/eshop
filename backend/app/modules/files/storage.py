"""图片存储与处理。**纯函数 + 文件系统，不碰数据库。**

对应 docs/12-review.md §10 的"图片上传安全要点"与 docs/15 §4 的约束。
评价与售后凭证共用这套能力，所以它不属于任何一个业务模块。

**安全模型：用户提供的字节从不落盘。** 一律全解码 → 重新编码成全新的 WEBP →
只写我们自己生成的那份。这一条同时解决四类问题：

- **多态文件**（图片尾部拼接 JS/PHP/SVG 脚本）：原字节里的任何尾巴都随重编码消失
- **EXIF 泄露**：重编码丢弃全部元数据（`exif_transpose` 只把方向烘进像素）
- **伪造类型**：按内容嗅探真实格式（`Image.open` 读魔数），不看扩展名也不看
  `Content-Type`
- **路径穿越**：文件名 100% 由服务端生成（uuid4），用户输入不进路径

解码是 CPU 密集的同步调用，路由层必须用 ``anyio.to_thread`` 把它丢到线程池，
否则会阻塞事件循环。
"""

from __future__ import annotations

import uuid
from io import BytesIO
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import get_settings
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

# 允许的图片格式（Pillow 嗅探出来的格式名，不是扩展名）
ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})

# 三档尺寸，按"消费场景"分，不按"好看"分：
#
#   full  1280  详情页大图、店铺 LOGO、轮播图、头像
#   mid    640  商城商品卡片（卡片宽 200~260，2x 屏要 ~520，640 刚好不糊又不浪费）
#   thumb  200  评价九宫格、订单行、售后凭证这类小缩略
#
# ★ 加 mid 的原因是：原来卡片直接吃 1280 的 full，而卡片只有 200~260 CSS px ——
#   多送 5~6 倍像素，用户一个都看不到。生成图时代看不出来（平色渐变才 3KB），
#   换成真实照片后一张就是 100~300KB，12 张卡片就是一两个 MB。
LONG_EDGE = 1280
MID_EDGE = 640
THUMB_EDGE = 200
# 派生图的后缀约定。库里只存原图路径，另外两档由这个约定推导（见 thumb_path_of）
THUMB_SUFFIX = "_t"
MID_SUFFIX = "_m"

# WEBP 编码质量
QUALITY = 82

# 解压炸弹防线：单张最多解出 4000 万像素。Pillow 默认约 1.79 亿，偏松
MAX_PIXELS = 40_000_000

# 允许上传的业务线（白名单）。**绝不能是任意字符串** —— 它会进路径
#
# products 是商品主图与 SKU 封面（商家上传）。在此之前商品图只能靠手填 URL，
# 而部署环境是 IP 直连、没有图床 —— 等于商家放不了真图。
#
# shops 是店铺 LOGO（商家上传，店铺设置页）。和 products 一样存的是完整 url，
# 因为它在商品详情页/列表卡片上直接当 <img src> 用。
#
# avatars 是用户头像（商城与后台的「个人资料」共用同一张）。同样存完整 url ——
# 它在导航栏、账号页、评价区都直接当 <img src> 用。
#
# banners 是首页轮播图（平台运营上传）。存完整 url，商城主页直接当 <img src> 用。
ALLOWED_BIZ = frozenset({"reviews", "aftersale", "products", "shops", "avatars", "banners"})

# Pillow 的全局保护：模块级设置一次。
# 注意不要设成 None —— 那等于把炸弹保护整个关掉
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def _fit(im: Image.Image, long_edge: int) -> Image.Image:
    """按长边等比缩放。**只缩不放** —— 小图放大只会变糊并浪费存储。"""
    if max(im.size) <= long_edge:
        return im
    out = im.copy()
    out.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    return out


def _encode(im: Image.Image) -> bytes:
    """编码成全新的 WEBP 字节流。

    ``exif=`` 一个字节都不传 —— 这正是不泄露拍摄地 GPS 的原因。
    """
    buf = BytesIO()
    im.save(buf, format="WEBP", quality=QUALITY, method=4)
    return buf.getvalue()


def encode_webp(im: Image.Image, long_edge: int) -> bytes:
    """按长边归一化后编码。"""
    return _encode(_fit(im, long_edge))


class Rendered(NamedTuple):
    """一次解码产出的三档字节。**三份都从同一张归一化后的图缩出来**，尺寸才自洽。"""

    full: bytes
    mid: bytes
    thumb: bytes
    width: int
    height: int


def process_image(raw: bytes) -> Rendered:
    """把用户上传的字节转成 full / mid / thumb 三档 webp。

    **纯函数**：不落盘、不读库，便于单测。

    ``width`` / ``height`` 是**归一化之后**的尺寸 —— 上报原始尺寸会让前端按错误的
    宽高留位，在图片加载完成时抖动。
    """
    if not raw:
        raise BizError(ErrorCode.INVALID_IMAGE, "文件为空")

    try:
        im = Image.open(BytesIO(raw))
        fmt = im.format
        # 先把 EXIF 里的方向烘进像素，这样用户看到的方向是对的；
        # 然后 im.load() 真正解码 —— 解压炸弹与截断文件都在这一步暴露
        im = ImageOps.exif_transpose(im)
        im.load()
    except Image.DecompressionBombError as exc:
        logger.warning("拒绝解压炸弹图片", extra={"bytes": len(raw)})
        raise BizError(ErrorCode.INVALID_IMAGE, "图片尺寸过大") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise BizError(ErrorCode.INVALID_IMAGE, "不是有效的图片文件") from exc

    if fmt not in ALLOWED_FORMATS:
        raise BizError(
            ErrorCode.INVALID_IMAGE, f"仅支持 {'/'.join(sorted(ALLOWED_FORMATS))} 格式的图片"
        )

    # 统一转 RGB/RGBA 再编码：P 模式（调色板）与 CMYK 直接存 WEBP 会有色偏
    if im.mode in ("P", "CMYK", "LA"):
        im = im.convert("RGBA" if "A" in im.mode or im.mode == "P" else "RGB")

    # 只归一化一次，三档都从归一化后的图再缩 —— 尺寸与产物保持一致
    fitted = _fit(im, LONG_EDGE)
    return Rendered(
        full=_encode(fitted),
        mid=_encode(_fit(fitted, MID_EDGE)),
        thumb=_encode(_fit(fitted, THUMB_EDGE)),
        width=fitted.width,
        height=fitted.height,
    )


def build_path(*, biz: str, user_id: int, now_suffix: str | None = None) -> str:
    """生成相对路径 ``{biz}/{user_id}/{uuid}.webp``。

    ★ 文件名由服务端生成，用户提供的原文件名被**丢弃** —— 路径穿越的前提是
    用户能影响路径，这里让他一个字符都进不来。
    按 user_id 分目录是为了避免单目录堆几十万文件。
    """
    if biz not in ALLOWED_BIZ:
        raise BizError(ErrorCode.VALIDATION_ERROR, "不支持的上传类型")
    name = now_suffix or uuid.uuid4().hex
    return f"{biz}/{user_id}/{name}.webp"


def _derive(path: str, suffix: str) -> str:
    """``ab.webp`` + ``_t`` → ``ab_t.webp``。三档路径全靠这一个约定，别各写各的。"""
    base, _, ext = path.rpartition(".")
    return f"{base}{suffix}.{ext}"


def thumb_path_of(path: str) -> str:
    """由原图路径推出 200 缩略图的路径。

    库里只存原图路径（``review.review.images`` 是字符串数组，docs/12 §2.1），
    派生图路径按后缀约定推导 —— 单一来源，不用多存两三列。
    """
    return _derive(path, THUMB_SUFFIX)


def mid_path_of(path: str) -> str:
    """由原图路径推出 640 中间档的路径。商品卡片用它。"""
    return _derive(path, MID_SUFFIX)


def _derive_url(url: str, suffix: str) -> str:
    """url 版的 ``_derive``，**只动我们自己生成的 media url**。

    空值、外链原样返回。理由：推导规则是"在扩展名前插后缀"，它对任何字符串都
    "能算出一个结果"，但对 ``https://cdn.example.com/a.jpg`` 算出来的地址必然 404。
    与其给前端一个坏链接再靠回退兜，不如在这里就不改。

    （商品主图正常都走上传接口，理论上都是 media url；这一层挡的是历史脏数据和
    将来可能出现的"填外链"用法。）
    """
    if not url.startswith(get_settings().media_url_prefix):
        return url
    return _derive(url, suffix)


def thumb_url_of(url: str) -> str:
    """由原图 **url** 推出 200 缩略的 url。

    ``products`` / ``shops`` / ``avatars`` / ``banners`` 这几条业务线库里存的是
    **完整 url**（直接用 ``<img src>``），不是相对路径。推导规则和路径版完全一样
    —— 都是"在扩展名前插后缀" —— 单独给个名字只是让调用点读起来不别扭。
    """
    return _derive_url(url, THUMB_SUFFIX)


def mid_url_of(url: str) -> str:
    """由原图 **url** 推出 640 中间档的 url。见 ``thumb_url_of``。"""
    return _derive_url(url, MID_SUFFIX)


def absolute_path(relative: str) -> Path:
    """相对路径 → 磁盘绝对路径。**只接受我们自己生成的形态**。"""
    root = Path(get_settings().media_root).resolve()
    target = (root / relative).resolve()
    # 双保险：即使上面哪里出了纰漏，也不允许写到 media_root 之外
    if not target.is_relative_to(root):
        raise BizError(ErrorCode.VALIDATION_ERROR, "非法的文件路径")
    return target


def save_image(relative: str, rendered: Rendered) -> None:
    """落盘三档。父目录不存在就建。

    ★ **幂等**：同一个 ``relative`` 重复写会覆盖同名文件。回填脚本正是靠这一点，
      对已有原图重跑一遍就能把缺的 ``_m`` / ``_t`` 补齐。
    """
    target = absolute_path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(rendered.full)
    absolute_path(mid_path_of(relative)).write_bytes(rendered.mid)
    absolute_path(thumb_path_of(relative)).write_bytes(rendered.thumb)
