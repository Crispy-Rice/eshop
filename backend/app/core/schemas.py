"""Pydantic 公共基类与自定义标量类型。

对接约定见 docs/15-api-and-errors.md §1：
- 字段名 Python 侧 snake_case，JSON 侧 camelCase（自动转换）
- 雪花 ID 在 JSON 里是**字符串**（超过 JS 的 2^53，用 number 会丢精度）
- 金额在 JSON 里是**整数分**，且只接受整数（strict）
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer
from pydantic.alias_generators import to_camel


def _to_int_if_digit(v: object) -> object:
    """入参兼容字符串形式的雪花 ID，出参一律序列化成字符串。"""
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return v


# 雪花 ID：接受 "123" 或 123，输出永远是 "123"
SnowflakeId = Annotated[
    int,
    BeforeValidator(_to_int_if_digit),
    PlainSerializer(str, return_type=str, when_used="json"),
]

# 金额（分）：非负整数，拒绝浮点与字符串，避免 12.5 这种值混进来
Fen = Annotated[int, Field(ge=0, strict=True)]

# 数量：1..200，对应 carts/order 的输入约束
Quantity = Annotated[int, Field(ge=1, le=200, strict=True)]


class CamelModel(BaseModel):
    """所有请求/响应模型的基类。

    - 出参按 alias（camelCase）序列化：FastAPI 的 response_model_by_alias 默认为 True
    - 入参两种写法都接受（populate_by_name）
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="forbid",  # 多传字段直接报错，避免前端拼错字段名却静默忽略
    )
