"""LLM 客户端 —— **本模块唯一碰 httpx 的地方**，见 docs/20-assistant.md。

走阿里云百炼的 **OpenAI 兼容模式**（``/compatible-mode/v1``），协议就是
``chat/completions``。于是只用 ``httpx``（本来就是依赖）+ JSON 就够了，
**不引入任何厂商 SDK** —— 少一个依赖、少一次版本升级的牵扯。

★ **协议边界只到这里**：``QwenClient`` 把厂商响应翻译成 ``ChatReply``，
  上层（``service.py`` 的循环）只认 ``ChatReply``。换供应商 = 换一个实现类，
  不动循环、不动工具、不动提示词。

★ **不做自动重试**。重试放在 service 的受控逻辑里：那里知道"这一轮是第几轮、
  已经花了多少 token、还剩多少时间预算"，而这一层什么都不知道，盲目重放只会
  把成本和延迟乘起来（参考 worker 侧给的 ``max_tries=1`` 是同一个理由）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.config import Settings

logger = get_logger(__name__)

# 连接超时给 5 秒：连不上就不必等读超时那么久
CONNECT_TIMEOUT_SECONDS = 5.0

# 一次请求（含工具定义）大概多大，用于日志里估算，不参与逻辑
_MAX_LOGGED_BODY = 2000


class LlmError(Exception):
    """上游调用失败。

    ``timeout`` 单独标出来，因为**用户能做的事不一样**：超时值得重试，
    而 4xx（密钥错、余额不足、模型名写错）重试多少次都一样。
    """

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型要求调用某个工具。

    ``arguments`` **保持模型给的原始字符串**，不在这里解析 —— 模型产出非法 JSON
    是常态，解析与丢弃的责任在 ``rules.parse_tool_args``（那里能给出可读的失败原因，
    也能顺手把多余的键丢掉）。
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ChatReply:
    """一次模型回复。``content`` 与 ``tool_calls`` 可能同时有值。"""

    content: str | None
    tool_calls: tuple[ToolCall, ...]
    prompt_tokens: int
    completion_tokens: int
    model: str


class LlmClient(Protocol):
    """上层依赖的是这个协议，不是具体实现 —— 测试注入脚本化的假客户端。"""

    async def chat(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> ChatReply: ...


def build_payload(
    settings: Settings,
    *,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
    tool_choice: str | None,
) -> dict[str, Any]:
    """组装 ``/chat/completions`` 的请求体。**纯函数**，便于单测钉住字段。

    ★ ``enable_thinking`` 是百炼在 OpenAI 协议之外的扩展字段（官方示例写作
      ``extra_body={"enable_thinking": ...}``），走原始 HTTP 时就是**顶层一个键**。
      默认关：助手要的是"查数再答"，不需要长链推理；开着还会多花 output token、
      多等几秒，而且思考模式下**不能强制 required 工具** —— 而我们最后一轮要
      用 ``tool_choice="none"`` 收尾。
    """
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": list(messages),
        "max_tokens": settings.assistant_max_output_tokens,
        "enable_thinking": settings.llm_enable_thinking,
    }
    if tools:
        payload["tools"] = list(tools)
        # 最后一轮会被上层强制成 "none"：让模型必须用已有信息作答，不再调工具
        payload["tool_choice"] = tool_choice or "auto"
    return payload


class QwenClient:
    """百炼（OpenAI 兼容）。

    ★ **每次调用新建一个 ``AsyncClient``**，不维护进程级单例。代价是一次 TLS
      握手（几十毫秒），而一次模型调用是几秒到几十秒 —— 换来的是不必新增一个
      "谁来关它"的生命周期问题（项目的 ``get_redis`` / ``dispose_engine`` 那套
      显式关闭是必须的，能不加就不加）。
    """

    async def chat(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> ChatReply:
        settings = get_settings()
        payload = build_payload(
            settings, messages=messages, tools=tools, tool_choice=tool_choice
        )

        timeout = httpx.Timeout(settings.llm_timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise LlmError(f"模型接口超时：{exc}", timeout=True) from exc
        except httpx.HTTPError as exc:
            raise LlmError(f"模型接口不可达：{exc}") from exc

        if resp.status_code >= 400:
            # 4xx 里的正文往往直接写着原因（模型名错 / 余额不足），留着便于排查，
            # 但只截一段 —— 上游可能回一整页 HTML
            raise LlmError(f"模型接口返回 {resp.status_code}：{resp.text[:_MAX_LOGGED_BODY]}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise LlmError("模型接口返回的不是 JSON") from exc

        return _parse_reply(body)


def _parse_reply(body: dict[str, Any]) -> ChatReply:
    """把 OpenAI 兼容响应翻译成 ``ChatReply``。

    ★ **防御式取字段**：兼容层与官方的形状偶有出入，缺字段时给默认值而不是抛
      ``KeyError`` —— 一次解析崩掉会让整条回答失败，而这些字段缺了并不影响回答。
    """
    choices = body.get("choices") or []
    if not choices:
        raise LlmError("模型接口没有返回任何候选")

    message = choices[0].get("message") or {}
    raw_calls = message.get("tool_calls") or []
    calls = tuple(
        ToolCall(
            id=str(call.get("id") or ""),
            name=str((call.get("function") or {}).get("name") or ""),
            arguments=str((call.get("function") or {}).get("arguments") or ""),
        )
        for call in raw_calls
        if (call.get("function") or {}).get("name")
    )

    usage = body.get("usage") or {}
    content = message.get("content")
    return ChatReply(
        # 有些实现会把"没有正文"给成空串而不是 None，统一成 None 省得上层判两种
        content=str(content) if content else None,
        tool_calls=calls,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        model=str(body.get("model") or ""),
    )
