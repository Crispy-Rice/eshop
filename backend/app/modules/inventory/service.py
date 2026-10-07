"""inventory 模块对外暴露的 service 接口。

docs/03 §11 列的七个函数。**全部接受 ``biz_key``，没有例外**——这是把幂等
责任放在最底层的设计选择：底层可靠，上层（trade / payment / aftersale）
就可以简化。

每个函数的固定流程（顺序不能变）：

1. **幂等前置**：先往 ``stock_biz_key`` 里 ``INSERT ... ON CONFLICT DO NOTHING``。
   ``rowcount == 0`` 说明这批操作已处理过，**直接返回，不碰任何库存**。
   这是防重复回补的终极武器——即使上游所有幂等判断都失效，
   这里的主键也会拦住第二次写入（docs/03 §8）。
2. **业务条件写进 WHERE**，不是"先查后改"。``rowcount == 0`` 就是并发冲突，
   由调用方回滚整个事务（docs/03 §5）。
3. **批量按 ``(warehouse_id, sku_id)`` 升序**遍历。顺序不一致会让两个订单
   以相反顺序锁同一组 SKU 而死锁（docs/03 §5.1）。
4. 同一事务内写 ``stock_flow`` 流水，``before_qty``/``after_qty`` 从 ``RETURNING`` 取。
5. Redis 变更在 DB 事务之外先做，DB 失败则按**同一个 biz_key** 补偿回补。

Redis 与 DB 都要做（docs/03 §4.2）：只有 Redis 会在 AOF 丢写入时超卖；
只有 DB 会让热点行锁打满。**DB 是账本**，Redis 只是闸门。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_keys as rk
from app.core.config import get_settings
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.redis import get_lua, get_redis
from app.core.snowflake import next_id
from app.modules.inventory import redis_stock, routing
from app.modules.inventory import repository as repo
from app.modules.inventory.models import (
    CHANGE_ADJUST,
    CHANGE_CONFIRM,
    CHANGE_DEFECTIVE,
    CHANGE_DELIVER,
    CHANGE_INIT,
    CHANGE_LOCK,
    CHANGE_REFUND_BACK,
    CHANGE_RELEASE,
    CHANGE_RETURN_IN,
    CHANGE_TYPE_TEXT,
    WAREHOUSE_DISABLED,
    WAREHOUSE_ENABLED,
    SkuStock,
    Warehouse,
    WarehouseRegionRule,
)
from app.modules.inventory.schemas import (
    RegionRuleOut,
    StockAdjustOut,
    StockAdjustRequest,
    StockFlowListOut,
    StockFlowOut,
    StockItemOut,
    StockListOut,
    WarehouseCreatedOut,
    WarehouseCreateRequest,
    WarehouseOut,
    WarehouseUpdateRequest,
    display_stock_text,
)

# ★ 只允许 inventory → product 这一个方向（docs/01 §2）。
#   product 不 import inventory，所以不会成环。
from app.modules.product import service as product_service

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StockItem:
    """一笔库存变更涉及的单个 SKU。"""

    sku_id: int
    warehouse_id: int
    num: int


# ============================================================
# 幂等与流水
# ============================================================


async def _claim_biz_key(session: AsyncSession, biz_key: str) -> bool:
    """占用幂等键。返回 True 表示本次是**首次**处理，False 表示已经处理过。

    用 ``ON CONFLICT DO NOTHING`` + rowcount 判断，而不是"先 SELECT 再 INSERT"：
    后者在并发下有竞态，两个请求可能都查到"不存在"然后各自继续。
    """
    result = await session.execute(
        text(
            "INSERT INTO inventory.stock_biz_key (biz_key) VALUES (:biz_key) "
            "ON CONFLICT DO NOTHING"
        ),
        {"biz_key": biz_key},
    )
    return result.rowcount == 1


async def _write_flow(
    session: AsyncSession,
    *,
    sku_id: int,
    warehouse_id: int,
    change_type: int,
    num: int,
    before_qty: int,
    after_qty: int,
    biz_key: str,
    order_no: str | None = None,
    operator: str | None = None,
    remark: str | None = None,
) -> None:
    """写一条库存流水。

    这是事后排查超卖的**唯一可靠依据**（docs/03 §4），所以每次变更都要落。
    与库存变更在同一事务内：要么都成功，要么都不写。
    """
    await session.execute(
        text(
            "INSERT INTO inventory.stock_flow "
            "(sku_id, warehouse_id, order_no, change_type, num, before_qty, after_qty, "
            " biz_key, operator, remark) "
            "VALUES (:sku_id, :warehouse_id, :order_no, :change_type, :num, :before_qty, "
            "        :after_qty, :biz_key, :operator, :remark)"
        ),
        {
            "sku_id": sku_id,
            "warehouse_id": warehouse_id,
            "order_no": order_no,
            "change_type": change_type,
            "num": num,
            "before_qty": before_qty,
            "after_qty": after_qty,
            "biz_key": biz_key,
            "operator": operator,
            "remark": remark,
        },
    )


# ============================================================
# DB 侧条件更新
#
# 每条 SQL 都带业务条件（available >= n / locked >= n），这不是装饰——
# 它是防止"并发覆盖"与"重复回补导致库存虚高"的最后防线。即使超时任务和
# 用户取消同时触发，第二次的条件也会失败（docs/03 §5）。
#
# RETURNING 里的 before/after 都指**可售量**。除 lock/release/return_in/adjust
# 外，其余变更不改变 available，所以 before == after（流水仍然要记，
# 因为它是"这笔单据动过库存"的凭证）。
# ============================================================

_LOCK_SQL = text(
    """
    UPDATE inventory.sku_stock
    SET available = available - :num, locked = locked + :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id AND available >= :num
    RETURNING available + :num AS before_qty, available AS after_qty
    """
)

_RELEASE_SQL = text(
    """
    UPDATE inventory.sku_stock
    SET available = available + :num, locked = locked - :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id AND locked >= :num
    RETURNING available AS after_qty, available - :num AS before_qty
    """
)

_CONFIRM_SQL = text(
    """
    UPDATE inventory.sku_stock
    SET locked = locked - :num, frozen = frozen + :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id AND locked >= :num
    RETURNING available AS before_qty, available AS after_qty
    """
)

_DELIVER_SQL = text(
    """
    UPDATE inventory.sku_stock
    SET frozen = frozen - :num, total = total - :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id AND frozen >= :num
    RETURNING available AS before_qty, available AS after_qty
    """
)

_RETURN_IN_SQL = text(
    """
    UPDATE inventory.sku_stock
    SET available = available + :num, total = total + :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id
    RETURNING available - :num AS before_qty, available AS after_qty
    """
)

# 未发货退款：钱收了但货没出库，商品物理上一直在仓库里。
# 所以是 frozen → available（总量不变），而不是像退货入库那样加 total。
_UNSHIPPED_REFUND_SQL = text(
    """
    UPDATE inventory.sku_stock
    SET available = available + :num, frozen = frozen - :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id AND frozen >= :num
    RETURNING available - :num AS before_qty, available AS after_qty
    """
)

# 质检不合格：商品回到商家手里但不能卖，进残次品池。
# **不动 total / available** —— 它既不是可售库存，也不该计入账面总量
_DEFECTIVE_SQL = text(
    """
    UPDATE inventory.sku_stock
    SET defective = defective + :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id
    RETURNING available AS before_qty, available AS after_qty
    """
)

# 手工调整允许增减。条件里放的是"调整后不能为负"，挡住把可售调成负数
_ADJUST_SQL = text(
    """
    UPDATE inventory.sku_stock
    SET available = available + :num, total = total + :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id
      AND available + :num >= 0 AND total + :num >= 0
    RETURNING available - :num AS before_qty, available AS after_qty
    """
)

_SQL_BY_CHANGE = {
    CHANGE_LOCK: _LOCK_SQL,
    CHANGE_RELEASE: _RELEASE_SQL,
    CHANGE_CONFIRM: _CONFIRM_SQL,
    CHANGE_DELIVER: _DELIVER_SQL,
    CHANGE_RETURN_IN: _RETURN_IN_SQL,
    CHANGE_REFUND_BACK: _UNSHIPPED_REFUND_SQL,
    CHANGE_DEFECTIVE: _DEFECTIVE_SQL,
    CHANGE_ADJUST: _ADJUST_SQL,
}


async def _apply(
    session: AsyncSession,
    items: list[StockItem],
    change_type: int,
    biz_key: str,
    *,
    order_no: str | None = None,
    operator: str | None = None,
    remark: str | None = None,
) -> list[tuple[StockItem, int, int]]:
    """在调用方的事务内逐项变更 + 写流水。任一项失败就抛异常，由调用方回滚。

    返回 ``[(item, before_qty, after_qty), ...]``，供调用方写自己的业务表。
    """
    sql = _SQL_BY_CHANGE[change_type]
    # ★ 升序排列：两个订单以相反顺序锁同一组 SKU 会死锁（docs/03 §5.1）
    ordered = sorted(items, key=lambda it: (it.warehouse_id, it.sku_id))

    applied: list[tuple[StockItem, int, int]] = []
    for item in ordered:
        # 每项一个幂等键，形如 LOCK:{orderSubNo}:{skuId}（docs/03 §8）
        item_key = f"{biz_key}:{item.sku_id}"
        if not await _claim_biz_key(session, item_key):
            continue  # 这一项已经处理过

        row = (
            await session.execute(
                sql,
                {
                    "num": item.num,
                    "sku_id": item.sku_id,
                    "warehouse_id": item.warehouse_id,
                },
            )
        ).one_or_none()

        if row is None:
            # 条件不满足：库存不足、或该 SKU 在此仓没有库存记录
            raise BizError(
                ErrorCode.STOCK_INSUFFICIENT,
                f"SKU {item.sku_id} 在该仓的库存不足（需要 {item.num}）",
            )

        await _write_flow(
            session,
            sku_id=item.sku_id,
            warehouse_id=item.warehouse_id,
            change_type=change_type,
            num=item.num,
            before_qty=row.before_qty,
            after_qty=row.after_qty,
            biz_key=item_key,
            order_no=order_no,
            operator=operator,
            remark=remark,
        )
        applied.append((item, row.before_qty, row.after_qty))

    return applied


# ============================================================
# Redis 侧
# ============================================================


async def _redis_lock_batch(
    redis: Redis,
    session: AsyncSession,
    items: list[StockItem],
    biz_key: str,
    user_id: int,
) -> bool:
    """整批预占。返回是否成功（含幂等命中）。

    分片回落规则（docs/03 §10）：先试该用户散列到的片，不足则**整片**换下一片，
    **不允许跨片凑**。最多试 ``shard_count()`` 次，全试完就判定售罄。
    """
    n = len(items)
    batch_key = rk.stock_batch(biz_key)
    settings = get_settings()
    lua = get_lua()

    attempt = 0
    reloaded: set[tuple[int, int]] = set()
    while attempt < redis_stock.shard_count():
        keys = [batch_key] + [
            redis_stock.shard_key(it.sku_id, it.warehouse_id, user_id, attempt) for it in items
        ]
        args = [n, settings.inventory_lock_ttl_seconds, int(time.time())] + [
            it.num for it in items
        ]
        code, idx, _remain = await lua.stock_lock_batch(keys=keys, args=args)

        if code in (1, 2):
            return True
        if code == 0:
            attempt += 1  # 本片不够，整片换下一片
            continue

        # code == -1：第 idx 个（1-based）SKU 的分片没初始化，回源后重试同一 attempt
        item = items[idx - 1]
        token = (item.sku_id, item.warehouse_id)
        if token in reloaded:
            return False  # 回源过了还是不行，说明 DB 里根本没有这条库存记录
        reloaded.add(token)
        await redis_stock.ensure_loaded(redis, session, item.sku_id, item.warehouse_id)

    return False


async def _redis_release_batch(redis: Redis, biz_key: str) -> None:
    """补偿：把整批预占还回去。DB 事务失败时调用，用同一个 biz_key 所以幂等。"""
    await get_lua().stock_release_batch(keys=[rk.stock_batch(biz_key)], args=[int(time.time())])


async def _refresh_sold_out(redis: Redis, items: list[StockItem]) -> None:
    """变更后检查各 SKU 是否售罄，维护 ``zero`` 快速拒绝标记。

    刻意放在 Lua 脚本**之外**：售罄标记的 key 与分片是兄弟 key，
    放进去会让脚本的 KEYS 列表翻倍（docs/14 §3.2）。
    """
    for item in items:
        await redis_stock.mark_sold_out_if_empty(redis, item.sku_id, item.warehouse_id)


# ============================================================
# 对外接口（docs/03 §11）
# ============================================================


async def lock(
    session: AsyncSession,
    items: list[StockItem],
    biz_key: str,
    *,
    user_id: int = 0,
    order_no: str | None = None,
) -> None:
    """批量预占。trade 下单时调用。

    Redis 预扣在事务外先做（快速拦截 99% 的无效请求），DB 预占在调用方的
    事务内做。DB 失败时补偿 Redis，否则 Redis 会永久少掉这批库存。

    ``user_id`` 参与分片散列，让同一用户固定落同一片（docs/03 §3.3）。
    """
    if not items:
        return
    redis = get_redis()

    if not await _redis_lock_batch(redis, session, items, biz_key, user_id):
        raise BizError(ErrorCode.STOCK_SOLD_OUT, "库存不足或已售罄")

    try:
        await _apply(session, items, CHANGE_LOCK, biz_key, order_no=order_no)
    except Exception:
        # DB 侧已经失败，Redis 侧必须还回去，否则这批库存凭空消失
        await _redis_release_batch(redis, biz_key)
        raise

    await _refresh_sold_out(redis, items)


async def release(
    session: AsyncSession, items: list[StockItem], biz_key: str, *, order_no: str | None = None
) -> None:
    """释放预占（用户取消 / 超时未支付 / 支付失败）。"""
    if not items:
        return
    redis = get_redis()
    await _redis_release_batch(redis, biz_key)
    await _apply(session, items, CHANGE_RELEASE, biz_key, order_no=order_no)
    # 有货了，清掉售罄标记，否则该 SKU 会被一直快速拒绝
    for item in items:
        await redis_stock.clear_sold_out(redis, item.sku_id, item.warehouse_id)


async def unshipped_refund(
    session: AsyncSession, items: list[StockItem], biz_key: str, *, order_no: str | None = None
) -> None:
    """未发货退款的库存回补：``frozen → available``（**总量不变**）。

    ★ 与 ``release`` 的区别：``release`` 是"还没付款就取消"（``locked → available``），
    这个是"付了款、货还没出库就退款"（``frozen → available``）。两者都不能用错 ——
    用 ``release`` 会去减 ``locked``，而这时货在 ``frozen`` 里，条件更新不满足会直接报错。

    与 ``return_in`` 的区别：退货入库是商品真的出过库又回来，要 ``total += n``；
    这里商品压根没离开过仓库，``total`` 不该动。

    Redis 侧不写新的 Lua：退款是低频操作，直接按 DB 的权威值重建分片
    （``_sync_one_to_redis``），比再维护一套"frozen 转 available"的脚本更不容易错。
    """
    if not items:
        return
    redis = get_redis()
    await _apply(session, items, CHANGE_REFUND_BACK, biz_key, order_no=order_no)
    for item in items:
        # 有货了，清掉售罄标记，否则该 SKU 会被一直快速拒绝
        await redis_stock.clear_sold_out(redis, item.sku_id, item.warehouse_id)
        await _sync_one_to_redis(session, redis, item.sku_id, item.warehouse_id)


async def mark_defective(
    session: AsyncSession, items: list[StockItem], biz_key: str, *, order_no: str | None = None
) -> None:
    """质检不合格：商品进残次品池，**不回补可售库存**（docs/08 §3.3）。

    ``defective`` 不参与 ``total = available + locked + frozen`` 恒等式 ——
    商品还在，但已经不能卖了。运营后续决定翻新（defective → available/total）
    还是销毁（defective 直接减）。
    """
    if not items:
        return
    await _apply(session, items, CHANGE_DEFECTIVE, biz_key, order_no=order_no)


async def confirm(
    session: AsyncSession, items: list[StockItem], biz_key: str, *, order_no: str | None = None
) -> None:
    """预占转实扣（支付成功时）。``locked → frozen``。

    库存数在预占时已经扣过，这里只是把它从"锁定"挪到"冻结"。
    """
    if not items:
        return
    lua = get_lua()
    for item in items:
        code = await lua.stock_confirm(
            keys=[rk.stock_lock(order_no or biz_key, item.sku_id)], args=["status"]
        )
        # -1 表示 Redis 记录已被回补（超时关单与晚到的支付撞上了），
        # 属时序错乱，记警告即可 —— DB 才是账本，Redis 计数由对账任务修正
        if code == -1:
            logger.warning(
                "预占已被回补却收到支付成功，Redis 计数将由对账修正",
                extra={"sku_id": item.sku_id, "biz_key": biz_key},
            )
    await _apply(session, items, CHANGE_CONFIRM, biz_key, order_no=order_no)


async def deliver(
    session: AsyncSession, items: list[StockItem], biz_key: str, *, order_no: str | None = None
) -> None:
    """发货。``frozen → total`` 真正扣减 —— 这一步之后总量才变小。"""
    if not items:
        return
    await _apply(session, items, CHANGE_DELIVER, biz_key, order_no=order_no)


async def return_in(
    session: AsyncSession, items: list[StockItem], biz_key: str, *, order_no: str | None = None
) -> None:
    """退货入库回补。**这是退货链路唯一的库存回补入口**（docs/03 §6）。

    质检不合格的商品走报废，不进这个函数。
    """
    if not items:
        return
    redis = get_redis()
    await _apply(session, items, CHANGE_RETURN_IN, biz_key, order_no=order_no)
    # 回补后 Redis 要加回去，并清掉可能存在的售罄标记
    for item in items:
        await redis_stock.ensure_loaded(redis, session, item.sku_id, item.warehouse_id)
        await redis_stock.clear_sold_out(redis, item.sku_id, item.warehouse_id)


async def adjust(
    session: AsyncSession,
    *,
    sku_id: int,
    warehouse_id: int,
    delta: int,
    biz_key: str,
    operator: str | None = None,
    remark: str | None = None,
) -> tuple[int, int]:
    """手工调整库存（商家后台）。返回 ``(before_qty, after_qty)``。

    ``delta`` 可正可负。本期不走审批，但**必写流水并记操作人**——
    流水是唯一能回答"这批库存是被谁改的"的地方。

    DB 改完后把 Redis 重新推一遍（``SET`` 而非 ``INCR``），
    让两边立刻一致，不必等对账任务。
    """
    redis = get_redis()
    item = StockItem(sku_id=sku_id, warehouse_id=warehouse_id, num=delta)

    applied = await _apply(
        session,
        [item],
        CHANGE_ADJUST,
        biz_key,
        operator=operator,
        remark=remark,
    )
    if not applied:
        # 幂等命中：这次调整之前已经生效过，返回当前 DB 值
        row = (
            await session.execute(
                text(
                    "SELECT available FROM inventory.sku_stock "
                    "WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id"
                ),
                {"sku_id": sku_id, "warehouse_id": warehouse_id},
            )
        ).one_or_none()
        current = row.available if row else 0
        return current, current

    _, before_qty, after_qty = applied[0]
    # 调整后立刻把 DB 的值推给 Redis，避免"商家看到改成功了但用户还是买不到"
    await _sync_one_to_redis(session, redis, sku_id, warehouse_id)
    return before_qty, after_qty


async def init(
    session: AsyncSession,
    *,
    sku_id: int,
    warehouse_id: int,
    qty: int,
    biz_key: str,
    operator: str | None = None,
) -> None:
    """初始化/覆盖库存（幂等 SET）。

    商品上架时由商家后台调用。重复执行是安全的：同一 ``biz_key`` 只会生效一次。

    ★ 有在途库存时**拒绝覆盖**：把 available 直接设成 qty 会破坏
    ``total = available + locked + frozen`` 这条 CHECK 约束（约束会拦下来，
    但报的是数据库错误，对调用方毫无意义）。所以这里显式检测并给出
    业务错误，让商家知道"这个 SKU 还有未支付的订单"。
    """
    if qty < 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "库存数量不能为负")

    redis = get_redis()
    item_key = f"{biz_key}:{sku_id}"
    if not await _claim_biz_key(session, item_key):
        return

    warehouse = await session.scalar(select(Warehouse).where(Warehouse.id == warehouse_id))
    if warehouse is None:
        raise BizError(ErrorCode.NOT_FOUND, "仓库不存在")

    result = await session.execute(
        text(
            "INSERT INTO inventory.sku_stock "
            "(id, sku_id, shop_id, warehouse_id, total, available, locked, frozen, version) "
            "VALUES (:id, :sku_id, :shop_id, :warehouse_id, :qty, :qty, 0, 0, 0) "
            "ON CONFLICT (sku_id, warehouse_id) DO UPDATE "
            "SET total = EXCLUDED.total, available = EXCLUDED.available, "
            "    locked = 0, frozen = 0, version = sku_stock.version + 1, updated_at = now() "
            "WHERE sku_stock.locked = 0 AND sku_stock.frozen = 0"
        ),
        {
            "id": next_id(),
            "sku_id": sku_id,
            "shop_id": warehouse.shop_id,
            "warehouse_id": warehouse_id,
            "qty": qty,
        },
    )
    if result.rowcount == 0:
        raise BizError(ErrorCode.STOCK_IN_USE)

    await _write_flow(
        session,
        sku_id=sku_id,
        warehouse_id=warehouse_id,
        change_type=CHANGE_INIT,
        num=qty,
        before_qty=0,
        after_qty=qty,
        biz_key=item_key,
        operator=operator,
        remark="初始化库存",
    )
    await _sync_one_to_redis(session, redis, sku_id, warehouse_id)


# ============================================================
# 内部工具
# ============================================================


async def _sync_one_to_redis(
    session: AsyncSession, redis: Redis, sku_id: int, warehouse_id: int
) -> None:
    """把 DB 里这一行的值推给 Redis（``SET`` 而非 ``INCR``）。

    ★ 必须用**原生 SQL** 读，不能用 ORM 查询：``_apply`` 是用原生 UPDATE 改的值，
    而 ORM 的 identity map 里可能还留着变更前的对象（比如 ``ensure_stock_rows``
    刚 ``session.add`` 过同一行），ORM 查询会直接返回那个**没刷新的旧对象**，
    于是把过期值推给 Redis —— DB 已经是 120，Redis 却还是 0。
    """
    row = (
        await session.execute(
            text(
                "SELECT sku_id, warehouse_id, total, available, locked, frozen, version "
                "FROM inventory.sku_stock WHERE sku_id = :sku_id AND warehouse_id = :warehouse_id"
            ),
            {"sku_id": sku_id, "warehouse_id": warehouse_id},
        )
    ).one_or_none()
    if row is not None:
        await redis_stock.rebuild_from_db(redis, row)


# ============================================================
# 读侧（商家后台）
#
# ★ 依赖方向只允许 inventory → product。这里调 product 的 service 拿 SKU
#   标题/编码，**product 不反向 import inventory**，否则两个模块会成环
#   （docs/01 §2 的模块边界）。
# ============================================================


def _to_rule_out(rule: WarehouseRegionRule) -> RegionRuleOut:
    return RegionRuleOut(
        id=rule.id, region_code=rule.region_code, region_level=rule.region_level
    )


def _to_warehouse_out(
    warehouse: Warehouse, rules: list[RegionRuleOut] | None = None
) -> WarehouseOut:
    return WarehouseOut(
        id=warehouse.id,
        name=warehouse.name,
        region_code=warehouse.region_code,
        province=warehouse.province,
        city=warehouse.city,
        district=warehouse.district,
        detail=warehouse.detail,
        contact_name=warehouse.contact_name,
        contact_phone=warehouse.contact_phone,
        is_default=warehouse.is_default,
        status=warehouse.status,
        created_at=warehouse.created_at,
        rules=rules or [],
    )


async def _require_warehouse(
    session: AsyncSession, shop_id: int, warehouse_id: int
) -> Warehouse:
    """取本店的仓。带上 shop_id 查，顺手做了越权校验。"""
    warehouse = await repo.get_warehouse(session, shop_id, warehouse_id)
    if warehouse is None:
        raise BizError(ErrorCode.NOT_FOUND, "仓库不存在")
    return warehouse


async def list_warehouses(session: AsyncSession, shop_id: int) -> list[WarehouseOut]:
    """仓列表（含每个仓覆盖的区划）。

    规则**一次查全再分组**，不逐个仓查 —— 一家店没几个仓也没几条规则，
    逐个查才是 N+1。
    """
    warehouses = await repo.list_warehouses(session, shop_id)
    by_warehouse: dict[int, list[RegionRuleOut]] = {}
    for rule in await repo.list_region_rules(session, shop_id):
        by_warehouse.setdefault(int(rule.warehouse_id), []).append(_to_rule_out(rule))
    return [_to_warehouse_out(w, by_warehouse.get(int(w.id), [])) for w in warehouses]


async def create_warehouse(
    session: AsyncSession, shop_id: int, req: WarehouseCreateRequest
) -> WarehouseCreatedOut:
    """建仓。**顺手给该店全部 SKU 在这个仓补 0 库存行**。

    ★ 为什么建仓就要铺货：路由可能把这个仓指定给某些地区，而"路由到的仓里某商品
      没有库存行"会让那些订单**整单失败**。预建行让商家只需去库存页填数量。
      但**光靠这一步不够** —— 建仓之后新发布的 SKU 不会自动有这个仓的行，
      所以下单前还有一道惰性补齐（`ensure_stock_rows(..., sku_ids=...)`）。
    """
    warehouse = await repo.create_warehouse(session, shop_id, values=req.model_dump())
    stocked = await ensure_stock_rows(session, shop_id, warehouse.id)
    out = _to_warehouse_out(warehouse)
    return WarehouseCreatedOut(**out.model_dump(), stocked_skus=stocked)


async def update_warehouse(
    session: AsyncSession, shop_id: int, warehouse_id: int, req: WarehouseUpdateRequest
) -> WarehouseOut:
    """改仓（名称 / 地址 / 联系人）。部分更新：只写传了的字段。

    ★ 地址里的空串是**有意义的**（清空那一项），所以过滤时只丢 ``None``（= 没传）。
    """
    warehouse = await _require_warehouse(session, shop_id, warehouse_id)
    values = {
        key: value
        for key, value in req.model_dump(exclude_unset=True).items()
        if value is not None
    }
    await repo.update_warehouse_fields(session, warehouse.id, values)
    refreshed = await _require_warehouse(session, shop_id, warehouse_id)
    return _to_warehouse_out(refreshed, await _rules_of(session, shop_id, warehouse_id))


async def set_warehouse_default(
    session: AsyncSession, shop_id: int, warehouse_id: int
) -> WarehouseOut:
    """把某个仓设为默认仓（路由的兜底）。

    停用的仓不能当默认仓 —— 兜底的本意是"总有一个能发货的仓"。
    """
    warehouse = await _require_warehouse(session, shop_id, warehouse_id)
    if warehouse.status != WAREHOUSE_ENABLED:
        raise BizError(ErrorCode.VALIDATION_ERROR, "停用的仓不能设为默认仓，请先启用它")
    await repo.set_default_warehouse(session, shop_id, warehouse_id)
    refreshed = await _require_warehouse(session, shop_id, warehouse_id)
    return _to_warehouse_out(refreshed, await _rules_of(session, shop_id, warehouse_id))


async def set_warehouse_status(
    session: AsyncSession, shop_id: int, warehouse_id: int, status: int
) -> WarehouseOut:
    """启用 / 停用。

    ★ **默认仓不允许停用**："每店必有默认仓"是路由兜底的前提，停了它这个店就没仓可发。
      要停就先设另一个为默认。
    ★ 停用只影响**新订单**的路由；历史订单落在这个仓的照常发货、售后照常回补 ——
      那些路径读的是订单上记录的仓，根本不看仓状态。
    """
    warehouse = await _require_warehouse(session, shop_id, warehouse_id)
    if status == WAREHOUSE_DISABLED and warehouse.is_default:
        raise BizError(
            ErrorCode.VALIDATION_ERROR,
            "默认仓不能停用 —— 收货地址没命中任何区域规则时，订单要靠它兜底。请先把别的仓设为默认",
        )
    await repo.update_warehouse_fields(session, warehouse.id, {"status": status})
    refreshed = await _require_warehouse(session, shop_id, warehouse_id)
    return _to_warehouse_out(refreshed, await _rules_of(session, shop_id, warehouse_id))


async def _rules_of(
    session: AsyncSession, shop_id: int, warehouse_id: int
) -> list[RegionRuleOut]:
    return [
        _to_rule_out(r)
        for r in await repo.list_region_rules(session, shop_id)
        if int(r.warehouse_id) == warehouse_id
    ]


async def replace_region_rules(
    session: AsyncSession, shop_id: int, warehouse_id: int, codes: Sequence[str]
) -> list[RegionRuleOut]:
    """**整体替换**这个仓覆盖的区划（照 freight 的 region rules 编辑方式）。

    ★ 唯一键是 ``(shop_id, region_code)``：一个地方只能由一个仓发货。所以提交前先查
      "这些区划有没有被别的仓占着"，占了就报**人话**（哪个地区、归哪个仓），
      而不是丢一个数据库唯一约束错误给商家。
    """
    warehouse = await _require_warehouse(session, shop_id, warehouse_id)
    unique_codes = list(dict.fromkeys(codes))  # 同一份提交里的重复项

    taken = await repo.region_rule_owners(
        session, shop_id=shop_id, codes=unique_codes, exclude_warehouse_id=warehouse.id
    )
    if taken:
        names = {int(w.id): w.name for w in await repo.list_warehouses(session, shop_id)}
        detail = "、".join(
            f"{code}（现在是「{names.get(wh_id, '其它仓')}」发货）" for code, wh_id in taken.items()
        )
        raise BizError(ErrorCode.VALIDATION_ERROR, f"这些地区已经分给别的仓了：{detail}")

    await repo.delete_region_rules_of_warehouse(session, warehouse.id)
    await repo.insert_region_rules(
        session, shop_id=shop_id, warehouse_id=warehouse.id, codes=unique_codes
    )
    return await _rules_of(session, shop_id, warehouse_id)


# ============================================================
# 发货仓路由
# ============================================================
@dataclass(frozen=True, slots=True)
class WarehouseChoice:
    """按仓择仓的结果。

    ★ **三种结局必须分开**，因为补救办法不同（文案见 ``trade`` 与 ``promotion.checkout``）：

    - ``warehouse is None`` —— 该店连一个启用的仓都没有 → "还没配置发货仓库"
    - ``covered=True`` —— ``warehouse`` 能一次盖住这一单的全部货 → 正常发货
    - ``covered=False`` —— **没有任何候选仓**能盖住全部。``warehouse`` 仍给出**首个候选**
      （报错时用来点名），``short_sku_ids`` 列出**哪个仓都盖不住**的那些 SKU。

    ``short_sku_ids`` 为空而仍未覆盖，就是 :attr:`split_across`：每条商品各自都有仓够、
    只是凑不到同一个仓 —— 那种情况**分开下单真的能解决**（择仓是按整单判的）。
    """

    warehouse: Warehouse | None
    covered: bool
    short_sku_ids: frozenset[int] = frozenset()

    @property
    def split_across(self) -> bool:
        """没盖住，但**没有哪一条是"哪儿都没货"** → 只是分散在不同仓，分开下单可解。"""
        return not self.covered and not self.short_sku_ids


async def route_warehouse(
    session: AsyncSession,
    *,
    shop_id: int,
    region_code: str | None,
    need: Mapping[int, int],
) -> WarehouseChoice:
    """该店发往这个区划用哪个仓 —— **规则优先，规则仓盖不住时按候选链往后退**。

    ``need`` 是 ``{sku_id: num}``：**整单必须能落进同一个仓**，所以判定是"这个仓对这些
    SKU 的 available 都 >= 各自的数量"；不做逐件挑仓（那要把一个子单拆成多张发货单，
    见 ``routing`` 的模块文档）。

    ★ 判据用 **DB 的 ``available``**（``sku_stock`` 是账本，Redis 只是前置闸）。
    ★ 只读、不锁。真正的扣减在下单的 ``lock`` 里；两者之间被抢空是可能的，那时下单会
      失败 —— 这是接受的（不重试，理由见 ``trade.service.create_order``）。
    """
    warehouses = await repo.list_warehouses(session, shop_id)
    enabled = {int(w.id): w for w in warehouses if routing.is_enabled(w)}
    default_wh = next((w for w in warehouses if w.is_default), None)
    rules = await repo.list_region_rules(session, shop_id)
    candidates = routing.warehouse_candidates(
        rules=rules, enabled=enabled, default_wh=default_wh, region_code=region_code or ""
    )
    if not candidates:
        return WarehouseChoice(warehouse=None, covered=False)

    items = {int(sku_id): int(num) for sku_id, num in need.items() if int(num) > 0}
    if not items:  # 没有要发的货 —— 只按规则给仓，不必查库存
        return WarehouseChoice(warehouse=candidates[0], covered=True)

    avail = await repo.available_of(
        session,
        warehouse_ids=[int(w.id) for w in candidates],
        sku_ids=list(items),
    )
    for warehouse in candidates:
        wid = int(warehouse.id)
        if all(avail.get((sku_id, wid), 0) >= num for sku_id, num in items.items()):
            return WarehouseChoice(warehouse=warehouse, covered=True)

    # 一个仓都盖不住：把"哪条是哪儿都没货"记下来 —— 文案要按它分两种（见 WarehouseChoice）
    short = frozenset(
        sku_id
        for sku_id, num in items.items()
        if not any(avail.get((sku_id, int(w.id)), 0) >= num for w in candidates)
    )
    return WarehouseChoice(warehouse=candidates[0], covered=False, short_sku_ids=short)


async def route_warehouses(
    session: AsyncSession,
    *,
    region_code: str | None,
    sku_to_shop: Mapping[int, int],
    need: Mapping[int, int],
) -> dict[int, WarehouseChoice]:
    """``sku_id → WarehouseChoice``。按店铺分组，**每店只算一次**择仓。

    调用方本来就知道每个 SKU 属于哪个店（算价拿的是 CalcItem / SKU 快照），
    所以这里收 ``sku_to_shop`` 而不是自己去查商品 —— 省一次批量查询。

    ★ ``need``（``{sku_id: num}``）决定了"这一单要多少" —— **择仓是按整单判的**，
      这正是"分开下单能解决货分散在不同仓"的原因。
    ★ 没有可用仓的店铺，它的 SKU **不会出现在返回值里**；调用方按"这个商品没有发货仓"
      处理（``trade`` 会拒单）。
    ★ 同一个店的所有 SKU 一定拿到**同一个** choice —— 择仓只由（店铺, 收货区划, 整单数量）
      决定，与 SKU 逐个无关。这正是"仓可以记在子单上"的依据。
    """
    if not sku_to_shop:
        return {}
    by_shop: dict[int, list[int]] = {}
    for sku_id, shop_id in sku_to_shop.items():
        by_shop.setdefault(int(shop_id), []).append(int(sku_id))

    result: dict[int, WarehouseChoice] = {}
    for shop_id, sku_ids in by_shop.items():
        choice = await route_warehouse(
            session,
            shop_id=shop_id,
            region_code=region_code,
            need={sku_id: int(need[sku_id]) for sku_id in sku_ids if sku_id in need},
        )
        if choice.warehouse is None:
            continue
        for sku_id in sku_ids:
            result[sku_id] = choice
    return result


def warehouses_of(choices: Mapping[int, WarehouseChoice]) -> dict[int, int]:
    """把择仓结果压成 ``sku_id → warehouse_id`` —— 给 ``freight.estimate`` 用。

    ★ 没盖住（``covered=False``）的也照样带上它**首个候选**的 id：算价仍要算出一个完整
      的运费给买家看，是否可下单由 ``can_submit`` 单独说（失败文案也由 ``trade`` 统一给）。
      该店没有仓的 SKU 不在返回值里，与改造前一致。
    """
    return {
        int(sku_id): int(choice.warehouse.id)
        for sku_id, choice in choices.items()
        if choice.warehouse is not None
    }


async def default_warehouse_id(session: AsyncSession, shop_id: int) -> int | None:
    """该店默认仓的 id（没有仓时 ``None``）。

    给"迁移前的老订单"回退用：那些 ``order_sub.warehouse_id`` 为空，发货/回补要落一个仓。
    ★ 回退到**默认仓**而不是重新路由 —— 默认仓是显式配置、不随区域规则变，
      所以不会把退货退到一个当初根本没发货的仓。
    """
    warehouse = await repo.get_default_warehouse(session, shop_id)
    return int(warehouse.id) if warehouse is not None else None


async def ensure_stock_rows(session: AsyncSession, shop_id: int, warehouse_id: int) -> int:
    """给该仓库**还没有库存记录**的 SKU 补一条 0 库存。返回新建条数。

    ★ 两个调用点：**建仓时**（顺手铺好，商家只需去库存页填数量）、
      **库存页**（`list_stock_out` 的 `sync_missing`，商家选哪个仓就补哪个仓）。

    ★ **刻意不放在下单路径上。** 曾经想在"下单前给路由到的仓补这次要买的 SKU"，
      但那跑在订单事务里：行不存在 → 预占必然失败 → 整个事务回滚 → 补出来的行也
      一起没了。也就是说它**恰好在唯一需要它的场合不生效**，是死重。
      真正的兜底是库存页那条（商家选仓时补齐）加上可操作的报错（见 trade 那边）。

    幂等：已经有记录（含 available=0 的）不会被覆盖，所以可以反复调。
    """
    sku_ids = await product_service.list_shop_sku_ids(session, shop_id)
    if not sku_ids:
        return 0

    existing = set(
        await session.scalars(
            select(SkuStock.sku_id).where(
                SkuStock.shop_id == shop_id,
                SkuStock.warehouse_id == warehouse_id,
                SkuStock.sku_id.in_(sku_ids),
            )
        )
    )
    missing = [sid for sid in sku_ids if sid not in existing]
    if not missing:
        return 0

    for sku_id in missing:
        session.add(
            SkuStock(
                id=next_id(),
                sku_id=sku_id,
                shop_id=shop_id,
                warehouse_id=warehouse_id,
                total=0,
                available=0,
                locked=0,
                frozen=0,
                version=0,
            )
        )
    await session.flush()
    return len(missing)


async def count_out_of_stock(session: AsyncSession, *, shop_id: int) -> int:
    """可售为 0 的库存行数。给 AI 助手的"店铺概览"用（docs/20 §3）。

    ★ 与 ``list_stock_out`` 用**同一份**已删 SKU 名单 —— 两个口径不一致的话，
      助手说"有 3 处没货"而库存页上找不到那几行，用户只会觉得助手在胡说。
    """
    deleted_sku_ids = await product_service.list_deleted_sku_ids(session, shop_id)
    return await repo.count_out_of_stock(session, shop_id, exclude_sku_ids=deleted_sku_ids)


async def list_stock_out(
    session: AsyncSession,
    shop_id: int,
    *,
    warehouse_id: int | None = None,
    sku_id: int | None = None,
    cursor: str | None = None,
    limit: int = 20,
    sync_missing: bool = True,
) -> StockListOut:
    """库存列表。

    ``sync_missing`` 为真时（默认）先补齐没有库存记录的 SKU —— 商家发布新商品后
    能在库存页直接看到它，这是库存页的主路径。按 sku_id 精确查询时跳过，
    因为那种查询不需要补齐。
    """
    if sync_missing and sku_id is None:
        target_wh = warehouse_id
        if target_wh is None:
            default_wh = await repo.get_default_warehouse(session, shop_id)
            target_wh = default_wh.id if default_wh else None
        if target_wh is not None:
            await ensure_stock_rows(session, shop_id, target_wh)

    # ★ 已软删商品的库存行不展示。行还在库里（可能有预占对应未发货订单，
    #   删掉会让发货/解锁对不上账），但商品已经从商城消失，摆在库存页只会让
    #   商家以为"没删干净"。过滤必须在 SQL 层做，见 repo.list_stock。
    deleted_sku_ids = await product_service.list_deleted_sku_ids(session, shop_id)
    rows = await repo.list_stock(
        session,
        shop_id,
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        exclude_sku_ids=deleted_sku_ids,
        cursor=cursor,
        limit=limit,
    )
    if not rows:
        return StockListOut(items=[], next_cursor=None, has_more=False)

    warehouses = {w.id: w.name for w in await repo.list_warehouses(session, shop_id)}
    # only_on_shelf=False：商家要能看自己草稿/已下架商品的库存
    skus = {
        s.id: s
        for s in await product_service.batch_get_skus(
            session, [r.sku_id for r in rows], only_on_shelf=False
        )
    }

    items: list[StockItemOut] = []
    for row in rows:
        sku = skus.get(row.sku_id)
        items.append(
            StockItemOut(
                sku_id=row.sku_id,
                warehouse_id=row.warehouse_id,
                warehouse_name=warehouses.get(row.warehouse_id, ""),
                # 已软删商品的 SKU 上面就被排掉了，取不到只可能是数据异常
                # （SKU 指向了不存在的 SPU）。标题留空，前端会退回显示 skuId，
                # 比整行静默消失强 —— 那种情况商家会以为库存丢了。
                sku_code=(sku.sku_code or "") if sku else "",
                spec_text=sku.spec_text if sku else "",
                spu_title=sku.title if sku else "",
                total=row.total,
                available=row.available,
                locked=row.locked,
                frozen=row.frozen,
                updated_at=row.updated_at,
            )
        )

    has_more = len(rows) == limit
    return StockListOut(
        items=items,
        next_cursor=str(rows[-1].id) if has_more else None,
        has_more=has_more,
    )


async def list_flows_out(
    session: AsyncSession,
    shop_id: int,
    *,
    sku_id: int | None = None,
    warehouse_id: int | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> StockFlowListOut:
    rows = await repo.list_flows(
        session,
        shop_id,
        sku_id=sku_id,
        warehouse_id=warehouse_id,
        cursor=cursor,
        limit=limit,
    )
    items = [
        StockFlowOut(
            id=row.id,
            sku_id=row.sku_id,
            warehouse_id=row.warehouse_id,
            order_no=row.order_no,
            change_type=row.change_type,
            change_type_text=CHANGE_TYPE_TEXT.get(row.change_type, str(row.change_type)),
            num=row.num,
            before_qty=row.before_qty,
            after_qty=row.after_qty,
            operator=row.operator,
            remark=row.remark,
            created_at=row.created_at,
        )
        for row in rows
    ]
    has_more = len(rows) == limit
    return StockFlowListOut(
        items=items,
        next_cursor=str(rows[-1].id) if has_more and rows else None,
        has_more=has_more,
    )


async def adjust_for_merchant(
    session: AsyncSession,
    shop_id: int,
    req: StockAdjustRequest,
    *,
    operator: str,
    idempotency_key: str,
) -> StockAdjustOut:
    """商家手工调整的 HTTP 入口。先做归属校验，再走 service.adjust。"""
    warehouse = await repo.get_warehouse(session, shop_id, req.warehouse_id)
    if warehouse is None:
        raise BizError(ErrorCode.NOT_FOUND, "仓库不存在")

    row = await repo.get_stock(session, req.sku_id, req.warehouse_id)
    if row is None:
        raise BizError(ErrorCode.NOT_FOUND, "该商品在此仓库还没有库存记录，请先设置库存")
    if row.shop_id != shop_id:
        raise BizError(ErrorCode.FORBIDDEN, "无权调整其他店铺的库存")

    before, after = await adjust(
        session,
        sku_id=req.sku_id,
        warehouse_id=req.warehouse_id,
        delta=req.delta,
        biz_key=f"ADJUST:{idempotency_key}",
        operator=operator,
        remark=req.remark,
    )
    return StockAdjustOut(
        sku_id=req.sku_id,
        warehouse_id=req.warehouse_id,
        before_qty=before,
        after_qty=after,
        available=after,
    )


# ============================================================
# 读侧（买家）
# ============================================================


async def sku_display(session: AsyncSession, sku_id: int) -> tuple[str, bool]:
    """买家侧的库存档位。返回 ``(文案, 是否售罄)``。

    **不回传真实库存**，只给取整后的档位（docs/03 §9）。
    """
    available = await repo.sum_available(session, sku_id)
    text, _shown = display_stock_text(available)
    return text, available <= 0


async def batch_available(session: AsyncSession, sku_ids: Sequence[int]) -> dict[int, int]:
    """批量取可售总量。**这是"真实值"**，给订单/购物车这类内部判断用。

    买家侧的**展示**要走 :func:`sku_display`（取整后的档位），
    不要把这个函数的返回值直接甩给前端 —— 那会把真实库存喂给爬虫。
    """
    return await repo.sum_available_by_skus(session, list(sku_ids))
