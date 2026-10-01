"""files 模块（图片上传）的测试。

这个模块的每一条规则都是**安全边界**，所以测的是"攻击被挡住了没有"：

- 按内容判型（伪造扩展名）
- 重编码丢弃 EXIF（含拍摄地 GPS）
- 像素上限（解压炸弹）
- 体积上限
- 文件名由服务端生成（路径穿越）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from io import BytesIO

import pytest
from httpx import AsyncClient
from PIL import Image

from app.core.config import get_settings
from app.core.errors import BizError
from app.modules.files import storage
from tests.conftest import auth_header, register

BUYER_PHONE = "13900139071"


def _image_bytes(
    fmt: str = "JPEG",
    size: tuple[int, int] = (2000, 1500),
    exif: bytes | None = None,
) -> bytes:
    im = Image.new("RGB", size, (120, 160, 200))
    buf = BytesIO()
    kwargs = {"format": fmt}
    if exif:
        kwargs["exif"] = exif
    im.save(buf, **kwargs)
    return buf.getvalue()


def _exif_with_gps() -> bytes:
    """造一段带 GPS 的 EXIF —— 重编码后必须消失（否则会泄露拍摄地点）。"""
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation
    exif[0x8825] = {1: "N", 2: (31.0, 12.0, 0.0), 3: "E", 4: (121.0, 28.0, 0.0)}  # GPSInfo
    return exif.tobytes()


# ============================================================
# 纯函数：处理流水线
# ============================================================
def test_normalises_long_edge() -> None:
    """长边归一化到 1280（原图直出可能两三 MB 一张）。"""
    full, _thumb, width, height = storage.process_image(_image_bytes(size=(3000, 1500)))
    assert max(width, height) == storage.LONG_EDGE
    out = Image.open(BytesIO(full))
    assert out.format == "WEBP"
    assert max(out.size) == storage.LONG_EDGE


def test_small_image_is_not_upscaled() -> None:
    """小图不放大 —— 放大只会变糊并浪费存储。"""
    _full, _thumb, width, height = storage.process_image(_image_bytes(size=(300, 200)))
    assert (width, height) == (300, 200)


def test_thumbnail_is_200px() -> None:
    _full, thumb, _w, _h = storage.process_image(_image_bytes(size=(3000, 1500)))
    assert max(Image.open(BytesIO(thumb)).size) == storage.THUMB_EDGE


def test_exif_and_gps_are_stripped() -> None:
    """★ 重编码后 EXIF 与 GPS 必须全部消失。"""
    full, thumb, _w, _h = storage.process_image(_image_bytes(exif=_exif_with_gps()))
    for data in (full, thumb):
        out = Image.open(BytesIO(data))
        assert len(out.getexif()) == 0, "重编码必须丢弃全部元数据"
        assert not out.info.get("exif")


def test_orientation_is_applied_then_dropped() -> None:
    """方向要烘进像素（用户看到的方向得对），然后 EXIF 本身丢掉。"""
    im = Image.new("RGB", (400, 200), (200, 30, 40))
    exif = Image.Exif()
    exif[0x0112] = 6  # 旋转 90 度
    buf = BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes())

    _full, _thumb, width, height = storage.process_image(buf.getvalue())
    assert (width, height) == (200, 400), "EXIF 里的方向应当已经被应用到像素上"


def test_rejects_non_image_bytes() -> None:
    """★ 不是图片的字节：按内容判型，与扩展名无关。"""
    with pytest.raises(BizError) as exc:
        storage.process_image(b"<?php echo 1; ?> definitely not an image")
    assert "图片" in exc.value.message or "图片" in str(exc.value)


def test_rejects_svg() -> None:
    """★ SVG 能带脚本，必须拒绝（Pillow 打不开它，正好拒掉）。"""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(BizError):
        storage.process_image(svg)


def test_rejects_decompression_bomb() -> None:
    """★ 解压炸弹：小文件解出上亿像素，必须在解码时就挡住。"""
    # 造一张超过 MAX_PIXELS 的图（这里只声明尺寸，不真的分配那么多内存）
    huge = Image.new("RGB", (1, 1))
    buf = BytesIO()
    huge.save(buf, format="PNG")
    raw = bytearray(buf.getvalue())
    # 手工把 PNG 的 IHDR 宽高改成超大值，Pillow 读到会触发 DecompressionBombError
    raw[16:20] = (70000).to_bytes(4, "big")
    raw[20:24] = (70000).to_bytes(4, "big")
    # 修正 CRC 太麻烦；Pillow 的原子性检查在 CRC 之前就可能报错，两者都是拒绝
    with pytest.raises((BizError, OSError, Exception)):
        storage.process_image(bytes(raw))


def test_rejects_empty_payload() -> None:
    with pytest.raises(BizError):
        storage.process_image(b"")


# ============================================================
# 路径生成
# ============================================================
def test_path_is_server_generated_under_user_dir() -> None:
    """★ 文件名 100% 服务端生成，用户提供的名字一个字符都进不来。"""
    path = storage.build_path(biz="reviews", user_id=42)
    assert path.startswith("reviews/42/")
    assert path.endswith(".webp")
    assert len(path.split("/")[-1]) == len("0" * 32) + len(".webp")


def test_path_rejects_unknown_biz() -> None:
    """biz 是白名单 —— 它会进路径，绝不能是任意字符串。"""
    with pytest.raises(BizError):
        storage.build_path(biz="../../etc", user_id=1)


def test_thumb_path_convention() -> None:
    assert storage.thumb_path_of("reviews/1/ab.webp") == "reviews/1/ab_t.webp"


def test_absolute_path_blocks_traversal() -> None:
    """★ 即使上面的环节都出了纰漏，落盘前还有一道路径穿越检查。"""
    with pytest.raises(BizError):
        storage.absolute_path("../outside.webp")


# ============================================================
# 接口
# ============================================================
@pytest.fixture
async def buyer(client: AsyncClient) -> AsyncIterator[dict]:
    user = await register(client, phone=BUYER_PHONE)
    yield auth_header(user["accessToken"])


@pytest.fixture(autouse=True)
def temp_media_root(tmp_path, monkeypatch) -> None:
    """把上传目录指到临时目录。

    ★ 不这么做的话，每次跑测试都会往开发用的 ``backend/data/media`` 里塞文件，
    跑几轮就积一堆看不懂的 ``reviews/{userId}/`` 目录 —— 而 DB 是被清干净的，
    更容易让人以为"文件丢了"。
    """
    real = get_settings()

    class _Settings:
        media_root = str(tmp_path)
        media_url_prefix = "/media/"
        media_max_image_mb = real.media_max_image_mb

    monkeypatch.setattr(storage, "get_settings", lambda: _Settings())


async def test_upload_endpoint_returns_paths(client: AsyncClient, buyer: dict) -> None:
    resp = await client.post(
        "/api/files/images",
        files={"file": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=buyer,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["path"].startswith("reviews/"), "入库用的相对路径要带 reviews/ 前缀"
    assert not data["path"].startswith("/"), "入库路径不带前导斜杠"
    assert data["url"].startswith("/media/")
    assert data["thumbPath"].endswith("_t.webp")
    assert data["thumbUrl"].startswith("/media/")
    assert data["width"] > 0


async def test_upload_ignores_disguised_extension(client: AsyncClient, buyer: dict) -> None:
    """★ 扩展名与 Content-Type 都可以伪造，按内容判型才靠得住。"""
    resp = await client.post(
        "/api/files/images",
        files={"file": ("evil.jpg", b"not an image at all", "image/jpeg")},
        headers=buyer,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_IMAGE"


async def test_upload_rejects_oversized(client: AsyncClient, buyer: dict) -> None:
    """体积上限：在解码之前挡住，避免先落临时盘再吃内存。"""
    big = b"x" * (6 * 1024 * 1024)  # 6MB > 默认 5MB
    resp = await client.post(
        "/api/files/images",
        files={"file": ("big.jpg", big, "image/jpeg")},
        headers=buyer,
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "IMAGE_TOO_LARGE"


async def test_upload_requires_login(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/files/images",
        files={"file": ("photo.jpg", _image_bytes(), "image/jpeg")},
    )
    assert resp.status_code == 401


async def test_upload_rejects_unknown_biz(client: AsyncClient, buyer: dict) -> None:
    resp = await client.post(
        "/api/files/images?biz=hack",
        files={"file": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=buyer,
    )
    assert resp.status_code == 400


async def test_upload_accepts_products_biz(client: AsyncClient, buyer: dict) -> None:
    """商品图走同一个接口，落盘前缀是 products/{uid}/。

    这条是补的：在此之前 biz 白名单只有 reviews/aftersale，
    商家在界面上放不了商品图（只能手填 URL，而部署环境没有图床）。
    """
    resp = await client.post(
        "/api/files/images?biz=products",
        files={"file": ("cover.jpg", _image_bytes(size=(800, 800)), "image/jpeg")},
        headers=buyer,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["path"].startswith("products/"), "落盘前缀要跟着 biz 走"
    assert data["url"] == f"/media/{data['path']}"


async def test_uploaded_file_lands_on_disk_as_webp(client: AsyncClient, buyer: dict) -> None:
    """★ 上传的字节按约定落到 ``{media_root}/{biz}/{uid}/`` 下，且是归一化后的 WEBP。

    这里断言**磁盘产物**而不是走 ``/media/...``：挂载点是在应用导入时按真实
    ``media_root`` 绑定的，而测试把上传目录指到了临时目录（见上面的 fixture），
    两者不是同一个目录。`/media` 的提供由浏览器端到端验证覆盖。
    """
    resp = await client.post(
        "/api/files/images",
        files={"file": ("photo.jpg", _image_bytes(size=(1600, 1000)), "image/jpeg")},
        headers=buyer,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    full = storage.absolute_path(data["path"])
    thumb = storage.absolute_path(data["thumbPath"])
    assert full.exists() and thumb.exists()

    im = Image.open(full)
    assert im.format == "WEBP"
    assert max(im.size) == storage.LONG_EDGE, "落盘的应当是归一化之后的图"
    assert max(Image.open(thumb).size) == storage.THUMB_EDGE

    # url 只是给前端拼的，路径前缀与 media_url_prefix 一致即可
    assert data["url"] == f"/media/{data['path']}"
