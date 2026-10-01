"""review 模块的数据访问层。

只读写 ``review`` schema。

**状态变更一律走 ``service.transit()``** —— 它在改状态的同时调统计 delta。
这里只提供 CAS 原语，唯一的例外是评价插入：它用
``ON CONFLICT DO NOTHING ... RETURNING`` 表达"已经评价过"（见 ``insert_review``）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ReviewStatus
from app.modules.review.models import (
    REPLY_DELETED,
    REPLY_MERCHANT,
    Review,
    ReviewReply,
)

# 排序方式（商品详情页）
SORT_LATEST = "latest"
SORT_RECOMMEND = "recommend"
# 筛选（商品详情页）
FILTER_ALL = "all"
FILTER_GOOD = "good"
FILTER_WITH_IMAGE = "with_image"


async def get_by_id(session: AsyncSession, review_id: int) -> Review | None:
    return await session.get(Review, review_id)


async def get_for_update(session: AsyncSession, review_id: int) -> Review | None:
    """加**行锁**读评价。商家回复前锁它，挡住并发的"第 4 条回复"。"""
    return await session.scalar(
        select(Review).where(Review.id == review_id).with_for_update()
    )


async def insert_review(session: AsyncSession, values: dict) -> int | None:
    """插入一条评价，**冲突时返回 None 而不是抛异常**。

    ★ 用 ``ON CONFLICT DO NOTHING``（命中 ``uk_review_order_item``）而不是捕获
    ``IntegrityError``：PostgreSQL 里唯一冲突会让**整个事务进入 aborted 状态**，
    后续任何语句都报 ``current transaction is aborted``，要恢复得给这条 INSERT
    单独包 SAVEPOINT。``ON CONFLICT DO NOTHING`` 冲突时不报错、``RETURNING`` 拿到
    NULL，事务保持健康 —— 调用方把 None 映射成"已经评价过了"即可
    （docs/12 §2.2 点名了这个模式，本仓库的 ``core/outbox.py`` 也是这么写的）。

    同时挡住的还有并发重复提交：两个请求都通过了预检查，也只有一条能插进来。
    """
    stmt = (
        pg_insert(Review)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["order_item_id"], index_where=text("NOT is_follow_up")
        )
        .returning(Review.id)
    )
    return await session.scalar(stmt)


async def insert_follow_up(session: AsyncSession, values: dict) -> int | None:
    """插入追评。冲突（命中 ``uk_review_follow_up``）时返回 None。"""
    stmt = (
        pg_insert(Review)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["parent_id"], index_where=text("is_follow_up"))
        .returning(Review.id)
    )
    return await session.scalar(stmt)


async def get_first_review_of_item(
    session: AsyncSession, order_item_id: int
) -> Review | None:
    """该订单项的首评。**只是友好提示的预检查**，真正保证是唯一索引。"""
    return await session.scalar(
        select(Review).where(
            Review.order_item_id == order_item_id, Review.is_follow_up.is_(False)
        )
    )


async def get_follow_up_of(session: AsyncSession, parent_id: int) -> Review | None:
    return await session.scalar(
        select(Review).where(Review.parent_id == parent_id, Review.is_follow_up.is_(True))
    )


async def reviewed_item_ids(
    session: AsyncSession, order_item_ids: Sequence[int]
) -> set[int]:
    """这批订单项里**已评过**的那些（待评价列表要减掉它们）。"""
    if not order_item_ids:
        return set()
    rows = await session.scalars(
        select(Review.order_item_id).where(
            Review.order_item_id.in_(order_item_ids), Review.is_follow_up.is_(False)
        )
    )
    return set(rows)


async def cas_status(
    session: AsyncSession,
    review_id: int,
    *,
    from_status: int,
    to_status: int,
    audit_remark: str | None = None,
) -> bool:
    """CAS 更新评价状态。``rowcount = 0`` 说明已被别人处置过。"""
    result = await session.execute(
        update(Review)
        .where(Review.id == review_id, Review.status == from_status)
        .values(
            status=to_status,
            audit_remark=audit_remark,
            updated_at=func.now(),
            version=Review.version + 1,
        )
    )
    return result.rowcount > 0


async def incr_reply_count(session: AsyncSession, review_id: int) -> None:
    await session.execute(
        update(Review)
        .where(Review.id == review_id)
        .values(reply_count=Review.reply_count + 1, updated_at=func.now())
    )


async def insert_reply(session: AsyncSession, reply: ReviewReply) -> ReviewReply:
    session.add(reply)
    await session.flush()
    return reply


async def count_merchant_replies(session: AsyncSession, review_id: int) -> int:
    """该评价已有几条**未删除的商家回复**（平台回复不计入上限）。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ReviewReply)
            .where(
                ReviewReply.review_id == review_id,
                ReviewReply.reply_type == REPLY_MERCHANT,
                ReviewReply.status != REPLY_DELETED,
            )
        )
        or 0
    )


