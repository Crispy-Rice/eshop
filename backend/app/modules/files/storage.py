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

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import get_settings
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

# 允许的图片格式（Pillow 嗅探出来的格式名，不是扩展名）
ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})

# 长边归一化到这个尺寸。**这一步替代了缩略图**的主要作用：
# 原图直出可能一张 3MB，归一化 + WEBP 后通常 50~150KB
LONG_EDGE = 1280
THUMB_EDGE = 200
# 缩略图的后缀约定。库里只存原图路径，缩略图路径由这个约定推导
THUMB_SUFFIX = "_t"

# WEBP 编码质量
QUALITY = 82

# 解压炸弹防线：单张最多解出 4000 万像素。Pillow 默认约 1.79 亿，偏松
MAX_PIXELS = 40_000_000

# 允许上传的业务线（白名单）。**绝不能是任意字符串** —— 它会进路径
#
# products 是商品主图与 SKU 封面（商家上传）。在此之前商品图只能靠手填 URL，
# 而部署环境是 IP 直连、没有图床 —— 等于商家放不了真图。
ALLOWED_BIZ = frozenset({"reviews", "aftersale", "products"})

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


def process_image(raw: bytes) -> tuple[bytes, bytes, int, int]:
    """把用户上传的字节转成 (原图 webp, 缩略图 webp, 宽, 高)。

    **纯函数**：不落盘、不读库，便于单测。

    返回的宽高是**归一化之后**的尺寸 —— 上报原始尺寸会让前端按错误的宽高留位，
    在图片加载完成时抖动。
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

    # 只归一化一次，缩略图从归一化后的图再缩 —— 尺寸与产物保持一致
    fitted = _fit(im, LONG_EDGE)
    return _encode(fitted), _encode(_fit(fitted, THUMB_EDGE)), fitted.width, fitted.height


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


def thumb_path_of(path: str) -> str:
    """由原图路径推出缩略图路径（``ab.webp`` → ``ab_t.webp``）。

    库里只存原图路径（``review.review.images`` 是字符串数组，docs/12 §2.1），
    缩略图路径按这个约定推导 —— 单一来源，不用多存一列。
    """
    base, _, ext = path.rpartition(".")
    return f"{base}{THUMB_SUFFIX}.{ext}"


def absolute_path(relative: str) -> Path:
    """相对路径 → 磁盘绝对路径。**只接受我们自己生成的形态**。"""
    root = Path(get_settings().media_root).resolve()
    target = (root / relative).resolve()
    # 双保险：即使上面哪里出了纰漏，也不允许写到 media_root 之外
    if not target.is_relative_to(root):
        raise BizError(ErrorCode.VALIDATION_ERROR, "非法的文件路径")
    return target


def save_image(relative: str, full: bytes, thumb: bytes) -> None:
    """落盘原图与缩略图。父目录不存在就建。"""
    target = absolute_path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(full)
    absolute_path(thumb_path_of(relative)).write_bytes(thumb)
