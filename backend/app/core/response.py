"""统一响应包装。见 docs/15-api-and-errors.md §1.2。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import Field

from app.core.context import current_request_id
from app.core.schemas import CamelModel

T = TypeVar("T")


class ApiResponse(CamelModel, Generic[T]):
    code: str = "OK"
    message: str = "success"
    data: T | None = None
    request_id: str = Field(default_factory=current_request_id)

    @classmethod
    def ok(cls, data: T) -> ApiResponse[T]:
        return cls(data=data)

    @classmethod
    def fail(cls, code: str, message: str, data: object | None = None) -> ApiResponse[None]:
        return cls(code=code, message=message, data=data)  # type: ignore[arg-type]


class PageMeta(CamelModel):
    """游标分页元信息。

    一律用游标分页，不用 OFFSET —— 深分页时 OFFSET 会扫过前面所有行
    （docs/12-review.md §10）。
    """

    has_more: bool
    # 下一页请求时原样带回来；为空表示没有下一页
    next_cursor: str | None = None


class Page(CamelModel, Generic[T]):
    items: list[T]
    meta: PageMeta
