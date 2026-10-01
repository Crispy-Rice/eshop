"""review 的纯函数单测：机审、排序分、反刷标记、审核状态机与统计 delta。

不碰数据库，跑得飞快。

**审核状态机与统计 delta 是同一件事的两面**（改了状态不调 delta，统计就漂；
调了 delta 不改状态，统计就重复计），所以这里两者一起测，
并且**穷举**所有 (状态, 动作) 组合 —— 手工列用例必然漏，漏掉的那条就是线上的一次非法处置。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.enums import ReviewStatus
from app.core.errors import BizError, ErrorCode
from app.modules.review import calc
from app.modules.review.models import (
    RANK_SUSPECT_PENALTY,
    RANK_WITH_CONTENT,
    RANK_WITH_IMAGE,
    SUSPECT_WITHIN_SECONDS,
)
from app.modules.review.sensitive import hit_high_risk, machine_audit
from app.modules.review.state_machine import (
    TRANSITIONS,
    A,
    AuditAction,
    can_transit,
    next_status,
    stat_delta,
)

S = ReviewStatus


# ============================================================
# 机审
# ============================================================
def test_clean_content_publishes_immediately() -> None:
    """没有敏感词的正常评价：直接发布，但标记待抽检（先发后审）。"""
    status, need_second = machine_audit("质量很好，物流也快，下次还来买")
    assert status == int(S.PUBLISHED)
    assert need_second is True


def test_high_risk_content_goes_to_manual_audit() -> None:
    """★ 命中引流词 → 待审核（先审后发）。这类内容一秒钟都不该挂出来。"""
    status, need_second = machine_audit("好评返现，加微信 领取红包")
    assert status == int(S.PENDING_AUDIT)
    assert need_second is False


def test_phone_and_qq_patterns_hit() -> None:
    """手机号与 QQ 号是正则匹配的（不是固定词）。"""
    assert hit_high_risk("有事联系 13812345678") is True
    assert hit_high_risk("我的 QQ: 123456789") is True
    assert hit_high_risk("价格 138 元") is False, "短数字不该误伤"


def test_empty_content_is_not_suspect_of_words() -> None:
    """只打星、不写字：不算命中敏感词。"""
    assert hit_high_risk(None) is False
    assert hit_high_risk("") is False
    assert machine_audit(None)[0] == int(S.PUBLISHED)


# ============================================================
# 「签收后立刻评价」的反刷标记
# ============================================================
def test_suspect_when_reviewed_immediately() -> None:
    now = datetime.now(UTC)
    assert calc.is_suspect(now, now + timedelta(seconds=SUSPECT_WITHIN_SECONDS - 1)) is True


def test_not_suspect_after_enough_time() -> None:
    now = datetime.now(UTC)
    assert calc.is_suspect(now, now + timedelta(seconds=SUSPECT_WITHIN_SECONDS + 1)) is False


def test_suspect_boundary_is_exclusive() -> None:
    now = datetime.now(UTC)
    assert calc.is_suspect(now, now + timedelta(seconds=SUSPECT_WITHIN_SECONDS)) is False


def test_never_suspect_without_receive_time() -> None:
    """没签收时间（理论上走不到）不判疑似，不能因为缺数据就给人扣分。"""
    assert calc.is_suspect(None, datetime.now(UTC)) is False


def test_naive_datetimes_are_tolerated() -> None:
    """库里是带时区的，但纯函数不该因为拿到 naive 时间就炸。"""
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert calc.is_suspect(naive, naive + timedelta(seconds=10)) is True


# ============================================================
# 推荐排序分
# ============================================================
def test_rank_score_rewards_content_and_images() -> None:
    assert calc.calc_rank_score(content=None, images=[]) == 0
    assert calc.calc_rank_score(content="还行", images=[]) == RANK_WITH_CONTENT
    assert calc.calc_rank_score(content="还行", images=["a.webp"]) == (
        RANK_WITH_CONTENT + RANK_WITH_IMAGE
    )
    # 只有图没内容（用户懒得写字）也给分
    assert calc.calc_rank_score(content=None, images=["a.webp"]) == RANK_WITH_IMAGE


def test_blank_content_does_not_count() -> None:
    """只有空白字符不算"有内容"—— 否则用户敲个空格就能拿推荐权重。"""
    assert calc.calc_rank_score(content="   ", images=[]) == 0


def test_likes_add_and_suspect_penalises() -> None:
    assert calc.calc_rank_score(content="好", images=[], like_count=7) == RANK_WITH_CONTENT + 7
    assert calc.calc_rank_score(content="好", images=[], suspect=True) == (
        RANK_WITH_CONTENT + RANK_SUSPECT_PENALTY
    )


# ============================================================
# 内容与图片校验
# ============================================================
def test_images_must_belong_to_current_user() -> None:
    """★ 图片路径必须是自己的目录 —— 否则可以把别人的图塞进自己的评价。"""
    calc.check_content(content="好", images=["reviews/42/ab.webp"], user_id=42)  # 不抛

    with pytest.raises(BizError) as exc:
        calc.check_content(content="好", images=["reviews/99/ab.webp"], user_id=42)
    assert exc.value.code is ErrorCode.VALIDATION_ERROR


def test_reject_path_traversal_in_images() -> None:
    with pytest.raises(BizError):
        calc.check_content(content=None, images=["reviews/42/../../etc/passwd"], user_id=42)


def test_reject_absolute_image_path() -> None:
    with pytest.raises(BizError):
        calc.check_content(content=None, images=["/media/reviews/42/ab.webp"], user_id=42)


def test_reject_too_many_images() -> None:
    images = [f"reviews/1/{i}.webp" for i in range(10)]
    with pytest.raises(BizError):
        calc.check_content(content=None, images=images, user_id=1)


def test_reject_too_long_content() -> None:
    with pytest.raises(BizError):
        calc.check_content(content="x" * 2001, images=[], user_id=1)


# ============================================================
# 审核状态机 —— 穷举
# ============================================================
EXPECTED: dict[tuple[ReviewStatus, AuditAction], ReviewStatus] = {
    (S.PENDING_AUDIT, A.APPROVE): S.PUBLISHED,
    (S.PENDING_AUDIT, A.REJECT): S.REJECTED,
    (S.PUBLISHED, A.BLOCK): S.BLOCKED,
    (S.BLOCKED, A.UNBLOCK): S.PUBLISHED,
}


def test_transitions_table_matches_expected() -> None:
    actual: dict[tuple[ReviewStatus, AuditAction], ReviewStatus] = {}
    for from_status, actions in TRANSITIONS.items():
        for action, to_status in actions.items():
            actual[(from_status, action)] = to_status
    assert actual == EXPECTED


def test_every_illegal_action_rejected() -> None:
    """穷举所有 (状态, 动作) —— 非法的必须全被拒。

    漏掉的那条就是线上的一次非法处置（比如把"审核不通过"的评价又"通过"了）。
    """
    checked = 0
    for from_status in list(ReviewStatus):
        for action in list(AuditAction):
            checked += 1
            if (from_status, action) in EXPECTED:
                continue
            with pytest.raises(BizError) as exc:
                next_status(from_status, action)
            assert exc.value.code is ErrorCode.REVIEW_STATUS_INVALID
    assert checked == len(list(ReviewStatus)) * len(list(AuditAction))


def test_rejected_is_terminal() -> None:
    """审核不通过是终态 —— 本期不做申诉，不能再改回来。"""
    assert TRANSITIONS[S.REJECTED] == {}
    for action in list(AuditAction):
        assert can_transit(S.REJECTED, action) is False


def test_can_transit_helper() -> None:
    assert can_transit(S.PENDING_AUDIT, A.APPROVE) is True
    assert can_transit(S.PENDING_AUDIT, A.BLOCK) is False


# ============================================================
# 统计 delta —— 与状态机成对
# ============================================================
def test_stat_delta_on_publish_and_unpublish() -> None:
    # 待审核 → 发布：+1 条、+分数、5 星计好评
    assert stat_delta(S.PENDING_AUDIT, S.PUBLISHED, score=5, is_follow_up=False) == (1, 5, 1)
    # 发布 → 屏蔽：−1 条
    assert stat_delta(S.PUBLISHED, S.BLOCKED, score=5, is_follow_up=False) == (-1, -5, -1)
    # 屏蔽 → 解除：+1 条（回到发布）
    assert stat_delta(S.BLOCKED, S.PUBLISHED, score=5, is_follow_up=False) == (1, 5, 1)


def test_stat_delta_good_threshold() -> None:
    """4 星及以上算好评，3 星不算。"""
    assert stat_delta(S.PENDING_AUDIT, S.PUBLISHED, score=4, is_follow_up=False)[2] == 1
    assert stat_delta(S.PENDING_AUDIT, S.PUBLISHED, score=3, is_follow_up=False)[2] == 0
    # 但它仍然计入评价总数与总分
    assert stat_delta(S.PENDING_AUDIT, S.PUBLISHED, score=3, is_follow_up=False)[:2] == (1, 3)


def test_stat_delta_ignores_rejected_path() -> None:
    """待审核 → 审核不通过：从未计数，所以不减。"""
    assert stat_delta(S.PENDING_AUDIT, S.REJECTED, score=5, is_follow_up=False) == (0, 0, 0)


def test_stat_delta_ignores_follow_ups() -> None:
    """★ 追评一律不计入统计。

    统计口径是"已发布的首评"（与每日全量重算的 `NOT is_follow_up` 一致）。
    若是这里计了，第二天的重算会把计数打回去，日报与实时值就永远对不上。
    """
    assert stat_delta(S.PENDING_AUDIT, S.PUBLISHED, score=5, is_follow_up=True) == (0, 0, 0)
    assert stat_delta(S.PUBLISHED, S.BLOCKED, score=5, is_follow_up=True) == (0, 0, 0)


def test_stat_delta_same_state_changes_nothing() -> None:
    assert stat_delta(S.PUBLISHED, S.PUBLISHED, score=5, is_follow_up=False) == (0, 0, 0)
