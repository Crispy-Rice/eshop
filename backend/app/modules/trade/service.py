"""trade 模块的领域逻辑。

两个核心：

1. **``transit()``** —— 所有子单状态变更的唯一入口（三重保护）
2. **``create_order()``** —— 下单：算价 → 拆单 → 预占库存 → 锁券 → 落库

模块边界（docs/07 §12）：trade 调别人的 service，别人通过 ``transit`` 改订单状态。
售后模块将来接上时，在它自己的事务里调 ``transit``，两边状态在**同一个事务**里变更，
不存在"一边改了另一边没改"的窗口。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import after_commit
from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.snowflake import next_id
from app.modules.account import service as account_service
from app.modules.core import outbox
from app.modules.inventory import service as inventory_service
from app.modules.inventory.service import StockItem
from app.modules.promotion import checkout as promotion_checkout
from app.modules.promotion import service as promotion_service
from app.modules.promotion.allocation import allocate
from app.modules.promotion.schemas import CalcPriceRequest
from app.modules.trade import repository as repo
from app.modules.trade.models import (
    AUTO_RECEIVE_DAYS,
    DELIVERY_ORDER_SENT,
    OPERATOR_MERCHANT,
    OPERATOR_SYSTEM,
    OPERATOR_USER,
    ORDER_CLOSED,
    ORDER_TYPE_SUB,
    ORDER_WAIT_DELIVER,
    ORDER_WAIT_PAY,
    PAY_PAID,
    PAY_UNPAID,
    STATUS_TEXT,
    DeliveryItem,
    DeliveryOrder,
    OrderDiscountSnapshot,
    OrderItem,
    OrderMain,
    OrderStateFlow,
    OrderSub,
)
from app.modules.trade.order_no import build_delivery_no, build_main_no, build_sub_no
from app.modules.trade.schemas import (
    MerchantOrderListOut,
    MerchantOrderOut,
    OrderListOut,
    OrderMainOut,
    to_merchant_order_out,
    to_order_list_item,
    to_order_main_out,
)
from app.modules.trade.state_machine import (
    OrderEvent,
    SubOrderStatus,
    aggregate_main_status,
    derive_pay_status,
    next_status,
)
from app.worker.enqueue import enqueue_at

logger = get_logger(__name__)

# 延迟关单任务的函数名。tasks.py 里的函数必须叫这个名字（ARQ 按名查找）。
# 放在这里而不是 tasks.py：service 要投递它，反过来 import 会成环。
JOB_CLOSE_ORDER = "close_order_if_unpaid"

# 母子单之间必须守恒的字段。下单前逐项校验，不相等直接拒绝
CONSERVED_FIELDS = (
    "total_amount",
    "item_discount",
    "shop_discount",
    "platform_discount",
    "point_deduction",
    "freight_amount",
    "payable_amount",
)


@dataclass(slots=True)
class TransitContext:
    """状态流转的上下文（谁操作的、备注、恢复到哪）。"""

    operator_type: int = OPERATOR_SYSTEM
    operator_id: str | None = None
    remark: str | None = None
    # REFUND_REJECT / USER_REVOKE 时需要：恢复到的原状态
    restore_to: SubOrderStatus | None = None
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class CreateOrderRequest:
    items: list[dict]  # [{skuId, num}]
    address_id: int
    coupon_code_ids: list[int] = field(default_factory=list)
    buyer_remark: str | None = None


@dataclass(slots=True)
class SubDraft:
    """拆单的中间结果。"""

    shop_id: int
    shop_name: str
    items: list  # promotion 的 CalcItem
    total_amount: int
    item_discount: int
    shop_discount: int
    platform_discount: int = 0
    coupon_amount: int = 0
    point_deduction: int = 0
    freight_amount: int = 0

    @property
    def payable_amount(self) -> int:
        return (
            self.total_amount
            - self.item_discount
            - self.shop_discount
            - self.platform_discount
            - self.point_deduction
            + self.freight_amount
        )


def _require_warehouse(warehouses: dict[int, int], sku_id: int) -> int:
    """取 SKU 的发货仓。没有就直接拒绝 —— 不知道该从哪发，预占与回补都无从谈起。"""
    wh_id = warehouses.get(sku_id)
    if wh_id is None:
        raise BizError(ErrorCode.SKU_NOT_SUPPORTED, "商品没有可用的发货仓，无法下单")
    return wh_id


# ============================================================
# 状态流转的唯一入口
# ============================================================
async def transit(
    session: AsyncSession,
    order_sub_no: str,
    event: OrderEvent,
    ctx: TransitContext | None = None,
) -> SubOrderStatus:
    """在**调用方的事务内**执行状态流转（docs/07 §4.4）。

    三重保护，任一层失效其他层仍能拦住：

    1. ``SELECT ... FOR UPDATE`` 行锁 —— 挡住并发
    2. 状态机查表 —— 业务校验，非法流转直接拒绝
    3. ``WHERE status = :from`` CAS —— double check

    然后写审计流水 + outbox 事件 + 聚合母单状态。
    """
    ctx = ctx or TransitContext()

    # ① 行锁读取
    sub = await repo.get_sub_for_update(session, order_sub_no)
    if sub is None:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "子单不存在")
    from_status = SubOrderStatus(sub.status)

    # ② 状态机校验
    to_status = next_status(from_status, event, restore_to=ctx.restore_to)

    # ③ CAS 更新
    if not await repo.cas_sub_status(
        session, order_sub_no, from_status=int(from_status), to_status=int(to_status)
    ):
        raise BizError(ErrorCode.ORDER_STATUS_INVALID, "订单状态已变化，请刷新后重试")

    # ④ 审计流水（不可变日志）
    await repo.insert_state_flow(
        session,
        OrderStateFlow(
            order_type=ORDER_TYPE_SUB,
            order_no=order_sub_no,
            from_status=int(from_status),
            to_status=int(to_status),
            event=str(event),
            operator_type=ctx.operator_type,
            operator_id=ctx.operator_id,
            remark=ctx.remark,
            extra=ctx.extra,
        ),
    )

    # ⑤ outbox：与状态变更同一事务提交，异步处理通知、积分、销量统计
    await outbox.add(
        session,
        topic=outbox.TOPIC_SUB_STATUS_CHANGED,
        biz_key=f"{order_sub_no}:{int(to_status)}",
        payload={
            "orderSubNo": order_sub_no,
            "orderMainNo": sub.order_main_no,
            "userId": sub.user_id,
            "from": int(from_status),
            "to": int(to_status),
            "event": str(event),
        },
    )

    # ⑥ 聚合母单状态
    await aggregate_main(session, sub.order_main_no)

    logger.info(
        "订单状态流转",
        extra={
            "orderSubNo": order_sub_no,
            "from": int(from_status),
            "to": int(to_status),
            "event": str(event),
        },
    )
    return to_status


async def aggregate_main(session: AsyncSession, order_main_no: str) -> None:
    """按子单聚合母单状态并回写。

    母单状态是**派生值**，没有自己的状态机 —— 所以这里不走 ``transit``。
    """
    counts = await repo.count_subs_by_status(session, order_main_no)
    if not counts:
        return
    statuses = {SubOrderStatus(status) for status in counts}
    main_status = aggregate_main_status(statuses)
    await repo.update_main_status(session, order_main_no, status=int(main_status))


async def refresh_main_pay_status(session: AsyncSession, order_main_no: str) -> None:
    """按已退金额重算母单的 ``pay_status``（0未付 1已付 2部分退 3全退）。

    ★ 支付状态与履约状态是**正交**的：子单可以一半已退款一半待收货，
    履约状态显示"待收货"，而支付状态显示"部分退款"。所以退款成功时要单独修它，
    不能指望 ``aggregate_main`` 顺手带上。

    由售后模块在退款成功的事务里调用。
    """
    paid = await repo.get_paid_amount(session, order_main_no)
    refunded = await repo.get_main_refunded_amount(session, order_main_no)
    sub_count, refunded_subs = await repo.count_refunded_subs(session, order_main_no)
    pay_status = derive_pay_status(
        paid, refunded, sub_count=sub_count, refunded_subs=refunded_subs
    )
    await repo.update_main_pay_status(session, order_main_no, pay_status=int(pay_status))


# ============================================================
# 给其它模块的只读查询
# ============================================================
async def spu_has_order_items(session: AsyncSession, spu_id: int) -> bool:
    """这个商品有没有任何订单项（不限订单状态）。

    ★ 供 product 判"规格还能不能改"。订单在 trade 域，所以这个查询由 trade 暴露 ——
      product 自己不能反过来 import trade（``trade`` → ``inventory``/``promotion``
      → ``product``，反向即环，见 docs/01 §2）。
    """
    return await repo.spu_has_order_items(session, spu_id)


# ============================================================
# 下单
# ============================================================
async def create_order(
    session: AsyncSession,
    *,
    user_id: int,
    req: CreateOrderRequest,
    request_id: str,
) -> OrderMain:
    """下单主流程（docs/07 §2）。

    ``request_id`` 是幂等键。**先查一次**：如果这个键已经下过单，
    直接把原单返回，而不是重复创建（唯一索引是第二道防线）。
    """
    existing = await repo.get_main_by_request(session, user_id, request_id)
    if existing is not None:
        return existing

    # ① 算价（含商品四级优惠、运费、运费券）
    calc = await promotion_checkout.calc_price(
        session,
        user_id,
        CalcPriceRequest(
            items=req.items,  # type: ignore[arg-type]
            coupon_code_ids=[str(c) for c in req.coupon_code_ids],
            address_id=str(req.address_id),
        ),
    )
    if calc.payable_amount <= 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "订单金额不能为 0")

    # ② 拆单
    subs = await _split(session, calc)
    await _assert_conservation(calc, subs)

    # ③ 收货地址快照
    address = await account_service.get_address_for_order(session, user_id, req.address_id)

    main_no = build_main_no(next_id())
    now = datetime.now(UTC)
    pay_deadline = now + timedelta(minutes=30)

    # ④ 预占库存（Redis 预扣 + DB 条件更新，biz_key 幂等）
    sku_ids = [int(it.sku_id) for it in calc.items]
    warehouses = await inventory_service.batch_sku_warehouses(session, sku_ids)
    stock_items = [
        StockItem(
            sku_id=int(it.sku_id),
            warehouse_id=_require_warehouse(warehouses, int(it.sku_id)),
            num=it.num,
        )
        for it in calc.items
    ]
    await inventory_service.lock(
        session, stock_items, f"LOCK:{main_no}", user_id=user_id, order_no=main_no
    )

    # ⑤ 锁券（未支付时占用，关单会解锁）
    for code_id in req.coupon_code_ids:
        await promotion_service.lock(
            session,
            code_id=int(code_id),
            user_id=user_id,
            order_main_no=main_no,
        )

    # ⑥ 写母子单
    main = OrderMain(
        id=next_id(),
        order_main_no=main_no,
        user_id=user_id,
        request_id=request_id,
        shop_count=len(subs),
        total_amount=calc.total_amount,
        item_discount=calc.item_discount,
        shop_discount=calc.shop_discount,
        platform_discount=calc.platform_discount,
        coupon_amount=sum(s.coupon_amount for s in subs),
        point_deduction=calc.point_deduction,
        point_used=0,
        freight_amount=calc.freight,
        payable_amount=calc.payable_amount,
        status=ORDER_WAIT_PAY,
        pay_status=PAY_UNPAID,
        receiver_name=address.receiver_name,
        receiver_phone=address.phone,
        receiver_province=address.province,
        receiver_city=address.city,
        receiver_district=address.district,
        receiver_detail=address.detail,
        region_code=address.region_code,
        # 运费明细快照：模板改了也能追溯当时的计算依据
        freight_detail=_freight_detail(calc),
        buyer_remark=req.buyer_remark,
        pay_deadline=pay_deadline,
    )
    await repo.insert_main(session, main)

    await _write_subs(session, main_no, user_id, subs)
    await _write_items(session, main_no, calc)
    await _write_discount_snapshots(session, main_no, calc)

    # ⑦ 到点关单的延迟任务。**注册成提交后回调**，不是现在投递 ——
    # 现在投递的话，事务若失败，队列里就多一个指向不存在订单的任务。
    # 即使这个投递整个丢了也不影响正确性：每 2 分钟一次的兜底扫描会补上。
    after_commit.defer(
        session,
        lambda: enqueue_at(JOB_CLOSE_ORDER, main_no, run_at=pay_deadline),
    )

    logger.info(
        "订单已创建",
        extra={"orderMainNo": main_no, "shopCount": len(subs), "payable": calc.payable_amount},
    )
    return main


async def _split(session: AsyncSession, calc) -> list[SubDraft]:
    """按店铺拆单 + 平台级优惠按金额占比分摊（docs/07 §2.4）。

    店铺券天然属于某个店铺，不用分摊；**平台券与平台活动必须分摊到各子单** ——
    因为退款要按子单退，不分摊的话用户退掉子单 A 时算不出该退多少钱。
    """
    from app.modules.account import service as account_service  # 局部避免循环

    by_shop: dict[int, list] = {}
    for item in calc.items:
        by_shop.setdefault(int(item.shop_id), []).append(item)

    shop_names = await account_service.list_shop_names(session, list(by_shop.keys()))

    subs = [
        SubDraft(
            shop_id=shop_id,
            shop_name=shop_names.get(shop_id, "未知店铺"),
            items=items,
            total_amount=sum(it.unit_price * it.num for it in items),
            item_discount=sum(
                a.amount
                for it in items
                for a in it.allocations
                if a.source_type.startswith("PROMO_ITEM")
            ),
            shop_discount=sum(
                a.amount
                for it in items
                for a in it.allocations
                if a.source_type in ("PROMO_ORDER_SHOP", "COUPON_SHOP")
            ),
            coupon_amount=sum(
                a.amount
                for it in items
                for a in it.allocations
                if a.source_type in ("COUPON_SHOP", "COUPON_PLATFORM")
            ),
        )
        for shop_id, items in by_shop.items()
    ]

    # 平台级优惠按「子单商品原价」占比分摊（最大余数法保证 Σ 恰好等于总额）
    if len(subs) == 1:
        # 单店铺不用分摊，全部归它 —— 分摊算法在只有一项时结果也一样，
        # 但直接赋值更清楚，也避免"分摊"这个词造成误解
        subs[0].platform_discount = calc.platform_discount
    else:
        base = [s.total_amount for s in subs]
        for sub, amount in zip(subs, allocate(calc.platform_discount, base), strict=True):
            sub.platform_discount = amount

    # 运费已按仓库算好并归属到店铺（freight 模块的 packages 带 warehouse 但不带 shop），
    # 这里按运费明细里的店铺归属取。第一期单仓单店，直接全部归第一个子单即可，
    # 多店铺时按商品金额占比分摊（与平台优惠同理）。
    if len(subs) == 1:
        subs[0].freight_amount = calc.freight
    elif calc.freight > 0:
        base = [s.total_amount for s in subs]
        for sub, amount in zip(subs, allocate(calc.freight, base), strict=True):
            sub.freight_amount = amount

    return subs


async def _assert_conservation(calc, subs: list[SubDraft]) -> None:
    """母子单守恒校验（docs/07 §2.3）。

    用**显式抛异常**而不是 ``assert`` —— 后者在 ``python -O`` 下会被整个移除，
    生产环境正好就是不加 ``-O`` 才安全，但这不该依赖启动参数。

    不平的订单**绝不能写进库**：一旦写了，用户付的钱和子系统里的金额对不上，
    退款时按子单退就会退错。
    """
    checks = [
        ("total_amount", calc.total_amount, sum(s.total_amount for s in subs)),
        ("item_discount", calc.item_discount, sum(s.item_discount for s in subs)),
        ("shop_discount", calc.shop_discount, sum(s.shop_discount for s in subs)),
        (
            "platform_discount",
            calc.platform_discount,
            sum(s.platform_discount for s in subs),
        ),
        ("freight_amount", calc.freight, sum(s.freight_amount for s in subs)),
        ("payable_amount", calc.payable_amount, sum(s.payable_amount for s in subs)),
    ]
    for name, main_value, sub_sum in checks:
        if main_value != sub_sum:
            raise BizError(
                ErrorCode.PRICE_CHANGED,
                f"订单金额校验失败（{name}：母单 {main_value} ≠ 子单合计 {sub_sum}）",
            )


async def _write_subs(
    session: AsyncSession, main_no: str, user_id: int, subs: list[SubDraft]
) -> None:
    auto_finish = datetime.now(UTC) + timedelta(days=AUTO_RECEIVE_DAYS)
    rows = [
        OrderSub(
            id=next_id(),
            order_sub_no=build_sub_no(main_no, index),
            order_main_no=main_no,
            user_id=user_id,
            shop_id=sub.shop_id,
            shop_name_snap=sub.shop_name,
            total_amount=sub.total_amount,
            item_discount=sub.item_discount,
            shop_discount=sub.shop_discount,
            platform_discount=sub.platform_discount,
            coupon_amount=sub.coupon_amount,
            point_deduction=sub.point_deduction,
            freight_amount=sub.freight_amount,
            payable_amount=sub.payable_amount,
            status=ORDER_WAIT_PAY,
            # 自动收货时间在**下单时**就算好，发货后由 cron 依据它扫描
            auto_finish_time=auto_finish,
        )
        for index, sub in enumerate(subs, start=1)
    ]
    await repo.insert_subs(session, rows)


async def _write_items(session: AsyncSession, main_no: str, calc) -> None:
    """写订单项。**快照字段写入后永不变更**（docs/07 §9）。"""
    items: list[OrderItem] = []
    # 按店铺顺序分配子单号，与 _write_subs 的序号保持一致
    shop_order = list(dict.fromkeys(int(it.shop_id) for it in calc.items))
    for item in calc.items:
        sub_index = shop_order.index(int(item.shop_id)) + 1
        discount = sum(a.amount for a in item.allocations)
        items.append(
            OrderItem(
                id=next_id(),
                order_main_no=main_no,
                order_sub_no=build_sub_no(main_no, sub_index),
                shop_id=int(item.shop_id),
                spu_id=int(item.spu_id),
                sku_id=int(item.sku_id),
                spu_title_snap=item.title,
                sku_spec_snap=item.spec_text,
                cover_image_snap=item.cover_image,
                unit_price_snap=item.unit_price,
                weight_g_snap=item.weight_g,
                num=item.num,
                item_amount=item.amount,
                discount_amount=discount,
                coupon_amount=sum(
                    a.amount for a in item.allocations if a.source_type.startswith("COUPON_")
                ),
                promo_amount=sum(
                    a.amount for a in item.allocations if a.source_type.startswith("PROMO_")
                ),
                point_amount=0,
                payable_amount=item.payable_amount,
            )
        )
    await repo.insert_items(session, items)


async def _write_discount_snapshots(session: AsyncSession, main_no: str, calc) -> None:
    """写优惠快照 —— 退款时要按**当时**的规则算，不能按现在的。"""
    level_of = {
        "PROMO_ITEM": 0,
        "PROMO_ORDER_SHOP": 1,
        "COUPON_SHOP": 1,
        "PROMO_ORDER_PLATFORM": 2,
        "COUPON_PLATFORM": 2,
    }
    rows = [
        OrderDiscountSnapshot(
            order_main_no=main_no,
            order_sub_no=None,
            level=level_of.get(d.source_type, 2),
            source_type=d.source_type,
            # 券是 couponCodeId、活动是 promoActivityId。有了它，退款/核销才能
            # 反查到是**哪一张**券参与了这笔订单（docs/05 §9）
            source_id=int(d.source_id),
            source_name=d.source_name,
            rule_snapshot={"amount": d.amount},
            discount_amount=d.amount,
        )
        for d in calc.discounts
    ]
    if rows:
        await repo.insert_discount_snapshots(session, rows)


def _freight_detail(calc) -> dict:
    """运费明细快照。docs/06 §12 第 6 点：模板改了，历史订单的运费依据仍要可追溯。"""
    return {
        "total": calc.freight,
        "notices": list(calc.notices),
    }


async def _stock_items_of_main(session: AsyncSession, order_main_no: str) -> list[StockItem]:
    """把母单的订单项转成库存模块要的行（SKU + 发货仓 + 数量）。

    关单回补、支付确认、发货扣减都要用它 —— 三处必须拿到**同一套**仓库归属，
    否则会出现"从 A 仓预占、往 B 仓回补"的错账。
    """
    items = await repo.list_items_of_main(session, order_main_no)
    return await _to_stock_items(session, items)


async def _stock_items_of_sub(session: AsyncSession, order_sub_no: str) -> list[StockItem]:
    """子单维度的库存行。发货是按子单发的，扣减也得按子单扣。"""
    items = await repo.list_items_of_subs(session, [order_sub_no])
    return await _to_stock_items(session, items)


async def _to_stock_items(session: AsyncSession, items: list[OrderItem]) -> list[StockItem]:
    if not items:
        return []
    warehouses = await inventory_service.batch_sku_warehouses(
        session, [int(it.sku_id) for it in items]
    )
    return [
        StockItem(
            sku_id=int(it.sku_id),
            warehouse_id=_require_warehouse(warehouses, int(it.sku_id)),
            num=it.num,
        )
        for it in items
    ]


# ============================================================
# 关单
# ============================================================
async def close_order(session: AsyncSession, order_main_no: str, *, event: OrderEvent) -> bool:
    """关闭订单。返回是否真的关闭了（False = 已支付或已关闭，不必处理）。

    **幂等**：CAS 关母单 ``rowcount = 0`` 就直接返回。超时任务和用户取消
    调的是同一个函数，重复执行无副作用（docs/07 §7.2）。

    库存回补走 outbox 而不是直接调用 —— **Redis 操作无法随 PG 事务回滚**，
    先回补再提交的话事务失败就会超卖（docs/07 §7.3）。
    """
    main = await repo.close_main_if_unpaid(session, order_main_no, reason=str(event))
    if main is None:
        return False

    subs = await repo.list_subs(session, order_main_no)
    ctx = TransitContext(operator_type=OPERATOR_SYSTEM, remark=f"关闭订单：{event}")
    for sub in subs:
        await transit(session, sub.order_sub_no, event, ctx)

    # 释放 DB 库存预占（同事务，biz_key 幂等）
    await inventory_service.release(
        session,
        await _stock_items_of_main(session, order_main_no),
        f"CANCEL:{order_main_no}",
        order_no=order_main_no,
    )

    # 支付单也要关掉 —— 否则用户手里的"待支付"入口还能点，
    # 点了会走到 mock-callback，那边发现订单已关闭会拒绝（钱收不进来但体验很差）
    from app.modules.payment import service as payment_service  # 局部避免循环依赖

    await payment_service.close_by_order(session, order_main_no)

    # 解锁在这张单上占用的券。**必须在同一个事务里** ——
    # 关单成功但券没解锁的话，用户白白损失一张券
    await promotion_service.release_by_order(session, order_main_no)

    # Redis 侧回补与通知走 outbox：事务提交 ⇔ 消息存在
    await outbox.add(
        session,
        topic=outbox.TOPIC_INVENTORY_REDIS_RELEASE,
        biz_key=f"CANCEL:{order_main_no}",
        payload={"orderMainNo": order_main_no},
    )
    await outbox.add(
        session,
        topic=outbox.TOPIC_ORDER_CLOSED,
        biz_key=f"CLOSED:{order_main_no}",
        payload={"userId": main.user_id, "orderMainNo": order_main_no},
    )
    return True


# ============================================================
# 发货 / 收货
# ============================================================
async def ship(
    session: AsyncSession,
    *,
    shop_id: int,
    order_sub_no: str,
    express_company: str,
    express_no: str,
) -> None:
    """商家发货。只能对**待发货**的子单操作（状态机会拒绝其他状态）。"""
    sub = await repo.get_sub(session, order_sub_no)
    if sub is None or int(sub.shop_id) != shop_id:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")

    # ★ 先预检单号是否已被用过，**再动状态机**。
    #   不预检的话，uk_delivery_express 的唯一冲突会中断整个 PG 事务、冒成
    #   500「系统繁忙」；而单号填重是商家自己能改的问题，必须给一句明确的话。
    if await repo.delivery_express_exists(session, express_company, express_no):
        raise BizError(ErrorCode.VALIDATION_ERROR, "该快递单号已被使用，请核对后重新填写")

    await transit(
        session,
        order_sub_no,
        OrderEvent.SHIP,
        TransitContext(operator_type=OPERATOR_MERCHANT, operator_id=str(shop_id)),
    )

    delivery = await repo.insert_delivery(
        session,
        DeliveryOrder(
            id=next_id(),
            delivery_no=build_delivery_no(next_id()),
            order_sub_no=order_sub_no,
            order_main_no=sub.order_main_no,
            shop_id=shop_id,
            warehouse_id=0,  # 第一期单仓，发货单不细分仓库
            express_company=express_company,
            express_no=express_no,
            status=DELIVERY_ORDER_SENT,
            deliver_time=datetime.now(UTC),
        ),
    )
    # 发货单明细：整单一次发完（第一期不支持部分发货，状态机里留了 PARTIAL_SHIP）
    items = await repo.list_items_of_subs(session, [order_sub_no])
    await repo.insert_delivery_items(
        session,
        [
            DeliveryItem(delivery_no=delivery.delivery_no, order_item_id=it.id, num=it.num)
            for it in items
        ],
    )
    await repo.update_sub_fields(
        session, order_sub_no, {"deliver_time": datetime.now(UTC), "delivery_count": 1}
    )

    # ★ 库存从「冻结」转「实扣」（frozen → total）。
    #   货已经出库，总量到这一步才真正变小 —— 这一步之前都还能通过退货挽回。
    await inventory_service.deliver(
        session,
        await _stock_items_of_sub(session, order_sub_no),
        f"DELIVER:{order_sub_no}",
        order_no=order_sub_no,
    )


async def receive(session: AsyncSession, *, user_id: int, order_sub_no: str) -> None:
    """买家确认收货。"""
    sub = await repo.get_sub(session, order_sub_no)
    if sub is None or int(sub.user_id) != user_id:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")

    await transit(
        session,
        order_sub_no,
        OrderEvent.CONFIRM_RECEIVE,
        TransitContext(operator_type=OPERATOR_USER, operator_id=str(user_id)),
    )
    await repo.update_sub_fields(
        session,
        order_sub_no,
        {"receive_time": datetime.now(UTC), "finish_time": datetime.now(UTC)},
    )


# ============================================================
# 支付成功（由 payment 模块调用）
# ============================================================
async def mark_paid(session: AsyncSession, order_main_no: str, *, paid_amount: int) -> bool:
    """支付成功后推进母单与所有子单。

    **由 payment 模块在自己的事务里调用**，两边状态同事务变更 ——
    不存在"支付单已成功但订单还是待付款"的窗口。

    幂等：条件是"母单还在待付款"，重放时 ``rowcount = 0`` 直接返回 False。
    """
    if not await repo.mark_main_paid(
        session,
        order_main_no,
        paid_amount=paid_amount,
        status=ORDER_WAIT_DELIVER,
        pay_status=PAY_PAID,
    ):
        return False

    subs = await repo.list_subs(session, order_main_no)
    ctx = TransitContext(operator_type=OPERATOR_SYSTEM, remark="支付成功")
    for sub in subs:
        await transit(session, sub.order_sub_no, OrderEvent.PAY_SUCCESS, ctx)

    # ★ 库存从「预占」转「冻结」（locked → frozen）。
    #   钱已经收了，这批货不再是"可能被释放的预占"，而是"确定要发的实扣"。
    #   少了这一步，已付款订单的货会一直躺在 locked 里，被当成未支付的预占看待。
    await inventory_service.confirm(
        session,
        await _stock_items_of_main(session, order_main_no),
        f"CONFIRM:{order_main_no}",
        order_no=order_main_no,
    )

    # 把这张单锁住的券核销掉。每张券抵扣了多少，从订单优惠快照里读 ——
    # 券的 source_id 就是 couponCodeId（docs/05 §9）
    snapshots = await repo.list_discount_snapshots(session, order_main_no)
    await promotion_service.settle_by_order(
        session,
        order_main_no,
        amount_by_code={
            int(s.source_id): int(s.discount_amount)
            for s in snapshots
            if s.source_type.startswith("COUPON_") and int(s.source_id) > 0
        },
    )

    return True


# ============================================================
# 用户取消
# ============================================================
async def cancel_order(session: AsyncSession, *, user_id: int, order_main_no: str) -> bool:
    """买家取消订单。只有**自己的、待付款的**订单能取消。

    重复取消不是错误 —— ``close_order`` 本身幂等，第二次返回 False。
    前端刷新重试、网络重发都不该报错。
    """
    main = await repo.get_main_for_user(session, order_main_no, user_id)
    if main is None:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")
    if int(main.status) == ORDER_CLOSED:
        return False
    if int(main.status) != ORDER_WAIT_PAY:
        raise BizError(
            ErrorCode.ORDER_STATUS_INVALID,
            f"订单当前是「{STATUS_TEXT.get(int(main.status), '未知')}」，不能取消",
        )
    return await close_order(session, order_main_no, event=OrderEvent.USER_CANCEL)


# ============================================================
# 批量扫描（cron 用）
# ============================================================
async def close_timeout_orders(session: AsyncSession, *, limit: int = 500) -> int:
    """关闭超时未支付的订单。返回真正关闭了几单。

    这是延迟任务的**兜底**（docs/07 §7.2）：延迟任务可能因为 worker 重启、
    投递失败而丢失，而订单超时不关会一直占着库存和券。两条路径调的是同一个
    ``close_order``，所以重复执行没有副作用。

    每单包一个 SAVEPOINT：某一单的数据异常（比如子单状态对不上）不该让
    整批扫描失败 —— 否则一个坏订单会卡住后面所有订单的关单。
    """
    nos = await repo.list_timeout_mains(session, limit=limit)
    closed = 0
    for no in nos:
        try:
            async with session.begin_nested():
                if await close_order(session, no, event=OrderEvent.TIMEOUT_CANCEL):
                    closed += 1
        except BizError as exc:
            logger.warning(
                "关单失败，已跳过该单",
                extra={"orderMainNo": no, "code": str(exc.code), "reason": exc.message},
            )
    if closed:
        logger.info("超时关单完成", extra={"scanned": len(nos), "closed": closed})
    return closed


async def auto_receive_expired(session: AsyncSession, *, limit: int = 500) -> int:
    """发货后超过 15 天自动确认收货。返回处理了几单。

    自动收货是为了让商家能拿到货款 —— 用户一直不点"确认收货"的话，
    钱会一直卡在平台。真实电商里这个期限通常是 7~15 天（docs/07 §7.4）。
    """
    sub_nos = await repo.list_auto_receive_subs(session, limit=limit)
    now = datetime.now(UTC)
    done = 0
    for sub_no in sub_nos:
        try:
            async with session.begin_nested():
                await transit(
                    session,
                    sub_no,
                    OrderEvent.AUTO_RECEIVE,
                    TransitContext(
                        operator_type=OPERATOR_SYSTEM, remark="发货后超期自动确认收货"
                    ),
                )
                await repo.update_sub_fields(
                    session, sub_no, {"receive_time": now, "finish_time": now}
                )
                done += 1
        except BizError as exc:
            # 用户刚好自己点了确认收货 —— 不是错误，跳过即可
            logger.info(
                "自动收货跳过（可能已被用户确认）",
                extra={"orderSubNo": sub_no, "reason": exc.message},
            )
    if done:
        logger.info("自动确认收货完成", extra={"scanned": len(sub_nos), "done": done})
    return done


# ============================================================
# 查询
# ============================================================
def _encode_cursor(create_time: datetime, row_id: int) -> str:
    raw = f"{create_time.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw_time, raw_id = base64.urlsafe_b64decode(padded).decode().split("|")
        return datetime.fromisoformat(raw_time), int(raw_id)
    except (ValueError, TypeError) as exc:
        raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新加载") from exc


async def get_order_detail(
    session: AsyncSession, order_main_no: str
) -> tuple[OrderMain, list[OrderSub], list[OrderItem], list[DeliveryOrder]] | None:
    main = await repo.get_main_by_no(session, order_main_no)
    if main is None:
        return None
    subs = await repo.list_subs(session, order_main_no)
    items = await repo.list_items_of_main(session, order_main_no)
    deliveries = await repo.list_deliveries_of_main(session, order_main_no)
    return main, subs, items, deliveries


async def get_my_order_detail(
    session: AsyncSession, *, user_id: int, order_main_no: str
) -> OrderMainOut:
    """我的订单详情。带归属校验 —— 拿别人的单号查不到（Luhn 只挡手输错误）。"""
    found = await get_order_detail(session, order_main_no)
    if found is None:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")
    main, subs, items, deliveries = found
    if int(main.user_id) != user_id:
        # 刻意返回"不存在"而不是"无权限"：不泄露"这个单号确实存在"
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")
    return to_order_main_out(main, subs, items, deliveries, now=datetime.now(UTC))


async def list_my_orders(
    session: AsyncSession,
    user_id: int,
    *,
    status: int | None = None,
    cursor: str | None = None,
    limit: int = 10,
) -> OrderListOut:
    """我的订单列表。**键集游标分页**（docs/07 §10）。

    多取一条判断 ``has_more`` —— 比再发一次 ``COUNT(*)`` 便宜得多。
    """
    rows = await repo.list_mains_for_user(
        session,
        user_id,
        status=status,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    # 批量取订单项，避免每行一次查询
    items = await repo.list_items_of_mains(session, [m.order_main_no for m in page])
    by_main: dict[str, list[OrderItem]] = {}
    for it in items:
        by_main.setdefault(it.order_main_no, []).append(it)

    now = datetime.now(UTC)
    return OrderListOut(
        items=[to_order_list_item(m, by_main.get(m.order_main_no, []), now=now) for m in page],
        next_cursor=(
            _encode_cursor(page[-1].create_time, page[-1].id) if has_more and page else None
        ),
        has_more=has_more,
    )


async def list_shop_orders(
    session: AsyncSession,
    shop_id: int,
    *,
    status: int | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> MerchantOrderListOut:
    """商家订单列表。走 ``idx_order_sub_shop``（docs/07 §10）。"""
    rows = await repo.list_subs_for_shop(
        session,
        shop_id,
        status=status,
        cursor=_decode_cursor(cursor) if cursor else None,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    page = rows[:limit]

    # 商家只看到自己那一单，但要发货就得有收货人信息 —— 那在母单上，批量回查
    mains = await repo.get_mains_by_nos(
        session, list({s.order_main_no for s in page})
    )
    sub_nos = [s.order_sub_no for s in page]
    items = await repo.list_items_of_subs(session, sub_nos)
    deliveries = await repo.list_deliveries_of_subs(session, sub_nos)

    by_sub: dict[str, list[OrderItem]] = {}
    for it in items:
        by_sub.setdefault(it.order_sub_no, []).append(it)
    deliv_by_sub: dict[str, list[DeliveryOrder]] = {}
    for d in deliveries:
        deliv_by_sub.setdefault(d.order_sub_no, []).append(d)

    return MerchantOrderListOut(
        items=[
            to_merchant_order_out(
                s,
                mains[s.order_main_no],
                by_sub.get(s.order_sub_no, []),
                deliv_by_sub.get(s.order_sub_no, []),
            )
            for s in page
            if s.order_main_no in mains
        ],
        next_cursor=(
            _encode_cursor(page[-1].create_time, page[-1].id) if has_more and page else None
        ),
        has_more=has_more,
    )


async def get_shop_order(
    session: AsyncSession, *, shop_id: int, order_sub_no: str
) -> MerchantOrderOut:
    """商家订单详情。查的不是自己的子单时返回"不存在"，不泄露他人订单。"""
    sub = await repo.get_sub(session, order_sub_no)
    if sub is None or int(sub.shop_id) != shop_id:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")
    mains = await repo.get_mains_by_nos(session, [sub.order_main_no])
    main = mains.get(sub.order_main_no)
    if main is None:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")
    return to_merchant_order_out(
        sub,
        main,
        await repo.list_items_of_subs(session, [order_sub_no]),
        await repo.list_deliveries(session, order_sub_no),
    )


# ============================================================
# 供 review 模块调用
# ============================================================
# 依赖方向是 review → trade（docs/01 §2 允许的方向）。trade 拥有 order_item /
# order_sub，所以「取订单项+子单」「读待评价候选」「写 is_reviewed」都由这里提供，
# review 不直接读 trade 的表。


async def get_item_with_sub(
    session: AsyncSession, order_item_id: int
) -> tuple[OrderItem, OrderSub] | None:
    """取订单项 + 它所属的子单。**评价资格判定用**（判定要同时看两边）。

    返回 ORM 对象供 review 读取；不要直接序列化给前端。
    """
    return await repo.get_item_with_sub(session, order_item_id)


async def list_reviewable_items(
    session: AsyncSession,
    user_id: int,
    *,
    within_days: int,
    cursor: int | None = None,
    limit: int = 20,
) -> list[OrderItem]:
    """待评价的候选订单项。**是否已评过由 review 自己判断**。"""
    return await repo.list_reviewable_items(
        session, user_id, within_days=within_days, cursor=cursor, limit=limit
    )


async def mark_sub_reviewed(
    session: AsyncSession, order_sub_no: str, *, reviewed: bool
) -> None:
    """写 ``order_sub.is_reviewed``。

    语义是"**该子单内每个未全退的订单项都已评价**"，而不是"评过其中一件" ——
    一个 3 件的子单只评了 1 件就把角标清掉，剩下 2 件用户再也找不到入口。
    """
    await repo.set_sub_reviewed(session, order_sub_no, reviewed=reviewed)


async def list_items_of_sub(session: AsyncSession, order_sub_no: str) -> list[OrderItem]:
    """取子单的订单项（评价模块判断"整单是否评完"用）。"""
    return await repo.list_items_of_subs(session, [order_sub_no])