async def list_replies_of(
    session: AsyncSession, review_ids: Sequence[int]
) -> dict[int, list[ReviewReply]]:
    """批量取回复（列表页避免 N+1）。只取已发布且未删除的。"""
    if not review_ids:
        return {}
    rows = await session.scalars(
        select(ReviewReply)
        .where(ReviewReply.review_id.in_(review_ids), ReviewReply.status == 1)
        .order_by(ReviewReply.id)
    )
    out: dict[int, list[ReviewReply]] = {}
    for row in rows:
        out.setdefault(int(row.review_id), []).append(row)
    return out


async def list_follow_ups(
    session: AsyncSession, parent_ids: Sequence[int]
) -> dict[int, Review]:
    """批量取追评，按首评 id 归组（列表页避免 N+1）。

    追评在展示上要紧跟在首评下方，所以一次把这些 parent 的追评全取回来。
    """
    if not parent_ids:
        return {}
    rows = await session.scalars(
        select(Review).where(Review.parent_id.in_(parent_ids), Review.is_follow_up.is_(True))
    )
    return {int(r.parent_id): r for r in rows if r.parent_id is not None}


# ============================================================
# 列表（键集游标分页）
# ============================================================
def _cursor_clause(column, row_id, cursor: tuple[datetime, int] | None):
    """行值比较的游标条件：``(created_at, id) < (last_time, last_id)``。"""
    if cursor is None:
        return None
    last_time, last_id = cursor
    return (column < last_time) | ((column == last_time) & (row_id < last_id))


def _published_first() -> tuple:
    """商品详情页只展示「已发布的首评」。"""
    return (Review.status == int(ReviewStatus.PUBLISHED), Review.is_follow_up.is_(False))


async def list_published_for_spu(
    session: AsyncSession,
    spu_id: int,
    *,
    sort: str = SORT_LATEST,
    filter_: str = FILTER_ALL,
    cursor: tuple[datetime, int] | None = None,
    recommend_cursor: tuple[int, int] | None = None,
    limit: int = 20,
) -> list[Review]:
    """商品详情页的评价列表。

    三种排序/筛选都有对应的部分索引（``idx_review_spu_list`` / ``idx_review_spu_rank``
    / ``idx_review_spu_score``），所以都是游标分页、没有深分页问题。
    """
    stmt: Select = select(Review).where(Review.spu_id == spu_id, *_published_first())
    if filter_ == FILTER_GOOD:
        stmt = stmt.where(Review.score >= 4)
    elif filter_ == FILTER_WITH_IMAGE:
        stmt = stmt.where(func.jsonb_array_length(Review.images) > 0)

    if sort == SORT_RECOMMEND:
        if recommend_cursor is not None:
            last_rank, last_id = recommend_cursor
            stmt = stmt.where(
                (Review.rank_score < last_rank)
                | ((Review.rank_score == last_rank) & (Review.id < last_id))
            )
        stmt = stmt.order_by(Review.rank_score.desc(), Review.id.desc())
    else:
        clause = _cursor_clause(Review.created_at, Review.id, cursor)
        if clause is not None:
            stmt = stmt.where(clause)
        stmt = stmt.order_by(Review.created_at.desc(), Review.id.desc())
    return list(await session.scalars(stmt.limit(limit)))


async def list_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    cursor: tuple[datetime, int] | None = None,
    limit: int = 10,
) -> list[Review]:
    """我的评价（首评与追评都列出来，用户要能看到自己发过的全部内容）。"""
    stmt: Select = select(Review).where(Review.user_id == user_id)
    clause = _cursor_clause(Review.created_at, Review.id, cursor)
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = stmt.order_by(Review.created_at.desc(), Review.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def list_for_shop(
    session: AsyncSession,
    shop_id: int,
    *,
    status: int | None = None,
    cursor: tuple[datetime, int] | None = None,
    limit: int = 20,
) -> list[Review]:
    """商家看本店铺的评价。走 ``idx_review_shop_status``。"""
    stmt: Select = select(Review).where(Review.shop_id == shop_id)
    if status is not None:
        stmt = stmt.where(Review.status == status)
    clause = _cursor_clause(Review.created_at, Review.id, cursor)
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = stmt.order_by(Review.created_at.desc(), Review.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def list_audit_queue(
    session: AsyncSession,
    *,
    status: int = int(ReviewStatus.PENDING_AUDIT),
    second_audit_only: bool = False,
    cursor: tuple[datetime, int] | None = None,
    limit: int = 20,
) -> list[Review]:
    """运营审核队列。

    ``second_audit_only`` 用于看"机审放行但标记待抽检"的那批 ——
    这是 docs/12 §4 混合审核模式里的抽检队列。
    """
    stmt: Select = select(Review).where(Review.status == status)
    if second_audit_only:
        stmt = stmt.where(Review.need_second_audit.is_(True))
    clause = _cursor_clause(Review.created_at, Review.id, cursor)
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = stmt.order_by(Review.created_at.desc(), Review.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def count_by_status(session: AsyncSession) -> dict[int, int]:
    """各状态的评价数（运营后台的角标）。"""
    rows = await session.execute(select(Review.status, func.count()).group_by(Review.status))
    return {int(s): int(n) for s, n in rows}
