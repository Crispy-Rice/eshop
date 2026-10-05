"""trade 模块的数据访问层。

只读写 ``trade`` schema 下的表。

**这里没有任何直接改状态的 UPDATE** —— 状态变更全走 ``service.transit()``。
唯一的例外是母单的 CAS 关闭（``close_main_if_unpaid``），它本身就是幂等的。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.trade.models import (
    ORDER_REFUNDED,
    ORDER_REFUNDING,
    ORDER_WAIT_PAY,
    DeliveryItem,
    DeliveryOrder,
    OrderDiscountSnapshot,
    OrderItem,
    OrderMain,
    OrderStateFlow,
    OrderSub,
)


# ============================================================
# 母单
# ============================================================
async def get_main_by_no(session: AsyncSession, order_main_no: str) -> OrderMain | None:
    return await session.scalar(
        select(OrderMain).where(OrderMain.order_main_no == order_main_no)
    )


async def get_main_for_user(
    session: AsyncSession, order_main_no: str, user_id: int
) -> OrderMain | None:
    """带归属校验的取母单 —— 顺便就挡住了"查到别人的订单"。"""
    return await session.scalar(
        select(OrderMain).where(
            OrderMain.order_main_no == order_main_no, OrderMain.user_id == user_id
        )
    )


async def get_main_by_request(
    session: AsyncSession, user_id: int, request_id: str
) -> OrderMain | None:
    """按幂等键找已创建的订单。下单接口重放时用它返回原订单。"""
    return await session.scalar(
        select(OrderMain).where(
            OrderMain.user_id == user_id, OrderMain.request_id == request_id
        )
    )


async def insert_main(session: AsyncSession, main: OrderMain) -> OrderMain:
    session.add(main)
    await session.flush()
    return main


async def close_main_if_unpaid(
    session: AsyncSession, order_main_no: str, *, reason: str
) -> OrderMain | None:
    """CAS 关闭母单。**这本身就是幂等的** —— ``rowcount = 0`` 说明已支付或已关闭。

    返回关闭后的母单，或者 None（表示不需要处理）。
    """
    # 用 returning 判断是否真的改到了行 —— 带了 RETURNING 的语句拿不到
    # ``rowcount``（asyncpg 走的是游标结果），所以取回 id 再看是不是 None
    closed_id = await session.scalar(
        update(OrderMain)
        .where(OrderMain.order_main_no == order_main_no, OrderMain.status == ORDER_WAIT_PAY)
        .values(
            status=50,
            close_time=func.now(),
            updated_at=func.now(),
            version=OrderMain.version + 1,
        )
        .returning(OrderMain.id)
    )
    if closed_id is None:
        return None
    return await get_main_by_no(session, order_main_no)


async def mark_main_paid(
    session: AsyncSession,
    order_main_no: str,
    *,
    paid_amount: int,
    status: int,
    pay_status: int,
) -> bool:
    """支付成功：更新母单的实付、状态与支付状态。条件是"还在待付款"。"""
    result = await session.execute(
        update(OrderMain)
        .where(OrderMain.order_main_no == order_main_no, OrderMain.status == ORDER_WAIT_PAY)
        .values(
            paid_amount=paid_amount,
            status=status,
            pay_status=pay_status,
            pay_time=func.now(),
            updated_at=func.now(),
            version=OrderMain.version + 1,
        )
    )
    return result.rowcount > 0


async def update_main_status(
    session: AsyncSession, order_main_no: str, *, status: int, pay_status: int | None = None
) -> None:
    """聚合后回写母单状态。不走 transit —— 母单状态是**派生值**，没有自己的状态机。"""
    values: dict = {
        "status": status,
        "updated_at": func.now(),
        "version": OrderMain.version + 1,
    }
    if pay_status is not None:
        values["pay_status"] = pay_status
    await session.execute(
        update(OrderMain).where(OrderMain.order_main_no == order_main_no).values(**values)
    )


async def update_main_pay_status(
    session: AsyncSession, order_main_no: str, *, pay_status: int
) -> None:
    """只更新母单的支付状态。履约状态由 ``update_main_status`` 管，两者正交。"""
    await session.execute(
        update(OrderMain)
        .where(OrderMain.order_main_no == order_main_no)
        .values(
            pay_status=pay_status,
            updated_at=func.now(),
            version=OrderMain.version + 1,
        )
    )


async def list_mains_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    status: int | None = None,
    cursor: tuple[datetime, int] | None = None,
    limit: int = 20,
) -> list[OrderMain]:
    """我的订单。**键集游标分页**，不用 OFFSET（越翻越慢）。"""
    stmt: Select = select(OrderMain).where(OrderMain.user_id == user_id)
    if status is not None:
        stmt = stmt.where(OrderMain.status == status)
    if cursor is not None:
        last_time, last_id = cursor
        stmt = stmt.where(
            (OrderMain.create_time < last_time)
            | ((OrderMain.create_time == last_time) & (OrderMain.id < last_id))
        )
    stmt = stmt.order_by(OrderMain.create_time.desc(), OrderMain.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def list_timeout_mains(session: AsyncSession, *, limit: int = 500) -> list[str]:
    """超时未支付的母单号。命中部分索引 ``idx_order_main_pay_deadline``。"""
    return list(
        await session.scalars(
            select(OrderMain.order_main_no)
            .where(OrderMain.status == ORDER_WAIT_PAY, OrderMain.pay_deadline < func.now())
            .order_by(OrderMain.pay_deadline)
            .limit(limit)
        )
    )


# ============================================================
# 子单
# ============================================================
async def get_sub(session: AsyncSession, order_sub_no: str) -> OrderSub | None:
    return await session.scalar(select(OrderSub).where(OrderSub.order_sub_no == order_sub_no))


async def get_sub_for_update(session: AsyncSession, order_sub_no: str) -> OrderSub | None:
    """加**行锁**读子单。状态流转的第一步（docs/07 §4.4）。

    行锁挡住"两个请求同时改同一个子单"，是三重保护里的第一层。
    """
    return await session.scalar(
        select(OrderSub).where(OrderSub.order_sub_no == order_sub_no).with_for_update()
    )


async def list_subs(session: AsyncSession, order_main_no: str) -> list[OrderSub]:
    return list(
        await session.scalars(
            select(OrderSub)
            .where(OrderSub.order_main_no == order_main_no)
            .order_by(OrderSub.order_sub_no)
        )
    )


async def get_subs_by_nos(
    session: AsyncSession, sub_nos: Sequence[str]
) -> dict[str, OrderSub]:
    """批量取子单，返回 ``{子单号: 子单}``。售后列表要拿店铺名，避免 N+1。"""
    if not sub_nos:
        return {}
    rows = await session.scalars(select(OrderSub).where(OrderSub.order_sub_no.in_(sub_nos)))
    return {s.order_sub_no: s for s in rows}


async def get_item_with_sub(
    session: AsyncSession, order_item_id: int
) -> tuple[OrderItem, OrderSub] | None:
    """取订单项 + 它所属的子单（一条 JOIN）。**评价资格判定用**。

    一次取回两者，是因为判定同时要看订单项（退款数量）与子单（签收时间、状态）。
    """
    row = (
        await session.execute(
            select(OrderItem, OrderSub)
            .join(OrderSub, OrderSub.order_sub_no == OrderItem.order_sub_no)
            .where(OrderItem.id == order_item_id)
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


async def list_reviewable_items(
    session: AsyncSession,
    user_id: int,
    *,
    within_days: int,
    cursor: int | None = None,
    limit: int = 20,
) -> list[OrderItem]:
    """待评价的订单项 —— **评价模块的候选集**。

    条件就是"可评价"的那几条（与 review 的资格判定保持一致）：

    - 已签收（``receive_time`` 非空）
    - 在评价窗口内（签收后 ``within_days`` 天）
    - 该订单项**没有全部退掉**（部分退款仍可评价，docs/12 §9）
    - 子单不在售后中（``status = 60``）

    ★ **不看 ``sub.status == 40``**：部分退款会把子单推到 70，用 40 兜会把
    剩余商品漏掉（这正是 docs/12 §9 要避免的）。

    ``within_days`` 由调用方传入 —— 窗口长度是**评价模块的规则**，
    放在 trade 里会让依赖方向反过来。

    这里只返回"候选"，是否**已经评过**由 review 用自己的表判断 ——
    trade 不读 ``review`` schema，模块边界保持单向。
    """
    stmt = (
        select(OrderItem)
        .join(OrderSub, OrderSub.order_sub_no == OrderItem.order_sub_no)
        .where(
            OrderSub.user_id == user_id,
            OrderSub.receive_time.isnot(None),
            OrderSub.receive_time > func.now() - timedelta(days=within_days),
            OrderSub.status != ORDER_REFUNDING,
            OrderItem.refunded_num < OrderItem.num,
        )
    )
    if cursor is not None:
        # 订单项 id 是雪花，单调递增 → 按 id 倒序就是"最近买的在前"，游标也简单
        stmt = stmt.where(OrderItem.id < cursor)
    stmt = stmt.order_by(OrderItem.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def set_sub_reviewed(
    session: AsyncSession, order_sub_no: str, *, reviewed: bool
) -> None:
    """写子单的 ``is_reviewed``。trade 拥有该列，所以写入口在这里。

    它只是**展示用缓存**（"我的订单"上的待评价角标），不是正确性依据 ——
    真正的判断是"这个订单项有没有首评"。
    """
    await session.execute(
        update(OrderSub)
        .where(OrderSub.order_sub_no == order_sub_no)
        .values(
            is_reviewed=reviewed,
            updated_at=func.now(),
            version=OrderSub.version + 1,
        )
    )


async def insert_subs(session: AsyncSession, subs: list[OrderSub]) -> None:
    session.add_all(subs)
    await session.flush()


async def cas_sub_status(
    session: AsyncSession, order_sub_no: str, *, from_status: int, to_status: int
) -> bool:
    """CAS 更新子单状态 —— 三重保护里的第三层。

    条件里带 ``status = :from``，即使行锁因为某种原因没生效，
    这一步也能挡住并发（``rowcount = 0``）。
    """
    result = await session.execute(
        update(OrderSub)
        .where(OrderSub.order_sub_no == order_sub_no, OrderSub.status == from_status)
        .values(status=to_status, updated_at=func.now(), version=OrderSub.version + 1)
    )
    return result.rowcount > 0


async def update_sub_fields(session: AsyncSession, order_sub_no: str, values: dict) -> None:
    """更新子单的**非状态**字段（发货时间、自动收货时间等）。

    状态必须走 ``cas_sub_status`` / ``transit``，不走这里。
    """
    await session.execute(
        update(OrderSub)
        .where(OrderSub.order_sub_no == order_sub_no)
        .values(updated_at=func.now(), version=OrderSub.version + 1, **values)
    )


async def list_subs_for_shop(
    session: AsyncSession,
    shop_id: int,
    *,
    status: int | None = None,
    cursor: tuple[datetime, int] | None = None,
    limit: int = 20,
) -> list[OrderSub]:
    """商家订单列表。走 ``idx_order_sub_shop``。"""
    stmt: Select = select(OrderSub).where(OrderSub.shop_id == shop_id)
    if status is not None:
        stmt = stmt.where(OrderSub.status == status)
    if cursor is not None:
        last_time, last_id = cursor
        stmt = stmt.where(
            (OrderSub.create_time < last_time)
            | ((OrderSub.create_time == last_time) & (OrderSub.id < last_id))
        )
    stmt = stmt.order_by(OrderSub.create_time.desc(), OrderSub.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def list_auto_receive_subs(session: AsyncSession, *, limit: int = 500) -> list[str]:
    """到点该自动确认收货的子单号。命中部分索引 ``idx_order_sub_auto_finish``。"""
    from app.modules.trade.models import ORDER_WAIT_RECEIVE

    return list(
        await session.scalars(
            select(OrderSub.order_sub_no)
            .where(
                OrderSub.status == ORDER_WAIT_RECEIVE,
                OrderSub.auto_finish_time.isnot(None),
                OrderSub.auto_finish_time < func.now(),
            )
            .order_by(OrderSub.auto_finish_time)
            .limit(limit)
        )
    )


async def count_subs_by_status(session: AsyncSession, order_main_no: str) -> dict[int, int]:
    """按状态统计子单数 —— 聚合母单状态要用。"""
    rows = await session.execute(
        select(OrderSub.status, func.count())
        .where(OrderSub.order_main_no == order_main_no)
        .group_by(OrderSub.status)
    )
    return {int(status): int(n) for status, n in rows}


# ============================================================
# 订单项与快照
# ============================================================
async def insert_items(session: AsyncSession, items: list[OrderItem]) -> None:
    session.add_all(items)
    await session.flush()


async def list_items_of_main(session: AsyncSession, order_main_no: str) -> list[OrderItem]:
    return list(
        await session.scalars(
            select(OrderItem).where(OrderItem.order_main_no == order_main_no).order_by(OrderItem.id)
        )
    )


async def spu_has_order_items(session: AsyncSession, spu_id: int) -> bool:
    """这个商品有没有任何订单项。

    给 product 模块判"规格还能不能改"用：已成交的商品不该再被改规格。
    走 ``exists`` 而不是 ``count`` —— 只要有一个就够，不必数完。
    """
    found = await session.scalar(select(OrderItem.id).where(OrderItem.spu_id == spu_id).limit(1))
    return found is not None


async def list_items_of_mains(
    session: AsyncSession, main_nos: Sequence[str]
) -> list[OrderItem]:
    """批量取多个母单的订单项。

    **列表页必须批量取**：一页 20 个订单逐个查就是 20 次往返（N+1），
    而订单列表是最常刷新的页面之一。
    """
    if not main_nos:
        return []
    return list(
        await session.scalars(
            select(OrderItem).where(OrderItem.order_main_no.in_(main_nos)).order_by(OrderItem.id)
        )
    )


async def list_items_of_subs(
    session: AsyncSession, sub_nos: Sequence[str]
) -> list[OrderItem]:
    if not sub_nos:
        return []
    return list(
        await session.scalars(
            select(OrderItem)
            .where(OrderItem.order_sub_no.in_(sub_nos))
            .order_by(OrderItem.id)
        )
    )


async def list_items_for_update(session: AsyncSession, order_sub_no: str) -> list[OrderItem]:
    """加**行锁**读子单的订单项。售后申请的第一步（docs/08 §9）。

    锁住订单项是为了挡住"同一行并发申请两个售后"—— 第二个申请会等锁，
    拿到锁后看到 ``refunding_num`` 已被预占，可退数量不足而失败。
    """
    return list(
        await session.scalars(
            select(OrderItem)
            .where(OrderItem.order_sub_no == order_sub_no)
            .order_by(OrderItem.id)
            .with_for_update()
        )
    )


# ============================================================
# 退款（售后模块驱动）
# ============================================================
# 这几个函数写的是 trade 自己的表，所以放在这里 —— 模块边界要求
# "谁拥有 schema 谁提供写入口"，aftersale 不能直接 UPDATE trade.order_item。


async def add_item_refunding_num(session: AsyncSession, order_item_id: int, num: int) -> bool:
    """申请售后时预占件数。条件是"预占后不超过购买数量"。

    ``rowcount = 0`` 说明可退数量不足（或并发被抢先），调用方据此拒绝。
    """
    result = await session.execute(
        update(OrderItem)
        .where(
            OrderItem.id == order_item_id,
            OrderItem.refunded_num + OrderItem.refunding_num + num <= OrderItem.num,
        )
        .values(refunding_num=OrderItem.refunding_num + num)
    )
    return result.rowcount > 0


async def release_item_refunding_num(session: AsyncSession, order_item_id: int, num: int) -> bool:
    """释放预占（拒绝/撤销/质检不通过）。"""
    result = await session.execute(
        update(OrderItem)
        .where(OrderItem.id == order_item_id, OrderItem.refunding_num >= num)
        .values(refunding_num=OrderItem.refunding_num - num)
    )
    return result.rowcount > 0


async def settle_item_refund(
    session: AsyncSession, order_item_id: int, num: int, amount: int
) -> bool:
    """退款成功：在退 → 已退，并累加已退金额。

    条件是"在退的件数够扣、已退件数不超买、已退金额不超实付"——
    三道条件同时满足才动，任一条不满足就整笔拒绝（返回 False）。
    """
    result = await session.execute(
        update(OrderItem)
        .where(
            OrderItem.id == order_item_id,
            OrderItem.refunding_num >= num,
            OrderItem.refunded_num + num <= OrderItem.num,
            OrderItem.refunded_amount + amount <= OrderItem.payable_amount,
        )
        .values(
            refunding_num=OrderItem.refunding_num - num,
            refunded_num=OrderItem.refunded_num + num,
            refunded_amount=OrderItem.refunded_amount + amount,
        )
    )
    return result.rowcount > 0


async def add_sub_refunded_amount(session: AsyncSession, order_sub_no: str, amount: int) -> bool:
    """累加子单已退金额（商品款 + 运费）。条件是"不超应付"。"""
    result = await session.execute(
        update(OrderSub)
        .where(
            OrderSub.order_sub_no == order_sub_no,
            OrderSub.refunded_amount + amount <= OrderSub.payable_amount,
        )
        .values(
            refunded_amount=OrderSub.refunded_amount + amount,
            updated_at=func.now(),
            version=OrderSub.version + 1,
        )
    )
    return result.rowcount > 0


async def add_main_refunded_amount(session: AsyncSession, order_main_no: str, amount: int) -> bool:
    """累加母单已退金额。条件是"不超实付"。"""
    result = await session.execute(
        update(OrderMain)
        .where(
            OrderMain.order_main_no == order_main_no,
            OrderMain.refunded_amount + amount <= OrderMain.paid_amount,
        )
        .values(
            refunded_amount=OrderMain.refunded_amount + amount,
            updated_at=func.now(),
            version=OrderMain.version + 1,
        )
    )
    return result.rowcount > 0


async def count_refunded_subs(session: AsyncSession, order_main_no: str) -> tuple[int, int]:
    """返回 ``(子单总数, 已退款子单数)``。用于推出母单的 pay_status。"""
    total = await session.scalar(
        select(func.count()).select_from(OrderSub).where(OrderSub.order_main_no == order_main_no)
    )
    refunded = await session.scalar(
        select(func.count())
        .select_from(OrderSub)
        .where(
            OrderSub.order_main_no == order_main_no,
            OrderSub.status == ORDER_REFUNDED,
        )
    )
    return int(total or 0), int(refunded or 0)


async def sub_refunded_amounts(session: AsyncSession, order_main_no: str) -> int:
    """母单下所有子单的已退金额合计。"""
    return int(
        await session.scalar(
            select(func.coalesce(func.sum(OrderSub.refunded_amount), 0)).where(
                OrderSub.order_main_no == order_main_no
            )
        )
        or 0
    )


async def get_main_refunded_amount(session: AsyncSession, order_main_no: str) -> int:
    return int(
        await session.scalar(
            select(OrderMain.refunded_amount).where(OrderMain.order_main_no == order_main_no)
        )
        or 0
    )


async def get_paid_amount(session: AsyncSession, order_main_no: str) -> int:
    return int(
        await session.scalar(
            select(OrderMain.paid_amount).where(OrderMain.order_main_no == order_main_no)
        )
        or 0
    )


async def insert_discount_snapshots(
    session: AsyncSession, rows: list[OrderDiscountSnapshot]
) -> None:
    session.add_all(rows)
    await session.flush()


async def list_discount_snapshots(
    session: AsyncSession, order_main_no: str
) -> list[OrderDiscountSnapshot]:
    return list(
        await session.scalars(
            select(OrderDiscountSnapshot)
            .where(OrderDiscountSnapshot.order_main_no == order_main_no)
            .order_by(OrderDiscountSnapshot.id)
        )
    )


# ============================================================
# 发货
# ============================================================
async def delivery_express_exists(
    session: AsyncSession, express_company: str, express_no: str
) -> bool:
    """这个「快递公司 + 单号」是否已经被用过。

    ``uk_delivery_express`` 是全局唯一约束（运单号本来就不该重复）。
    ★ 这里**预检**而不是等约束报错：唯一冲突会中断整个 PG 事务，冒出去就是 500 ——
      而单号填重是商家改一下就能解决的问题，应该回一句能看懂的话
      （本仓库既有约定，见 product.repository.category_name_exists）。
    """
    found = await session.scalar(
        select(DeliveryOrder.id)
        .where(
            DeliveryOrder.express_company == express_company,
            DeliveryOrder.express_no == express_no,
        )
        .limit(1)
    )
    return found is not None


async def insert_delivery(session: AsyncSession, delivery: DeliveryOrder) -> DeliveryOrder:
    session.add(delivery)
    await session.flush()
    return delivery


async def insert_delivery_items(session: AsyncSession, rows: list[DeliveryItem]) -> None:
    session.add_all(rows)
    await session.flush()


async def list_deliveries(session: AsyncSession, order_sub_no: str) -> list[DeliveryOrder]:
    return list(
        await session.scalars(
            select(DeliveryOrder)
            .where(DeliveryOrder.order_sub_no == order_sub_no)
            .order_by(DeliveryOrder.id)
        )
    )


async def list_deliveries_of_main(
    session: AsyncSession, order_main_no: str
) -> list[DeliveryOrder]:
    return list(
        await session.scalars(
            select(DeliveryOrder)
            .where(DeliveryOrder.order_main_no == order_main_no)
            .order_by(DeliveryOrder.id)
        )
    )


async def list_deliveries_of_subs(
    session: AsyncSession, sub_nos: Sequence[str]
) -> list[DeliveryOrder]:
    """批量取多个子单的发货单 —— 商家列表页用，避免 N+1。"""
    if not sub_nos:
        return []
    return list(
        await session.scalars(
            select(DeliveryOrder)
            .where(DeliveryOrder.order_sub_no.in_(sub_nos))
            .order_by(DeliveryOrder.id)
        )
    )


async def get_mains_by_nos(
    session: AsyncSession, main_nos: Sequence[str]
) -> dict[str, OrderMain]:
    """批量取母单，返回 ``{母单号: 母单}``。商家列表要拿收货信息，得回查母单。"""
    if not main_nos:
        return {}
    rows = await session.scalars(
        select(OrderMain).where(OrderMain.order_main_no.in_(main_nos))
    )
    return {m.order_main_no: m for m in rows}


# ============================================================
# 状态流水
# ============================================================
async def insert_state_flow(session: AsyncSession, flow: OrderStateFlow) -> None:
    session.add(flow)
    await session.flush()


async def list_state_flows(session: AsyncSession, order_no: str) -> list[OrderStateFlow]:
    return list(
        await session.scalars(
            select(OrderStateFlow)
            .where(OrderStateFlow.order_no == order_no)
            .order_by(OrderStateFlow.id)
        )
    )
