"""算价的编排层：加载数据 → 交给纯函数引擎 → 组装响应。

**为什么要单独一层**：引擎（``pricing.py``）必须保持纯函数（无 IO），
而算价确实要查商品、库存、券、活动。这一层负责把 IO 和计算分开 ——
IO 全在这里，计算全在引擎里。

★ 所有 DB 查询**顺序执行**，不用 ``asyncio.gather``：同一个 ``AsyncSession``
不能被并发协程共享（docs/05 §7.2 的明确警告）。Redis 查询本来就是批量一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.modules.account import service as account_service
from app.modules.freight import service as freight_service
from app.modules.inventory import service as inventory_service
from app.modules.product import service as product_service
from app.modules.product.models import SPU_ON_SHELF
from app.modules.promotion import repository as repo
from app.modules.promotion.models import (
    CODE_UNUSED,
    COUPON_TYPE_FREIGHT,
    LEVEL_ITEM,
    LEVEL_PLATFORM,
    LEVEL_SHOP,
    SCOPE_CATEGORY,
)
from app.modules.promotion.pricing import (
    ActivityInput,
    CalcItem,
    CouponInput,
    PriceCalculator,
    discount_type_text,
    fen_to_yuan,
)
from app.modules.promotion.schemas import (
    CalcAllocationOut,
    CalcDiscountOut,
    CalcItemOut,
    CalcPriceOut,
    CalcPriceRequest,
    UnavailableCouponOut,
)

# 积分本期未实现。运费已经实现，所以不再标注它
NOTICE = ["积分抵扣尚未实现（账户体系还没有积分）"]

SKU_ENABLED = 1


async def calc_price(session: AsyncSession, user_id: int, req: CalcPriceRequest) -> CalcPriceOut:
    """算价主流程。"""
    # ---------- ① 加载商品 ----------
    sku_ids = [int(i.sku_id) for i in req.items]
    skus = {
        int(s.id): s
        for s in await product_service.batch_get_skus(session, sku_ids, only_on_shelf=False)
    }

    missing = [sid for sid in sku_ids if sid not in skus]
    if missing:
        raise BizError(ErrorCode.SKU_OFF_SHELF, "部分商品已下架或不存在，请重新选择")
    for s in skus.values():
        if s.spu_status != SPU_ON_SHELF or s.status != SKU_ENABLED:
            raise BizError(ErrorCode.SKU_OFF_SHELF, f"「{s.title}」已下架，请重新选择")

    # ---------- ② 加载库存（只读，不预占；预占在下单时做）----------
    stock = await inventory_service.batch_available(session, sku_ids)

    # ---------- ③ 组装计算行 ----------
    now = datetime.now(UTC)
    items: list[CalcItem] = []
    for line in req.items:
        sid = int(line.sku_id)
        sku = skus[sid]
        items.append(
            CalcItem(
                sku_id=sid,
                num=line.num,
                unit_price=sku.price,
                shop_id=int(sku.shop_id),
                spu_id=int(sku.spu_id),
                category_id=int(sku.category_id),
                title=sku.title,
                spec_text=sku.spec_text,
                cover_image=sku.cover_image,
                weight_g=sku.weight_g,
            )
        )

    # ---------- ④ 加载用户选中的券 ----------
    # ★ 运费券（type=5）单独分出来：它作用在**运费**上，不是商品金额，
    #   门槛也针对运费本身（docs/06 §7）。混进商品券里会让它错误地抵扣商品。
    loaded = await _load_coupons(session, user_id, req.coupon_code_ids, now)
    unavailable = loaded.unavailable

    # ---------- ⑤ 加载生效中的活动与叠加规则 ----------
    activities = await _load_activities(session, now)
    rules = [
        (r.type_a, r.type_b, int(r.shop_id), r.stackable)
        for r in await repo.list_stack_rules(session, now=now)
    ]

    # ---------- ⑥ 纯内存计算（商品侧的四级优惠）----------
    # ★ 类目范围先展开成"含全部子类目"，再进引擎
    await _expand_category_scopes(session, activities=activities, coupons=loaded.goods)

    result = PriceCalculator(
        items, coupons=loaded.goods, activities=activities, stack_rules=rules
    ).run()

    # 引擎里发现的不可用原因（作用域不匹配、门槛不够）合并进来
    unavailable.extend(
        UnavailableCouponOut(
            code_id=c.code_id, name=c.name, reason=c.reason, reason_text=c.reason_text
        )
        for c in result.unavailable
    )

    # ---------- ⑥.5 运费 ----------
    freight, freight_notices = await _calc_freight(session, user_id, req, sku_ids)

    # ---------- ⑥.6 运费券抵扣 ----------
    freight, freight_notices = _apply_freight_coupon(
        loaded.freight, freight, freight_notices, unavailable
    )

    # ---------- ⑦ 组装响应 ----------
    out_items: list[CalcItemOut] = []
    for it in result.items:
        discount = it.total_discount()
        out_items.append(
            CalcItemOut(
                sku_id=it.sku_id,
                spu_id=it.spu_id,
                shop_id=it.shop_id,
                title=it.title,
                spec_text=it.spec_text,
                cover_image=it.cover_image,
                num=it.num,
                unit_price=it.unit_price,
                promo_price=it.promo_price,
                weight_g=it.weight_g,
                amount=it.unit_price * it.num,
                discount_amount=discount,
                payable_amount=it.current_amount,
                allocations=[
                    CalcAllocationOut(
                        source_type=source_type,
                        source_name=discount_type_text(source_type),
                        amount=amount,
                    )
                    for source_type, _source_id, amount in it.allocations
                    if amount > 0
                ],
            )
        )

    # 库存不足只提示，不阻断算价 —— 用户可能只是想看看多少钱
    notices = list(freight_notices)
    for it in result.items:
        if stock.get(it.sku_id, 0) < it.num:
            notices.append(f"「{it.title}」库存不足，下单前请调整数量")

    return CalcPriceOut(
        items=out_items,
        discounts=[
            CalcDiscountOut(
                level=d.level,
                source_type=d.source_type,
                source_id=d.source_id,
                source_name=d.source_name,
                amount=d.amount,
            )
            for d in result.discounts
        ],
        total_amount=result.total_amount,
        item_discount=result.item_discount,
        shop_discount=result.shop_discount,
        platform_discount=result.platform_discount,
        point_deduction=0,
        freight=freight,
        # 应付 = 商品总额 - 各级商品优惠 + 运费（运费券已经在 freight 里扣掉了）
        payable_amount=result.payable_amount + freight,
        unavailable_coupons=unavailable,
        notices=notices,
    )


@dataclass(slots=True)
class LoadedCoupons:
    """校验后的券，**按作用对象分开**。

    运费券（``coupon_type = 5``）作用在运费上，门槛也针对运费本身，
    混进商品券里会让它错误地抵扣商品金额（docs/06 §7）。
    """

    goods: list[CouponInput]
    freight: list[CouponInput]
    unavailable: list[UnavailableCouponOut]


async def _calc_freight(
    session: AsyncSession,
    user_id: int,
    req: CalcPriceRequest,
    sku_ids: list[int],
) -> tuple[int, list[str]]:
    """算运费。返回 ``(运费, 提示)``。

    **没传地址时按 0 计并说明** —— 结算页要先能展示商品金额，用户选完地址再来算运费。
    这不是"没实现"，是"还不知道发到哪"。
    """
    if req.address_id is None:
        return 0, ["选择收货地址后才会计算运费"]

    address = await account_service.get_address_for_order(
        session, user_id, int(req.address_id)
    )
    warehouses = await inventory_service.batch_sku_warehouses(session, sku_ids)

    lines = [
        freight_service.FreightLine(
            sku_id=int(i.sku_id),
            num=i.num,
            weight_g=0,  # 由 service 按 SKU 兜底，这里只需要 sku 与数量
            amount=0,
            shop_id=0,
        )
        for i in req.items
    ]
    # 重量与金额从商品查，避免调用方重复传
    skus = {
        int(s.id): s
        for s in await product_service.batch_get_skus(session, sku_ids, only_on_shelf=False)
    }
    for ln in lines:
        sku = skus.get(ln.sku_id)
        if sku is not None:
            ln.weight_g = sku.weight_g
            ln.amount = sku.price * ln.num
            ln.shop_id = int(sku.shop_id)
            ln.title = sku.title

    result = await freight_service.estimate(
        session, lines=lines, region_code=address.region_code, warehouses=warehouses
    )
    return result.total, list(result.notices)


def _apply_freight_coupon(
    freight_coupons: list[CouponInput],
    freight: int,
    notices: list[str],
    unavailable: list[UnavailableCouponOut],
) -> tuple[int, list[str]]:
    """运费券抵扣（docs/06 §7）。返回 ``(抵扣后的运费, 提示)``。

    三个与普通券不同的约束：

    - **门槛针对运费本身**（"满 20 元运费减 10"），不是商品金额
    - **一单只能用一张**：多张勾选时取抵扣最多的那张，其余标为不可用
    - 不影响商品金额的优惠计算（所以它不在商品券列表里）

    ``min(面额, 运费)`` 保证运费不会被抵成负数。
    """
    if not freight_coupons:
        return freight, notices

    # ① 门槛校验针对运费本身
    eligible: list[tuple[CouponInput, int]] = []
    for c in freight_coupons:
        if c.threshold > 0 and freight < c.threshold:
            unavailable.append(
                UnavailableCouponOut(
                    code_id=c.code_id,
                    name=c.name,
                    reason="THRESHOLD_NOT_MET",
                    reason_text=f"还差 ¥{fen_to_yuan(c.threshold - freight)} 运费可用",
                )
            )
            continue
        eligible.append((c, min(c.discount_value, freight)))

    if not eligible:
        return freight, notices

    # ② 一张订单只能用一张：取抵扣最多的
    eligible.sort(key=lambda pair: (-pair[1], pair[0].code_id))
    best, deductible = eligible[0]

    # 落选的券也要说清原因，否则用户会以为它生效了
    for c, _amount in eligible[1:]:
        unavailable.append(
            UnavailableCouponOut(
                code_id=c.code_id,
                name=c.name,
                reason="STACK_CONFLICT",
                reason_text="一单只能用一张运费券",
            )
        )

    if deductible <= 0:
        return freight, notices

    notices.append(f"运费券「{best.name}」已抵扣 ¥{fen_to_yuan(deductible)}")
    return freight - deductible, notices


async def _load_coupons(
    session: AsyncSession, user_id: int, code_ids: list[int], now: datetime
) -> LoadedCoupons:
    if not code_ids:
        return LoadedCoupons(goods=[], freight=[], unavailable=[])

    codes = await repo.list_user_codes_by_ids(session, user_id, [int(c) for c in code_ids])
    found = {int(c.id) for c in codes}

    goods: list[CouponInput] = []
    freight_coupons: list[CouponInput] = []
    unavailable: list[UnavailableCouponOut] = []

    # 勾了但找不到的（不是自己的，或已删除）
    for cid in code_ids:
        if int(cid) not in found:
            unavailable.append(
                UnavailableCouponOut(
                    code_id=cid,
                    name="未知券",
                    reason="NOT_FOUND",
                    reason_text="这张券不属于你或已不存在",
                )
            )

    if not codes:
        return LoadedCoupons(goods=[], freight=[], unavailable=unavailable)

    tpls = {
        int(t.id): t
        for t in await repo.list_templates_by_ids(session, [c.coupon_template_id for c in codes])
    }

    for code in codes:
        cid = int(code.id)
        tpl = tpls.get(int(code.coupon_template_id))
        base: dict = {"code_id": cid, "name": tpl.name if tpl else "优惠券"}

        if code.status != CODE_UNUSED:
            unavailable.append(
                UnavailableCouponOut(
                    **base,
                    reason="LOCKED_BY_ORDER" if code.status == 2 else "EXPIRED",
                    reason_text="正在被其他订单占用" if code.status == 2 else "已使用或已过期",
                )
            )
            continue
        if code.valid_start > now:
            unavailable.append(
                UnavailableCouponOut(**base, reason="NOT_STARTED", reason_text="未到使用时间")
            )
            continue
        if code.valid_end < now:
            unavailable.append(
                UnavailableCouponOut(**base, reason="EXPIRED", reason_text="已过期")
            )
            continue
        if tpl is None:
            unavailable.append(
                UnavailableCouponOut(**base, reason="NOT_FOUND", reason_text="券模板已不存在")
            )
            continue

        loaded = CouponInput(
            code_id=cid,
            template_id=int(tpl.id),
            name=tpl.name,
            coupon_type=tpl.type,
            discount_value=tpl.discount_value,
            max_discount=tpl.max_discount,
            threshold=tpl.threshold,
            shop_id=int(tpl.shop_id),
            scope_type=tpl.scope_type,
            scope_value=tpl.scope_value,
            exclude_value=tpl.exclude_value,
            stackable=tpl.stackable,
            valid_start=code.valid_start,
            valid_end=code.valid_end,
        )
        if tpl.type == COUPON_TYPE_FREIGHT:
            freight_coupons.append(loaded)
        else:
            goods.append(loaded)

    return LoadedCoupons(goods=goods, freight=freight_coupons, unavailable=unavailable)


async def _load_activities(session: AsyncSession, now: datetime) -> list[ActivityInput]:
    rows = await repo.list_active_activities(
        session, now=now, levels=[LEVEL_ITEM, LEVEL_SHOP, LEVEL_PLATFORM]
    )
    return [
        ActivityInput(
            activity_id=int(a.id),
            name=a.name,
            level=a.level,
            type=a.type,
            calc_type=a.calc_type,
            discount_value=a.discount_value,
            max_discount=a.max_discount,
            threshold=a.threshold,
            shop_id=int(a.shop_id),
            scope_type=a.scope_type,
            scope_value=a.scope_value,
            priority=a.priority,
        )
        for a in rows
    ]


async def _expand_category_scopes(
    session: AsyncSession,
    *,
    activities: list[ActivityInput],
    coupons: list[CouponInput],
) -> None:
    """把「指定类目」的 ``scope_value`` 就地展开成**含全部子类目**。

    ★ 为什么必须有这一步：选「图书」就该覆盖小说 / 童书 / 教育考试。
      引擎只是 ``item.category_id in scope_value`` 的精确比对，不展开的话
      父类目下的活动**一件商品都匹配不到** —— 而它在列表上和正常活动长得
      一模一样，只有运营去下单才会发现。以后新增的子类目也自动覆盖。

    ★ 为什么在这里展开而不是在引擎里：展开要读类目树，而引擎（``pricing.py``）
      是纯函数、不许有 IO。在这里展开一次，后面三处用到作用域的地方
      （单品级取候选、订单级算参与金额、优惠分摊）自动一致。

    存进 DB 的仍然是运营勾的那几个 id，语义留在数据里；展开只影响这一次计算。

    ``exclude_value``（排除范围）没跟着展开 —— 目前**没有任何入口能设置它**，
    等真加了排除功能时再一起处理，免得现在写一段没人走的代码。
    """
    wanted: set[int] = set()
    for entry in (*activities, *coupons):
        if entry.scope_type == SCOPE_CATEGORY and entry.scope_value:
            wanted.update(int(v) for v in entry.scope_value)
    if not wanted:
        return

    subtrees = await product_service.category_subtree_ids(session, sorted(wanted))
    for entry in (*activities, *coupons):
        if entry.scope_type != SCOPE_CATEGORY or not entry.scope_value:
            continue
        merged: set[int] = set()
        for v in entry.scope_value:
            merged |= subtrees.get(int(v), {int(v)})
        entry.scope_value = sorted(merged)
