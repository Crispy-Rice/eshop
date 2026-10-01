"""inventory 模块的集成测试。

连真实的 PostgreSQL 与 Redis（``eshop_test``），不 mock —— 本模块的正确性
几乎全部建立在**数据库约束**（条件更新、CHECK 恒等式、主键冲突）与
**Redis 脚本原子性**之上，mock 掉任何一个都测不出真正的行为。

覆盖 docs/03 的八类边界：并发不超发、批量整批原子、幂等、状态机、
死锁顺序、DB 兜底、CHECK 约束、分区。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text

from app.core.db import get_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.redis import get_redis
from app.core.snowflake import next_id
from app.modules.inventory import redis_stock
from app.modules.inventory import service as inv
from app.modules.inventory.models import SkuStock, Warehouse
from app.modules.inventory.schemas import display_stock_text
from app.modules.inventory.service import StockItem

SHOP_ID = 990001
SKU_A = 880001
SKU_B = 880002
SKU_C = 880003


@pytest.fixture(autouse=True)
async def clean_redis(app_runtime: None) -> AsyncIterator[None]:
    """清掉库存相关的 Redis key。

    DB 由 conftest 的 ``clean_tables`` 清，但 Redis 是另一套状态 ——
    不清的话上一个用例留下的分片会让下一个用例读到错误的库存。
    """
    redis = get_redis()
    keys: list[str] = []
    async for key in redis.scan_iter(match="stock:*"):
        keys.append(key)
    async for key in redis.scan_iter(match="lock:stock_init:*"):
        keys.append(key)
    if keys:
        await redis.delete(*keys)
    yield


@pytest.fixture
async def warehouse() -> int:
    """建一个测试仓库，返回 warehouse_id。"""
    async with get_session_factory()() as session, session.begin():
        wh = Warehouse(id=next_id(), shop_id=SHOP_ID, name="测试仓", is_default=True)
        session.add(wh)
    return wh.id


async def _init(wh_id: int, sku_id: int, qty: int, key: str = "init") -> None:
    async with get_session_factory()() as session, session.begin():
        await inv.init(session, sku_id=sku_id, warehouse_id=wh_id, qty=qty, biz_key=key)


async def _state(wh_id: int, sku_id: int) -> tuple[int, int, int, int]:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT total, available, locked, frozen FROM inventory.sku_stock "
                    "WHERE sku_id = :sku AND warehouse_id = :wh"
                ),
                {"sku": sku_id, "wh": wh_id},
            )
        ).one()
    return row.total, row.available, row.locked, row.frozen


async def _flow_count() -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(text("SELECT count(*) FROM inventory.stock_flow")) or 0)


# ============================================================
# ① 并发不超发
# ============================================================
async def test_concurrent_lock_never_oversells(warehouse: int) -> None:
    """库存 10，20 个并发预占各 1 件 —— 必须恰好 10 成功、10 失败。

    这是本模块存在的理由。三道防线分别在不同层面拦截：
    Redis 脚本保证原子扣减、DB 的 ``available >= :num`` 保证绝不为负。
    """
    wh = warehouse
    await _init(wh, SKU_A, 10)

    session_factory = get_session_factory()

    async def one(i: int) -> bool:
        async with session_factory() as session:
            try:
                await inv.lock(
                    session,
                    [StockItem(sku_id=SKU_A, warehouse_id=wh, num=1)],
                    f"LOCK:{i}",
                    user_id=i,
                )
                await session.commit()
                return True
            except BizError as exc:
                await session.rollback()
                assert exc.code in (ErrorCode.STOCK_INSUFFICIENT, ErrorCode.STOCK_SOLD_OUT)
                return False

    results = await asyncio.gather(*(one(i) for i in range(20)))

    assert sum(results) == 10, f"应当恰好 10 个成功，实际 {sum(results)}"
    total, available, locked, frozen = await _state(wh, SKU_A)
    assert available == 0
    assert locked == 10
    # 恒等式仍然成立 —— CHECK 约束也会保证这一点
    assert total == available + locked + frozen


# ============================================================
# ② 批量整批原子
# ============================================================
async def test_batch_lock_is_all_or_nothing(warehouse: int) -> None:
    """三个 SKU 里第二个不足 → 整单失败，另外两个的库存一点都不能动。

    Redis 脚本"先全查再全扣"保证了这一点：不会出现"前两个扣了、第三个不够"。
    """
    wh = warehouse
    await _init(wh, SKU_A, 5, key="a")
    await _init(wh, SKU_B, 1, key="b")  # 只有 1 件，要 3 件会失败
    await _init(wh, SKU_C, 5, key="c")

    items = [
        StockItem(sku_id=SKU_A, warehouse_id=wh, num=2),
        StockItem(sku_id=SKU_B, warehouse_id=wh, num=3),
        StockItem(sku_id=SKU_C, warehouse_id=wh, num=2),
    ]

    async with get_session_factory()() as session:
        with pytest.raises(BizError):
            await inv.lock(session, items, "BATCH:1", user_id=1)
        await session.rollback()

    # 三个 SKU 都必须保持原样
    assert (await _state(wh, SKU_A))[1] == 5, "A 不该被扣"
    assert (await _state(wh, SKU_B))[1] == 1, "B 不该被扣"
    assert (await _state(wh, SKU_C))[1] == 5, "C 不该被扣"

    # Redis 侧也不能有部分扣减
    redis = get_redis()
    assert await redis_stock.read_available(redis, SKU_A, wh) == 5
    assert await redis_stock.read_available(redis, SKU_B, wh) == 1


# ============================================================
# ③ 幂等
# ============================================================
async def test_release_is_idempotent(warehouse: int) -> None:
    """同一个 biz_key 回补两次，库存只加一次。

    依据是 ``stock_biz_key`` 的主键冲突 —— 即使调用方判断错了也不会重复回补。
    """
    wh = warehouse
    await _init(wh, SKU_A, 10)
    item = StockItem(sku_id=SKU_A, warehouse_id=wh, num=4)

    async with get_session_factory()() as session, session.begin():
        await inv.lock(session, [item], "LOCK:X", user_id=1)
    assert (await _state(wh, SKU_A))[1] == 6

    async with get_session_factory()() as session, session.begin():
        await inv.release(session, [item], "CANCEL:X")
    async with get_session_factory()() as session, session.begin():
        await inv.release(session, [item], "CANCEL:X")  # 重复

    total, available, locked, _ = await _state(wh, SKU_A)
    assert available == 10, "重复回补不该让库存虚高"
    assert locked == 0
    assert total == available


async def test_adjust_is_idempotent(warehouse: int) -> None:
    """手工调整同样受幂等键保护（HTTP 层用 Idempotency-Key 映射过来）。"""
    wh = warehouse
    await _init(wh, SKU_A, 10)

    async with get_session_factory()() as session, session.begin():
        before, after = await inv.adjust(
            session, sku_id=SKU_A, warehouse_id=wh, delta=5, biz_key="ADJ:K1"
        )
    assert (before, after) == (10, 15)

    async with get_session_factory()() as session, session.begin():
        await inv.adjust(session, sku_id=SKU_A, warehouse_id=wh, delta=5, biz_key="ADJ:K1")

    assert (await _state(wh, SKU_A))[1] == 15, "重复调整不该生效两次"

    redis = get_redis()
    assert await redis_stock.read_available(redis, SKU_A, wh) == 15


# ============================================================
# ④ 状态机
# ============================================================
async def test_state_machine_lock_confirm_deliver(warehouse: int) -> None:
    """lock → confirm → deliver 走完，总量真正减少，恒等式始终成立。"""
    wh = warehouse
    await _init(wh, SKU_A, 10)
    item = StockItem(sku_id=SKU_A, warehouse_id=wh, num=3)

    async with get_session_factory()() as session, session.begin():
        await inv.lock(session, [item], "LOCK:S1", user_id=1, order_no="S1")
    assert await _state(wh, SKU_A) == (10, 7, 3, 0)

    async with get_session_factory()() as session, session.begin():
        await inv.confirm(session, [item], "CONFIRM:S1", order_no="S1")
    assert await _state(wh, SKU_A) == (10, 7, 0, 3)

    async with get_session_factory()() as session, session.begin():
        await inv.deliver(session, [item], "DELIVER:S1", order_no="S1")
    total, available, locked, frozen = await _state(wh, SKU_A)
    assert (total, available, locked, frozen) == (7, 7, 0, 0), "发货后总量才真正减少"
    assert total == available + locked + frozen


async def test_release_after_confirm_must_fail(warehouse: int) -> None:
    """已实扣（locked 已清零）的单据再回补必须失败，否则库存会凭空多出来。"""
    wh = warehouse
    await _init(wh, SKU_A, 10)
    item = StockItem(sku_id=SKU_A, warehouse_id=wh, num=3)

    async with get_session_factory()() as session, session.begin():
        await inv.lock(session, [item], "LOCK:S2", user_id=1, order_no="S2")
    async with get_session_factory()() as session, session.begin():
        await inv.confirm(session, [item], "CONFIRM:S2", order_no="S2")

    async with get_session_factory()() as session:
        with pytest.raises(BizError) as exc:
            await inv.release(session, [item], "CANCEL:S2", order_no="S2")
        await session.rollback()
    assert exc.value.code is ErrorCode.STOCK_INSUFFICIENT

    assert await _state(wh, SKU_A) == (10, 7, 0, 3), "失败的 release 不该改动任何库存"


# ============================================================
# ⑤ 死锁顺序
# ============================================================
async def test_batch_lock_order_prevents_deadlock(warehouse: int) -> None:
    """两个事务以**相反顺序**批量锁同一组 SKU，都要成功。

    service 内部按 ``(warehouse_id, sku_id)`` 升序更新，所以两边的加锁顺序
    实际一致。若去掉那个排序，PG 会检测到死锁并回滚其中一个（40P01）。
    """
    wh = warehouse
    await _init(wh, SKU_A, 50, key="a")
    await _init(wh, SKU_B, 50, key="b")

    forward = [
        StockItem(sku_id=SKU_A, warehouse_id=wh, num=1),
        StockItem(sku_id=SKU_B, warehouse_id=wh, num=1),
    ]
    backward = list(reversed(forward))

    async def place(items: list[StockItem], key: str) -> None:
        async with get_session_factory()() as session:
            await inv.lock(session, items, key, user_id=1)
            await session.commit()

    # 并发发起，让两个事务真正交错
    await asyncio.gather(place(forward, "D:1"), place(backward, "D:2"))

    assert (await _state(wh, SKU_A))[1] == 48
    assert (await _state(wh, SKU_B))[1] == 48


# ============================================================
# ⑥ DB 兜底
# ============================================================
async def test_db_fallback_blocks_when_redis_allows(warehouse: int) -> None:
    """Redis 显示有货、DB 已经没了 —— DB 的条件更新必须拦住。

    模拟 Redis 漂移（或有人绕过应用直接改库）。这正是"不能只靠 Redis"的原因。
    """
    wh = warehouse
    await _init(wh, SKU_A, 10)

    # 绕过应用，直接把 DB 的可售改成 0（Redis 里还是 10）
    async with get_session_factory()() as session, session.begin():
        await session.execute(
            text(
                "UPDATE inventory.sku_stock SET available = 0, locked = 10 "
                "WHERE sku_id = :sku AND warehouse_id = :wh"
            ),
            {"sku": SKU_A, "wh": wh},
        )

    redis = get_redis()
    assert await redis_stock.read_available(redis, SKU_A, wh) == 10, "Redis 此时仍然显示有货"

    async with get_session_factory()() as session:
        with pytest.raises(BizError):
            await inv.lock(
                session,
                [StockItem(sku_id=SKU_A, warehouse_id=wh, num=1)],
                "LOCK:FALLBACK",
                user_id=1,
            )
        await session.rollback()

    # 事务回滚后 DB 保持原样
    assert await _state(wh, SKU_A) == (10, 0, 10, 0)


# ============================================================
# ⑦ CHECK 约束
# ============================================================
async def test_check_constraint_rejects_negative_stock(warehouse: int) -> None:
    """任何让库存变负的写法都必须被数据库拒绝。

    这是"约束被绕过"时的最后一道保险 —— 即使应用代码有 Bug，
    也不可能把负库存写进去。
    """
    wh = warehouse
    await _init(wh, SKU_A, 5)

    async with get_session_factory()() as session:
        with pytest.raises(Exception) as exc:
            await session.execute(
                text(
                    "UPDATE inventory.sku_stock SET available = -1 "
                    "WHERE sku_id = :sku AND warehouse_id = :wh"
                ),
                {"sku": SKU_A, "wh": wh},
            )
        await session.rollback()
    assert "ck_sku_stock" in str(exc.value) or "check" in str(exc.value).lower()


async def test_check_constraint_rejects_identity_break(warehouse: int) -> None:
    """破坏 ``total = available + locked + frozen`` 的更新同样被拒绝。"""
    wh = warehouse
    await _init(wh, SKU_A, 5)

    async with get_session_factory()() as session:
        with pytest.raises(Exception) as exc:
            await session.execute(
                text(
                    "UPDATE inventory.sku_stock SET total = total + 1 "
                    "WHERE sku_id = :sku AND warehouse_id = :wh"
                ),
                {"sku": SKU_A, "wh": wh},
            )
        await session.rollback()
    assert "identity" in str(exc.value) or "check" in str(exc.value).lower()


# ============================================================
# ⑧ 分区
# ============================================================
async def test_stock_flow_lands_in_month_partition(warehouse: int) -> None:
    """流水写入正确的月份分区。"""
    wh = warehouse
    await _init(wh, SKU_A, 5)

    async with get_session_factory()() as session:
        table = await session.scalar(
            text("SELECT tableoid::regclass::text FROM inventory.stock_flow LIMIT 1")
        )
    assert table is not None
    # 分区名形如 stock_flow_202610，由迁移预建
    assert table.startswith("inventory.stock_flow_"), f"流水没落到分区里：{table}"


async def test_flow_written_for_every_change(warehouse: int) -> None:
    """每笔变更都要留痕 —— 流水是事后排查超卖的唯一依据。"""
    wh = warehouse
    await _init(wh, SKU_A, 10)  # 1 条：初始化
    item = StockItem(sku_id=SKU_A, warehouse_id=wh, num=2)

    async with get_session_factory()() as session, session.begin():
        await inv.lock(session, [item], "LOCK:F", user_id=1)
    async with get_session_factory()() as session, session.begin():
        await inv.confirm(session, [item], "CONFIRM:F")
    async with get_session_factory()() as session, session.begin():
        await inv.deliver(session, [item], "DELIVER:F")
    async with get_session_factory()() as session, session.begin():
        await inv.adjust(session, sku_id=SKU_A, warehouse_id=wh, delta=3, biz_key="ADJ:F")

    assert await _flow_count() == 5


# ============================================================
# 展示档位（纯函数，不需要数据库）
# ============================================================
async def test_adjust_redis_sync_survives_identity_map(warehouse: int) -> None:
    """回归用例：会话里已有该行的 ORM 对象时，调整后 Redis 必须拿到**新值**。

    背景：``_apply`` 用原生 UPDATE 改库存，而 ORM 的 identity map 不会因此刷新。
    若 ``_sync_one_to_redis`` 用 ORM 查询读回这一行，会拿到变更前的旧对象，
    把过期值推给 Redis —— 结果是 DB 已经 120、Redis 还是 0，
    用户看到"已售罄"而实际有货（少卖）。库存页先 ``ensure_stock_rows``
    再调整就是这条路径，所以它一定会被触发，不是理论问题。
    """
    wh = warehouse
    await _init(wh, SKU_A, 0)
    redis = get_redis()

    async with get_session_factory()() as session:
        # 故意先把这一行加载进 identity map（此时 available = 0）
        loaded = await session.scalar(
            select(SkuStock).where(SkuStock.sku_id == SKU_A, SkuStock.warehouse_id == wh)
        )
        assert loaded is not None
        assert loaded.available == 0

        # 同一个会话里调整 —— 旧实现会把 identity map 里的 0 推给 Redis
        await inv.adjust(
            session, sku_id=SKU_A, warehouse_id=wh, delta=120, biz_key="ADJ:IM"
        )
        await session.commit()

    assert (await _state(wh, SKU_A))[1] == 120
    assert await redis_stock.read_available(redis, SKU_A, wh) == 120, (
        "Redis 必须与 DB 一致，不能被 identity map 里的旧值覆盖"
    )


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        (0, "已售罄"),
        (1, "仅剩 5 件以内"),
        (3, "仅剩 5 件以内"),  # 真实 3 向上取到 5，不暴露精确值
        (5, "仅剩 5 件以内"),
        (6, "仅剩 10 件以内"),
        (10, "仅剩 10 件以内"),
        (11, "仅剩 20 件以内"),
        (87, "仅剩 90 件以内"),
        (100, "仅剩 100 件以内"),
        (101, "有货"),  # 超过阈值只显示"有货"，避免被爬真实库存
    ],
)
def test_display_stock_text_buckets(available: int, expected: str) -> None:
    text_value, _shown = display_stock_text(available)
    assert text_value == expected
