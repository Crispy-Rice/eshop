"""机审：敏感词命中判定（docs/12 §4）。

**本期用朴素子串扫描，不引入 Aho-Corasick。** docs/12 提到 `pyahocorasick`，
那是词库上万条、每篇内容几万字时才有意义的优化；本项目的词库几十条、评价几百字，
`O(n·m)` 的开销可以忽略。为一个还不存在的规模问题加一个 C 扩展依赖不划算 ——
真到那时再换，接口（``hit_high_risk``）不用变。

词库是仓库里的 JSON 文件，进程启动时加载进内存。**改词库要发版** ——
docs/12 设想的"运营后台改词库、事件通知各进程重载"需要 outbox 消费者，
而本仓库还没有（与 aftersale 的积分同理）。一期词库本来也不常改。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from app.core.enums import ReviewStatus
from app.core.logging import get_logger

logger = get_logger(__name__)

_WORDS_FILE = Path(__file__).with_name("sensitive_words.json")


class _Wordbook:
    """加载好的词库：子串列表 + 编译好的正则。"""

    __slots__ = ("patterns", "words")

    def __init__(self, words: tuple[str, ...], patterns: tuple[re.Pattern[str], ...]) -> None:
        self.words = words
        self.patterns = patterns


@lru_cache(maxsize=1)
def load_wordbook() -> _Wordbook:
    """加载词库。缓存起来，只在进程首次使用时读一次盘。

    文件缺失或格式不对**不抛异常** —— 机审是增强能力，不该让整个服务起不来。
    退化成空词库（全部放行、全部标记待抽检），并打一条 error 日志。
    """
    try:
        raw = json.loads(_WORDS_FILE.read_text(encoding="utf-8"))
        words = tuple(str(w).strip() for w in raw.get("high_risk", []) if str(w).strip())
        patterns = tuple(re.compile(p) for p in raw.get("patterns", []))
    except (OSError, json.JSONDecodeError, re.error) as exc:  # pragma: no cover - 配置错误
        logger.error("敏感词库加载失败，机审降级为全部放行", exc_info=exc)
        return _Wordbook((), ())
    logger.info("敏感词库已加载", extra={"words": len(words), "patterns": len(patterns)})
    return _Wordbook(words, patterns)


def hit_high_risk(content: str | None) -> bool:
    """内容是否命中高风险词。空内容不算命中。"""
    if not content:
        return False
    book = load_wordbook()
    if any(word in content for word in book.words):
        return True
    return any(pattern.search(content) for pattern in book.patterns)


def machine_audit(content: str | None) -> tuple[int, bool]:
    """机审：返回 ``(初始状态, 是否需要二次审核)``。

    - **命中高风险** → 待审核（**先审后发**）。广告导流、辱骂这类内容一秒钟都不该挂出来
    - **未命中** → 直接发布，但标记 ``need_second_audit`` 进人工抽检队列（**先发后审**）

    这是 docs/12 §4 选的"混合"模式：机审放行的先发，疑似的人工审 ——
    纯"先审后发"会让用户发完看不到自己的评价，体验很差。
    """
    if hit_high_risk(content):
        return int(ReviewStatus.PENDING_AUDIT), False
    return int(ReviewStatus.PUBLISHED), True
