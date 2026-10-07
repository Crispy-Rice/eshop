"""assistant 的请求 / 响应模型。

★ 状态与角色在**出口处**翻译成字符串（``pending`` / ``done`` / …）：
  前端要判断"还在飞吗"，用字符串比用魔法数字可读得多，而库里存整数是为了省空间
  与好索引。翻译只此一处。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.core.schemas import CamelModel, SnowflakeId
from app.modules.assistant.models import (
    MESSAGE_ASSISTANT,
    MESSAGE_DEGRADED,
    MESSAGE_DONE,
    MESSAGE_FAILED,
    MESSAGE_PENDING,
    MESSAGE_RUNNING,
    MESSAGE_USER,
    Message,
    ShopFaq,
)

# 问题长度上界。卡在 2000 是因为它是"一句话问题"，不是贴文档的地方 ——
# 太长的问题除了烧钱没有别的作用
MAX_QUESTION_CHARS = 2000

# 轮询一次取多少条消息（前端渲染一屏足够）
MESSAGE_PAGE_SIZE = 50
# 历史对话列表一页多少条
CONVERSATION_PAGE_SIZE = 20

_STATUS_TEXT: dict[int, str] = {
    MESSAGE_PENDING: "pending",
    MESSAGE_RUNNING: "running",
    MESSAGE_DONE: "done",
    MESSAGE_FAILED: "failed",
    MESSAGE_DEGRADED: "degraded",
}
_ROLE_TEXT: dict[int, str] = {MESSAGE_USER: "user", MESSAGE_ASSISTANT: "assistant"}


class AskRequest(CamelModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    # ★ 是 ``str`` 不是 ``SnowflakeId``：会话号形如 ``A20261007…``（日期 + 雪花后 10 位
    #   + Luhn 校验位，见 build_conversation_no），**不是纯数字**，按雪花校验会解析失败
    conversation_no: str | None = Field(
        default=None,
        max_length=32,
        description="接着哪条会话问。**不传 = 开一条新对话**（前端的「新对话」就是清空它）",
    )


class MessageOut(CamelModel):
    id: SnowflakeId
    role: str = Field(description="user / assistant")
    status: str = Field(description="pending / running / done / failed / degraded")
    content: str | None = Field(default=None, description="答案正文；还在飞时为空")
    error_code: str | None = None
    create_time: datetime

    @classmethod
    def of(cls, row: Message) -> MessageOut:
        return cls(
            id=row.id,
            role=_ROLE_TEXT.get(row.role, "assistant"),
            status=_STATUS_TEXT.get(row.status, "done"),
            content=row.content,
            error_code=row.error_code,
            create_time=row.created_at,
        )


class AskOut(CamelModel):
    """提交之后的回执。前端拿 ``message_id`` 去轮询会话详情。"""

    conversation_no: str
    message_id: SnowflakeId


class ConversationOut(CamelModel):
    """一条会话 + 它的一页消息。

    ``conversation_no`` 为空有两种情况：这个账号还用不了助手（``available=false``），
    或者还没有任何会话。
    """

    conversation_no: str | None = None
    title: str | None = None
    # 能不能用助手：管理员恒 true；商家没开店时为 false（前端据此不显示入口）
    available: bool = True
    messages: list[MessageOut] = Field(default_factory=list)
    # 还有更早的消息吗？有的话把 next_cursor 原样回传就能取上一页
    has_more: bool = False
    next_cursor: str | None = Field(
        default=None, description="取更早一页用的游标（当前最早那条消息的 id）"
    )


class ConversationListItemOut(CamelModel):
    """会话列表里的一行（不带消息 —— 列表只要标题和时间）。"""

    conversation_no: str
    title: str
    updated_at: datetime


class ConversationListOut(CamelModel):
    items: list[ConversationListItemOut] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


class HandoffRequest(CamelModel):
    """转人工：转的是哪条会话、附一句什么补充说明（两者都会写进工单首条消息）。"""

    note: str | None = Field(default=None, max_length=MAX_QUESTION_CHARS)
    # ★ 会话号是 ``A`` + 日期 + 雪花后 10 位 + Luhn 的**单号**，不是纯雪花 ——
    #   用 SnowflakeId 会在 ``A…`` 上解析失败（与 AskRequest.conversation_no 同一个坑）。
    #   不传 = 转最近动过的那条。
    conversation_no: str | None = Field(default=None, max_length=32)


class HandoffOut(CamelModel):
    ticket_no: str
    # 已有进行中的会话时复用那一条，而不是又开一条（support.open_ticket 的语义）
    reused: bool


# ==================================================================
# 店小蜜（买家侧的智能客服）—— 商户侧维护用的出入参（docs/20 §14）
# ==================================================================
class ShopSettingOut(CamelModel):
    """本店的智能客服开关。**没有那一行 = 没开过 = 关**。"""

    ai_enabled: bool


class ShopSettingIn(CamelModel):
    ai_enabled: bool


class ShopFaqOut(CamelModel):
    id: SnowflakeId
    question: str
    answer: str
    enabled: bool
    updated_at: datetime

    @classmethod
    def of(cls, row: ShopFaq) -> ShopFaqOut:
        return cls(
            id=row.id,
            question=row.question,
            answer=row.answer,
            enabled=bool(row.enabled),
            updated_at=row.updated_at,
        )


class ShopFaqIn(CamelModel):
    """一条问答。★ 长度上限与库里那两列一致 —— 不一致的话运营会看到 500 而不是 422。"""

    question: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=1000)
    enabled: bool = True


class ShopBotStatsOut(CamelModel):
    """今日计数。给商户一个"它在替我说话、花我的钱"的可见处。"""

    answered: int
    not_answered: int
    pending: int
    tokens_today: int
