"""files 模块的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel


class ImageUploadOut(CamelModel):
    """上传结果。

    ``path`` 是**要入库的那个值**（相对路径），不是给 ``<img src>`` 用的 ——
    评价插入时会校验它必须以 ``reviews/{自己的 user_id}/`` 开头（docs/12 §9），
    所以它不能带 ``/media/`` 前缀。

    ``url`` / ``thumbUrl`` 只是给前端渲染用的便利字段。
    """

    path: str = Field(description="相对路径，入库用")
    url: str = Field(description="可直接用于 <img src>")
    thumb_path: str = Field(description="缩略图相对路径")
    thumb_url: str = Field(description="缩略图 URL，列表页用")
    width: int
    height: int
