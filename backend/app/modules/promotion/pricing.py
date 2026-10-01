"""算价引擎（docs/05 §3、§5、§7）。

**四层模型，按固定顺序叠加**：

    Level 0  单品促销     直降 / 折扣 / 特价，作用在单个 SKU 上
    Level 1  店铺级       店铺活动 + 店铺券，作用在「参与店铺优惠的金额」上
    Level 2  平台级       平台活动 + 平台券，作用在「参与平台优惠的金额」上
    Level 3  积分         本期跳过（account 还没有积分账户）

层级设计的三个理由（docs/05 §3.1）：**确定性**（同一购物车永远同一结果）、
**可解释**（前端能逐条展示优惠来源）、**可扩展**（新增优惠只需确定它属于哪层）。

**本模块是纯函数**：``PriceCalculator.run()`` 里没有任何 ``await``。
数据全部由调用方预加载好传进来。这是硬性约束（docs/05 §11）——
算价一旦夹杂 IO，P99 就不可控，也没法用固定输入做确定性单测。

**金额全部用整数分运算**，禁用浮点与内置 ``round()``（银行家舍入）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.modules.promotion.allocation import allocate_with_cap, apply_rate
from app.modules.promotion.models import (
    CALC_DIRECT,
    CALC_FIXED,
    CALC_RATE,
    DISCOUNT_COUPON_PLATFORM,
    DISCOUNT_COUPON_SHOP,
    DISCOUNT_ITEM_PROMO,
    DISCOUNT_PLATFORM_PROMO,
    DISCOUNT_SHOP_PROMO,
    DISCOUNT_TYPE_TEXT,
    LEVEL_ITEM,
    LEVEL_PLATFORM,
    LEVEL_SHOP,
    SCOPE_ALL,
    SCOPE_CATEGORY,
    SCOPE_SHOP,
    SCOPE_SKU,
)

# 不可用券的原因（docs/05 §7.3）
REASON_NOT_STARTED = "NOT_STARTED"
REASON_EXPIRED = "EXPIRED"
REASON_THRESHOLD_NOT_MET = "THRESHOLD_NOT_MET"
REASON_SCOPE_NOT_MATCH = "SCOPE_NOT_MATCH"
REASON_SHOP_NOT_MATCH = "SHOP_NOT_MATCH"
REASON_STACK_CONFLICT = "STACK_CONFLICT"
REASON_LOCKED_BY_ORDER = "LOCKED_BY_ORDER"

REASON_TEXT: dict[str, str] = {
    REASON_NOT_STARTED: "未到使用时间",
    REASON_EXPIRED: "已过期",
    REASON_THRESHOLD_NOT_MET: "还差 ¥{gap} 可用",
    REASON_SCOPE_NOT_MATCH: "仅限指定商品使用",
    REASON_SHOP_NOT_MATCH: "仅限 {shop} 店铺的商品使用",
    REASON_STACK_CONFLICT: "与已选优惠互斥",
    REASON_LOCKED_BY_ORDER: "正在被其他订单占用",
}


def fen_to_yuan(fen: int) -> str:
    """分 → 元字符串。**只用于文案**，不参与计算。"""
    return f"{fen // 100}.{fen % 100:02d}"


# ============================================================
# 输入 / 输出
# ============================================================
@dataclass(slots=True)
class CalcItem:
    """参与算价的一行商品。``unit_price`` 是**原价**。"""

    sku_id: int
    num: int
    unit_price: int
    shop_id: int
    spu_id: int
    category_id: int
    title: str = ""
    spec_text: str = ""
    cover_image: str = ""
    # 重量（克）。引擎自己用不到，但运费模块要按它分组计费，
    # 由调用方一起带进来，省得下游再查一次商品
    weight_g: int = 0

    # ---- 计算过程中会被改写 ----
    promo_price: int = 0  # Level 0 之后的价格
    promo_amount: int = 0  # Level 0 省下的钱
    current_amount: int = 0  # 当前剩余金额（每层递减）
    # 每条优惠分摊到这一行的金额，最终写进订单快照
    allocations: list[tuple[str, int, int]] = field(default_factory=list)  # (类型, 来源id, 金额)

    def init_amounts(self) -> None:
        self.promo_price = self.unit_price
        self.promo_amount = 0
        self.current_amount = self.unit_price * self.num

    def total_discount(self) -> int:
        return sum(a[2] for a in self.allocations)


@dataclass(slots=True)
class CouponInput:
    """一张可用的券（已通过所有权与状态校验）。"""

    code_id: int
    template_id: int
    name: str
    coupon_type: int  # 1满减 2折扣 3无门槛
    discount_value: int
    max_discount: int
    threshold: int
    shop_id: int  # 0 = 平台券
    scope_type: int
    scope_value: list | None
    exclude_value: list | None
    stackable: bool
    valid_start: datetime
    valid_end: datetime
    # 计算后回填
    amount: int = 0


@dataclass(slots=True)
class ActivityInput:
    """一个生效中的促销活动。"""

    activity_id: int
    name: str
    level: int
    type: str  # DISCOUNT_* 常量
    calc_type: int
    discount_value: int
    max_discount: int
    threshold: int
    shop_id: int
    scope_type: int
    scope_value: list | None
    priority: int


@dataclass(slots=True)
class AppliedDiscount:
    """一条最终生效的优惠。"""

    level: int
    source_type: str
    source_id: int
    source_name: str
    amount: int


@dataclass(slots=True)
class UnavailableCoupon:
    code_id: int
    name: str
    reason: str
    reason_text: str


@dataclass(slots=True)
class CalcResult:
    items: list[CalcItem]
    discounts: list[AppliedDiscount]
    unavailable: list[UnavailableCoupon]
    total_amount: int
    item_discount: int
    shop_discount: int
    platform_discount: int
    payable_amount: int


# ============================================================
# 作用域与门槛
# ============================================================
def _in_scope(
    item: CalcItem, scope_type: int, scope_value: list | None, exclude_value: list | None
) -> bool:
    """判断某行商品是否在优惠的作用域内。

    第一期直接解析 JSONB；docs/04 §7.1 提到的大集合优化（写一份 Redis Set、
    用 SISMEMBER 判断）留到作用域商品数上千时再做。
    """
    if scope_type == SCOPE_ALL:
        included = True
    elif scope_type == SCOPE_SKU:
        included = scope_value is not None and item.sku_id in scope_value
    elif scope_type == SCOPE_CATEGORY:
        included = scope_value is not None and item.category_id in scope_value
    elif scope_type == SCOPE_SHOP:
        included = scope_value is not None and item.shop_id in scope_value
    else:
        included = False

    if not included:
        return False
    # 排除范围优先级高于包含范围
    return not (exclude_value is not None and item.sku_id in exclude_value)


def _eligible_amount(
    items: list[CalcItem],
    scope_type: int,
    scope_value: list | None,
    exclude_value: list | None = None,
) -> tuple[int, list[CalcItem]]:
    """计算「参与优惠金额」与参与的行。

    ★ 这是整个引擎的核心概念（docs/05 §5.1）：优惠**不是作用在订单总额上**，
    而是作用在"它能作用的那部分金额"上。9 折券限定只对 A 商品生效时，
    门槛和折扣都只算 A 的金额。
    """
    selected = [
        it
        for it in items
        if _in_scope(it, scope_type, scope_value, exclude_value) and it.current_amount > 0
    ]
    return sum(it.current_amount for it in selected), selected


def _compute_discount(
    calc_type: int,
    discount_value: int,
    max_discount: int,
    base: int,
) -> int:
    """按计算方式算出优惠额。全部整数运算，四舍五入。

    只处理**订单级**的两种方式：直降与折扣。特价（``CALC_FIXED``）只对单品有意义
    ——它改变的是单价，不是从总额里减一笔，所以由 :func:`_per_unit_discount` 处理。
    """
    if base <= 0:
        return 0
    if calc_type == CALC_RATE:
        # 折扣必须按**作用域总额**算，再分摊到行（docs/05 §5.3 坑 1）。
        # 按行算的话 33.33 * 0.9 的舍入会在多行累积，和按总额算对不上。
        return apply_rate(base, discount_value, cap=max_discount)
    # 直降
    discount = discount_value
    if max_discount > 0:
        discount = min(discount, max_discount)
    return min(discount, base)


def _per_unit_discount(act: ActivityInput, unit_price: int) -> int:
    """单品活动的**每件**优惠额。

    特价（``CALC_FIXED``）的语义是"把单价设为 ``discount_value``"，
    所以优惠额是 ``原价 - 定价``，而不是直接减 ``discount_value``。
    """
    if act.calc_type == CALC_FIXED:
        return max(0, unit_price - act.discount_value)
    return _compute_discount(act.calc_type, act.discount_value, act.max_discount, unit_price)


# ============================================================
# 冲突组与叠加
# ============================================================
def _conflict_group(source_type: str, shop_id: int) -> str:
    """一个优惠属于哪个冲突组（docs/05 §4.2）。

    同组内只能选 ``max_select`` 个。所有组目前都是 1 —— 即"同层同源只能一个"，
    跨组是否可叠由 ``promo_stack_rule`` 决定。
    """
    if source_type == DISCOUNT_COUPON_PLATFORM:
        return "PLATFORM_COUPON"
    if source_type == DISCOUNT_COUPON_SHOP:
        return f"SHOP_COUPON:{shop_id}"
    if source_type == DISCOUNT_SHOP_PROMO:
        return f"SHOP_PROMO:{shop_id}"
    if source_type == DISCOUNT_PLATFORM_PROMO:
        return "PLATFORM_PROMO"
    if source_type == DISCOUNT_ITEM_PROMO:
        return "ITEM_PROMO"
    return source_type


def _stackable(
    type_a: str, type_b: str, shop_id: int, rules: dict[tuple[str, str, int], bool]
) -> bool:
    """两条优惠能否叠加。

    **默认互斥**（docs/05 §4.1）：规则表里没有这一对时返回 False。
    这个默认值是安全侧的——新增优惠类型忘了配规则时，结果是"不能叠加"，
    而不是"能叠加导致资损"。

    店铺级规则优先于全局规则。
    """
    if type_a == type_b:
        # 同类型：看该类型自己的规则（如"平台券之间互斥"）
        key = (type_a, type_b, shop_id) if shop_id else (type_a, type_b, 0)
        return rules.get(key, rules.get((type_a, type_b, 0), False))

    # 无向：两条规则任一方向声明可叠即可
    for a, b in ((type_a, type_b), (type_b, type_a)):
        if shop_id and (a, b, shop_id) in rules:
            return rules[(a, b, shop_id)]
        if (a, b, 0) in rules:
            return rules[(a, b, 0)]
    return False


def _pick_by_conflict(
    candidates: list[tuple[str, int, int, str, int]],  # (类型, 来源id, 金额, 名称, priority)
    rules: dict[tuple[str, str, int], bool],
    shop_id: int,
) -> list[tuple[str, int, int, str, int]]:
    """从候选里挑出最终生效的一组。

    规则（docs/05 §3.2）：
    1. 按冲突组分组，每组默认只留**优惠最大**的那个
       （平局按 priority 降序，再按来源 id 升序 —— 保证确定性）
    2. 跨组检查叠加矩阵，冲突的丢掉优惠小的那个
    """
    # ① 组内取最优
    best_in_group: dict[str, tuple[str, int, int, str, int]] = {}
    for cand in candidates:
        if cand[2] <= 0:
            continue  # 没算出优惠的候选不参与
        group = _conflict_group(cand[0], shop_id)
        current = best_in_group.get(group)
        # 优惠大的优先；相同则 priority 大的优先；再相同则 id 小的优先（确定性）
        if current is None or (cand[2], cand[4], -cand[1]) > (current[2], current[4], -current[1]):
            best_in_group[group] = cand

    picked = list(best_in_group.values())

    # ② 跨组叠加冲突：按优惠降序逐个加入，与已选的冲突就丢弃
    picked.sort(key=lambda c: (-c[2], -c[4], c[1]))
    final: list[tuple[str, int, int, str, int]] = []
    for cand in picked:
        if all(_stackable(cand[0], chosen[0], shop_id, rules) for chosen in final):
            final.append(cand)
    return final


# ============================================================
# 引擎
# ============================================================
class PriceCalculator:
    """四层算价。**纯函数**——不查库、不碰 Redis、没有任何 await。"""

    def __init__(
        self,
        items: list[CalcItem],
        *,
        coupons: list[CouponInput],
        activities: list[ActivityInput],
        stack_rules: list[tuple[str, str, int, bool]],
        selected_coupon_ids: list[int] | None = None,
    ) -> None:
        self.items = items
        self.coupons = coupons
        self.activities = activities
        # (type_a, type_b, shop_id) -> stackable
        self.rules: dict[tuple[str, str, int], bool] = {
            (a, b, sid): ok for a, b, sid, ok in stack_rules
        }
        self.selected_coupon_ids = set(selected_coupon_ids or [])
        self.unavailable: list[UnavailableCoupon] = []
        self.discounts: list[AppliedDiscount] = []

    # ---------- Level 0：单品促销 ----------
    def _apply_item_level(self) -> None:
        """同一 SKU 的多个单品活动**取最优**（docs/05 §2）。"""
        by_sku: dict[int, list[ActivityInput]] = {}
        for act in self.activities:
            if act.level != LEVEL_ITEM:
                continue
            for item in self.items:
                if _in_scope(item, act.scope_type, act.scope_value, None):
                    by_sku.setdefault(item.sku_id, []).append(act)

        for item in self.items:
            item.init_amounts()
            acts = by_sku.get(item.sku_id)
            if not acts:
                continue
            best = max(
                acts,
                key=lambda a: (
                    _per_unit_discount(a, item.unit_price),
                    a.priority,
                    -a.activity_id,
                ),
            )
            per_unit = min(_per_unit_discount(best, item.unit_price), item.unit_price)
            if per_unit <= 0:
                continue
            item.promo_price = item.unit_price - per_unit
            item.promo_amount = per_unit * item.num
            item.current_amount = item.promo_price * item.num
            item.allocations.append((DISCOUNT_ITEM_PROMO, best.activity_id, item.promo_amount))
            self.discounts.append(
                AppliedDiscount(
                    level=LEVEL_ITEM,
                    source_type=DISCOUNT_ITEM_PROMO,
                    source_id=best.activity_id,
                    source_name=best.name,
                    amount=item.promo_amount,
                )
            )

    # ---------- Level 1 / 2：订单级 ----------
    def _apply_order_level(self, level: int, source_types: tuple[str, ...], items: list[CalcItem]) -> None:
        """某一层（店铺级或平台级）的优惠计算。

        步骤严格按 docs/05 §7.2：候选 → 作用域过滤 → 门槛过滤 → 冲突组选最优 →
        算优惠额 → 分摊到行 → 扣减 current_amount。
        """
        if not items:
            return

        candidates: list[tuple[str, int, int, str, int]] = []
        # 记住候选的元信息，用于后续算优惠与分摊
        meta: dict[tuple[str, int], tuple[int, int, int, list, list | None]] = {}

        for act in self.activities:
            if act.level != level or act.type not in source_types:
                continue
            eligible, _rows = _eligible_amount(items, act.scope_type, act.scope_value)
            if eligible <= 0:
                continue
            if act.threshold > 0 and eligible < act.threshold:
                continue
            amount = _compute_discount(act.calc_type, act.discount_value, act.max_discount, eligible)
            if amount <= 0:
                continue
            candidates.append((act.type, act.activity_id, amount, act.name, act.priority))
            meta[(act.type, act.activity_id)] = (
                act.scope_type, act.calc_type, act.discount_value, act.scope_value or [], None
            )

        for coupon in self.coupons:
            # ★ 券属于哪一层由**它自己的 shop_id** 决定，不是由当前循环的层决定：
            #   shop_id = 0 → 平台券，只在平台层参与
            #   shop_id > 0 → 店铺券，只在店铺层参与，且只作用于本店商品
            #   漏掉这个判断会让平台券在店铺层被重复计算一次（优惠翻倍）。
            coupon_level = LEVEL_PLATFORM if coupon.shop_id == 0 else LEVEL_SHOP
            if coupon_level != level:
                continue
            expected = (
                DISCOUNT_COUPON_SHOP if coupon_level == LEVEL_SHOP else DISCOUNT_COUPON_PLATFORM
            )
            if expected not in source_types:
                continue

            shop_items = (
                [it for it in items if it.shop_id == coupon.shop_id]
                if coupon.shop_id > 0
                else items
            )
            eligible, _rows = _eligible_amount(
                shop_items, coupon.scope_type, coupon.scope_value, coupon.exclude_value
            )
            if eligible <= 0:
                self._mark_unavailable(coupon, REASON_SCOPE_NOT_MATCH)
                continue
            if coupon.threshold > 0 and eligible < coupon.threshold:
                self._mark_unavailable(
                    coupon,
                    REASON_THRESHOLD_NOT_MET,
                    gap=coupon.threshold - eligible,
                )
                continue

            # 券的类型：满减/无门槛 => 直减；折扣券 => 按率
            calc_type = CALC_RATE if coupon.coupon_type == 2 else CALC_DIRECT
            amount = _compute_discount(calc_type, coupon.discount_value, coupon.max_discount, eligible)
            if amount <= 0:
                continue
            coupon.amount = amount
            candidates.append((expected, coupon.code_id, amount, coupon.name, 0))
            meta[(expected, coupon.code_id)] = (
                coupon.scope_type, calc_type, coupon.discount_value, coupon.scope_value or [],
                coupon.exclude_value,
            )

        if not candidates:
            return

        shop_scope = items[0].shop_id if items else 0
        picked = _pick_by_conflict(candidates, self.rules, shop_scope)

        for source_type, source_id, amount, name, _priority in picked:
            scope_type, _ct, _dv, scope_value, exclude_value = meta[(source_type, source_id)]
            eligible, rows = _eligible_amount(items, scope_type, scope_value, exclude_value)
            if eligible <= 0:
                continue
            # ★ 用带上限的分摊：某行金额不足以承担它那份时截断，溢出到其他行，
            #   保证「Σ 分摊 == 优惠额」且「每行不为负」（docs/05 §6.3）
            caps = [it.current_amount for it in rows]
            parts = allocate_with_cap(amount, [it.current_amount for it in rows], caps)
            actual = sum(parts)

            for it, part in zip(rows, parts, strict=True):
                if part <= 0:
                    continue
                it.current_amount -= part
                it.allocations.append((source_type, source_id, part))

            if actual <= 0:
                continue
            self.discounts.append(
                AppliedDiscount(
                    level=level,
                    source_type=source_type,
                    source_id=source_id,
                    source_name=name,
                    amount=actual,
                )
            )

    def _mark_unavailable(self, coupon: CouponInput, reason: str, **kwargs: object) -> None:
        text_value = REASON_TEXT[reason].format(**kwargs) if kwargs else REASON_TEXT[reason]
        self.unavailable.append(
            UnavailableCoupon(
                code_id=coupon.code_id, name=coupon.name, reason=reason, reason_text=text_value
            )
        )

    # ---------- 主流程 ----------
    def run(self) -> CalcResult:
        """按 0 → 1 → 2 层依次计算。**没有任何 await**。"""
        self._apply_item_level()

        # Level 1：按店铺分组，各店独立计算（店铺券只作用于本店商品）
        shops: dict[int, list[CalcItem]] = {}
        for item in self.items:
            shops.setdefault(item.shop_id, []).append(item)
        for shop_items in shops.values():
            self._apply_order_level(
                LEVEL_SHOP, (DISCOUNT_SHOP_PROMO, DISCOUNT_COUPON_SHOP), shop_items
            )

        # Level 2：平台级，作用在所有商品上
        self._apply_order_level(
            LEVEL_PLATFORM, (DISCOUNT_PLATFORM_PROMO, DISCOUNT_COUPON_PLATFORM), self.items
        )

        return self._summarize()

    def _summarize(self) -> CalcResult:
        total_amount = sum(it.unit_price * it.num for it in self.items)
        item_discount = sum(d.amount for d in self.discounts if d.level == LEVEL_ITEM)
        shop_discount = sum(d.amount for d in self.discounts if d.level == LEVEL_SHOP)
        platform_discount = sum(d.amount for d in self.discounts if d.level == LEVEL_PLATFORM)

        payable = total_amount - item_discount - shop_discount - platform_discount
        if payable < 0:
            # 优惠不可能超过商品总额：分摊算法已经封顶到行金额，
            # 走到这里说明有 Bug，宁可报错也不能给出负的应付
            raise ValueError(f"应付金额为负：{payable}")

        return CalcResult(
            items=self.items,
            discounts=self.discounts,
            unavailable=self.unavailable,
            total_amount=total_amount,
            item_discount=item_discount,
            shop_discount=shop_discount,
            platform_discount=platform_discount,
            payable_amount=payable,
        )


def discount_type_text(source_type: str) -> str:
    return DISCOUNT_TYPE_TEXT.get(source_type, source_type)
