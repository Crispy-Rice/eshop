"""files 模块的 HTTP 路由。

只有一个接口：上传单张图片。**一次一张** —— 前端并发上传多张，单张失败可单独重试；
批量在"第 3 张非法"时会产生部分成功的语义麻烦。
"""

from __future__ import annotations

from typing import Annotated

import anyio
from fastapi import APIRouter, File, Query, UploadFile

from app.core.config import get_settings
from app.core.deps import CurrentUserDep
from app.core.errors import BizError, ErrorCode
from app.core.response import ApiResponse
from app.modules.files import storage
from app.modules.files.schemas import ImageUploadOut

router = APIRouter()


@router.post(
    "/api/files/images",
    response_model=ApiResponse[ImageUploadOut],
    summary="上传图片（评价、售后凭证共用）",
)
async def upload_image(
    user: CurrentUserDep,
    file: Annotated[UploadFile, File()],
    biz: str = Query(default="reviews", description="业务线：reviews / aftersale / products / shops"),
) -> ApiResponse[ImageUploadOut]:
    """上传单张图片。

    安全要点（docs/12 §10）：
    按内容判型（忽略扩展名与 Content-Type）、重编码成全新 WEBP（丢弃 EXIF 与任何
    尾部载荷）、限制体积与像素、文件名由服务端生成。
    """
    settings = get_settings()
    max_bytes = settings.media_max_image_mb * 1024 * 1024

    # 先限量读，在解码之前挡住超大文件 —— 否则会先落临时盘再吃内存
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise BizError(
            ErrorCode.IMAGE_TOO_LARGE, f"图片不能超过 {settings.media_max_image_mb}MB"
        )

    # ★ 解码与重编码是同步 CPU 操作，必须丢到线程池，不能阻塞事件循环
    rendered = await anyio.to_thread.run_sync(storage.process_image, raw)

    path = storage.build_path(biz=biz, user_id=user.id)
    await anyio.to_thread.run_sync(storage.save_image, path, rendered)

    prefix = settings.media_url_prefix
    mid_path = storage.mid_path_of(path)
    thumb_path = storage.thumb_path_of(path)
    return ApiResponse.ok(
        ImageUploadOut(
            path=path,
            url=f"{prefix}{path}",
            mid_path=mid_path,
            mid_url=f"{prefix}{mid_path}",
            thumb_path=thumb_path,
            thumb_url=f"{prefix}{thumb_path}",
            width=rendered.width,
            height=rendered.height,
        )
    )
