"""知识库：``knowledge/*.md`` 的加载与按受众过滤，见 docs/20 §4。

★ **放仓库文件，不放数据库 / 运营后台。** 理由只有一条但足够：它必须**随代码
  演化**。这个项目的规则变得快（多仓兜底、运费归属、注销流程都是最近改的），
  放库里的知识库一定会漂移 —— 而**拿旧规则答错的伤害大于不回答**。
  放仓库里，改规则的人会在同一个 PR 里看到它。

代价是改内容要发版。P0 接受；真需要运营自助编辑时，再加一张覆盖表，
git 仍是权威源（而不是把权威源搬走）。

★ 这份内容**不是 ``docs/`` 的复制**：``docs/`` 是内部设计文档（schema、redis key、
  幂等、部署），后台用户不该看到、对回答也没用。
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from app.core.enums import UserRole

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# 受众：文件可以在 front-matter 里声明只给谁看。
# 商家不该读到平台运营的处置口径，反之亦然 —— 那会引出它做不到的事。
#
# ★ **买家（``buyer``）是第三个、也是隔离要求最强的一个**：店小蜜拿到的知识库
#   必须**不含任何后台操作口径**（"新建仓库""绑运费模板"是商家后台的事，
#   讲给买家听既听不懂、也不该听到）。所以后台那几篇从 ``all`` 改成了 ``staff`` ——
#   原来的 ``all`` 在还没有买家端时是对的，现在它会把后台说明喂给买家。
AUDIENCE_STAFF = "staff"  # 后台用户（商家 + 运营）通用：平台怎么运转
AUDIENCE_ADMIN = "admin"  # 只有平台运营
AUDIENCE_BUYER = "buyer"  # 买家：面向公众的规则

# 店小蜜只看买家那一类
BUYER_AUDIENCES = frozenset({AUDIENCE_BUYER})


def audiences_for_role(role: str) -> frozenset[str]:
    """后台助手（商家 / 运营）该看哪些受众的知识。

    ★ **是有层次的，不是一个并集**：运营多一份"只有运营能看"的（处置口径），
      **商家看不到** —— 那正是当初加受众字段的理由（"商家不该读到平台运营的
      处置口径，那会引出它做不到的事"）。写成"后台都看 {staff, admin}"就把
      这条纪律丢了，商家会收到它执行不了的口径。
    """
    if role == UserRole.ADMIN:
        return frozenset({AUDIENCE_STAFF, AUDIENCE_ADMIN})
    return frozenset({AUDIENCE_STAFF})

_FRONT_MATTER_DELIM = "---"


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """解析 ``---`` 包裹的 front-matter。没有就返回空 meta + 原文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIM:
        return {}, text

    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONT_MATTER_DELIM:
            return meta, "\n".join(lines[index + 1 :]).strip()
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    # 只有开头的 ``---`` 而没有收尾：当作没有 front-matter，别把正文吃掉
    return {}, text


@lru_cache(maxsize=1)
def _load_all() -> tuple[tuple[str, str, str], ...]:
    """读一次，进程内缓存。返回 ``(audience, title, body)``，按文件名排序。

    ★ 排序是刻意的：知识库是拼进 system prompt 前缀的，顺序稳定才能吃到
      prompt cache（顺序一变，前缀就变，缓存全废）。
    """
    if not KNOWLEDGE_DIR.is_dir():
        return ()
    entries: list[tuple[str, str, str]] = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
        if not body:
            continue
        entries.append(
            (
                meta.get("audience", AUDIENCE_STAFF),
                meta.get("title", path.stem),
                body,
            )
        )
    return tuple(entries)


def load(audiences: Iterable[str] = (AUDIENCE_STAFF,)) -> str:
    """这个受众该看到的知识库正文（已拼好）。

    ``audiences`` 是一**组**受众：调用方说"我是谁"，文件说"给谁看"，取交集 ——
    这样加一个受众（比如这次的买家）不必回头改每个文件。
    """
    wanted = frozenset(audiences)
    parts = [
        f"### {title}\n{body}"
        for entry_audience, title, body in _load_all()
        if entry_audience in wanted
    ]
    return "\n\n".join(parts)
