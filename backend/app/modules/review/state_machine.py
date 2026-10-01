"""评价审核状态机（docs/12 §4）。

**表驱动，不是 if-else** —— 与 trade / aftersale 的状态机同一套写法。
状态枚举直接复用 ``app.core.enums.ReviewStatus``，不重复定义数字。

```
0 待审核 ──APPROVE──▶ 1 已发布        （★ 这一刻才计入统计）
        └─REJECT───▶ 3 审核不通过      （从未计数，故不减）
1 已发布 ──BLOCK───▶ 2 已屏蔽         （★ 统计 −1）
2 已屏蔽 ──UNBLOCK─▶ 1 已发布         （★ 统计 +1）
```

**统计与状态必须成对**：所以调用方统一走 ``service.transit()``，
它在改状态的同时调 ``product.apply_review_stat_delta``。
只要有一个地方绕过它直接改 status，计数就会漂 —— 这正是表驱动 + 单一入口的价值。

本期不做用户申诉（docs/12 §4 的"申诉后重新进人工队列"）。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.enums import ReviewStatus
from app.core.errors import BizError, ErrorCode
from app.modules.review.models import GOOD_SCORE, REVIEW_STATUS_TEXT

S = ReviewStatus


class AuditAction(StrEnum):
    """运营对评价的处置动作。"""

    APPROVE = "APPROVE"  # 待审核 → 通过（发布）
    REJECT = "REJECT"  # 待审核 → 审核不通过
    BLOCK = "BLOCK"  # 已发布 → 屏蔽
    UNBLOCK = "UNBLOCK"  # 已屏蔽 → 解除屏蔽（恢复发布）


A = AuditAction

# ★ 审核状态机：当前状态 → 动作 → 目标状态
TRANSITIONS: dict[ReviewStatus, dict[AuditAction, ReviewStatus]] = {
    S.PENDING_AUDIT: {
        A.APPROVE: S.PUBLISHED,
        A.REJECT: S.REJECTED,
    },
    S.PUBLISHED: {
        A.BLOCK: S.BLOCKED,
    },
    S.BLOCKED: {
        A.UNBLOCK: S.PUBLISHED,
    },
    # 终态：审核不通过之后不能再改（要恢复只能重新申诉，本期不做）
    S.REJECTED: {},
}

# 评价状态的中文文案在 models.py（与其它模块一致，状态文案跟着模型走）
AUDIT_ACTION_TEXT: dict[str, str] = {
    str(A.APPROVE): "审核通过",
    str(A.REJECT): "审核不通过",
    str(A.BLOCK): "屏蔽",
    str(A.UNBLOCK): "解除屏蔽",
}


def can_transit(from_status: ReviewStatus, action: AuditAction) -> bool:
    return action in TRANSITIONS.get(from_status, {})


def next_status(from_status: ReviewStatus, action: AuditAction) -> ReviewStatus:
    """算出目标状态。非法操作抛业务异常。"""
    target = TRANSITIONS.get(from_status, {}).get(action)
    if target is None:
        raise BizError(
            ErrorCode.REVIEW_STATUS_INVALID,
            f"评价当前是「{REVIEW_STATUS_TEXT.get(int(from_status), from_status)}」，"
            "不能执行该操作",
        )
    return target


def stat_delta(
    from_status: ReviewStatus,
    to_status: ReviewStatus,
    *,
    score: int,
    is_follow_up: bool,
) -> tuple[int, int, int]:
    """状态迁移对 SPU 统计的影响：``(count_delta, score_delta, good_delta)``。

    ★ **这个方法与状态机是同一件事的两面**，所以放在同一个文件里 ——
    只改状态不调它，统计就会漂；只调它不改状态，统计会重复计。

    规则（docs/12 §3.2 的统计口径是"已发布的首评"）：

    - **追评一律不计**（与统计 SQL 的 ``NOT is_follow_up`` 保持一致，
      否则每日全量重算会把线上计数打回去，形成日报与实时值之间的抖动）
    - 进入「已发布」→ +1；离开「已发布」→ −1；其余迁移（待审核↔不通过）都不动
    """
    if is_follow_up:
        return (0, 0, 0)

    was_published = from_status is S.PUBLISHED
    now_published = to_status is S.PUBLISHED
    if was_published == now_published:
        return (0, 0, 0)

    sign = 1 if now_published else -1
    good = 1 if score >= GOOD_SCORE else 0
    return (sign, sign * score, sign * good)
