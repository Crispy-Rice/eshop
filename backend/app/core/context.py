"""请求级上下文（ContextVar）。

用 ContextVar 而不是全局变量：asyncio 下每个请求是独立的 Task，
ContextVar 天然按 Task 隔离，并发请求之间不会串。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def current_request_id() -> str:
    """当前请求的 X-Request-Id，用于日志串联与错误响应。"""
    return request_id_var.get()


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """已认证用户。买家、商家、运营都从这里取身份。"""

    id: int
    role: str
    shop_id: int | None = None

    def is_merchant(self) -> bool:
        return self.role in {"merchant", "admin"}

    def is_admin(self) -> bool:
        return self.role == "admin"
