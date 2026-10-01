"""评价的纯函数计算 —— **不碰数据库**。

放在单独文件里是为了能直接单测（与 ``promotion/allocation.py``、
``aftersale/calc.py`` 同样的做法）。

三个计算：

1. ``rank_score`` —— 「推荐排序」的排序分。不适合在查询时实时算（走不了索引），
   所以发布时算好存下来（docs/12 §3.1）
2. ``is_suspect`` —— 「签收后立刻评价」的反刷标记（docs/12 §11）
3. ``check_content`` —— 内容与图片的合法性（长度、数量、图片路径归属）
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import BizError, ErrorCode
from app.modules.review.models import (
    MAX_CONTENT_LEN,
    MAX_IMAGES,
    RANK_SUSPECT_PENALTY,
    RANK_WITH_CONTENT,
    RANK_WITH_IMAGE,
    SUSPECT_WITHIN_SECONDS,
)


def calc_rank_score(
    *, content: str | None, images: list[str], like_count: int = 0, suspect: bool = False
) -> int:
    """算「推荐排序」分。

    有内容 +1000、有图 +500、被点赞按赞数加分；被判疑似刷评再降一档。
    默认排序（无内容无图）分数最低，自然沉底 —— 这正是文档想要的效果。
    """
    score = like_count
    if content and content.strip():
        score += RANK_WITH_CONTENT
    if images:
        score += RANK_WITH_IMAGE
    if suspect:
        score += RANK_SUSPECT_PENALTY
    return score


def is_suspect(receive_time: datetime | None, created_at: datetime) -> bool:
    """签收后 ``SUSPECT_WITHIN_SECONDS`` 秒内就写出评价 → 疑似没时间体验（docs/12 §11）。

    正常用户至少要看一眼货。不影响展示，只是降低推荐排序权重并进人工抽检。
    没签收时间（理论上不会走到这里）不算疑似。
    """
    if receive_time is None:
        return False
    received = receive_time if receive_time.tzinfo else receive_time.replace(tzinfo=UTC)
    created = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    return (created - received).total_seconds() < SUSPECT_WITHIN_SECONDS


def check_content(*, content: str | None, images: list[str], user_id: int) -> None:
    """校验评价内容与图片。

    ★ **图片路径必须属于当前用户**：只允许 ``reviews/{自己的 user_id}/`` 前缀。
    不加这条，用户可以把别人上传的图片（或随便一个路径）塞进自己的评价里。
    这是 docs/12 §9 点名的一条。
    """
    if content is not None and len(content) > MAX_CONTENT_LEN:
        raise BizError(
            ErrorCode.VALIDATION_ERROR, f"评价内容不能超过 {MAX_CONTENT_LEN} 字"
        )
    if len(images) > MAX_IMAGES:
        raise BizError(ErrorCode.VALIDATION_ERROR, f"最多上传 {MAX_IMAGES} 张图片")

    expected_prefix = f"reviews/{user_id}/"
    for path in images:
        if not path.startswith(expected_prefix):
            raise BizError(ErrorCode.VALIDATION_ERROR, "图片路径不合法，请重新上传")
        # 防路径穿越：即使前缀对了，也不允许出现上级目录
        if ".." in path or path.startswith("/"):
            raise BizError(ErrorCode.VALIDATION_ERROR, "图片路径不合法，请重新上传")


def has_content(content: str | None) -> bool:
    """是否算"有内容"的评价（统计口径与 rank_score 保持一致）。"""
    return bool(content and content.strip())
