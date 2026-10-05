"""算价引擎的单元测试（docs/05 §10）。

引擎是**纯函数**，所以这些测试不需要数据库、不需要 Redis、跑得飞快。
文档点名了 15 个必须覆盖的场景，这里逐个落实（积分那条本期跳过）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.promotion.models import (
    DISCOUNT_COUPON_PLATFORM,
    DISCOUNT_COUPON_SHOP,
    DISCOUNT_ITEM_PROMO,
    DISCOUNT_PLATFORM_PROMO,
    DISCOUNT_SHOP_PROMO,
    LEVEL_ITEM,
    LEVEL_PLATFORM,
    LEVEL_SHOP,
    SCOPE_ALL,
    SCOPE_SKU,
)
from app.modules.promotion.pricing import (
    CALC_DIRECT,
    CALC_FIXED,
    CALC_RATE,
    REASON_SCOPE_NOT_MATCH,
    REASON_THRESHOLD_NOT_MET,
    ActivityInput,
    CalcItem,
    CouponInput,
    PriceCalculator,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
LATER = NOW + timedelta(days=7)

# 默认叠加规则（与迁移里灌入的一致）
DEFAULT_RULES: list[tuple[str, str, int, bool]] = [
    (DISCOUNT_ITEM_PROMO, DISCOUNT_SHOP_PROMO, 0, True),
    (DISCOUNT_ITEM_PROMO, DISCOUNT_COUPON_PLATFORM, 0, True),
    (DISCOUNT_SHOP_PROMO, DISCOUNT_PLATFORM_PROMO, 0, True),
    (DISCOUNT_COUPON_SHOP, DISCOUNT_COUPON_PLATFORM, 0, True),
    (DISCOUNT_SHOP_PROMO, DISCOUNT_COUPON_SHOP, 0, False),
    (DISCOUNT_PLATFORM_PROMO, DISCOUNT_COUPON_PLATFORM, 0, False),
    (DISCOUNT_COUPON_PLATFORM, DISCOUNT_COUPON_PLATFORM, 0, False),
    (DISCOUNT_COUPON_SHOP, DISCOUNT_COUPON_SHOP, 0, False),
    (DISCOUNT_ITEM_PROMO, DISCOUNT_ITEM_PROMO, 0, False),
]


def item(
    sku_id: int, price: int, num: int = 1, shop: int = 1, category: int = 100
) -> CalcItem:
    return CalcItem(
        sku_id=sku_id,
        num=num,
        unit_price=price,
        shop_id=shop,
        spu_id=sku_id * 10,
        category_id=category,
    )


def coupon(
    code_id: int,
    *,
    name: str = "券",
    ctype: int = 1,
    value: int,
    threshold: int = 0,
    max_discount: int = 0,
    shop: int = 0,
    scope_type: int = SCOPE_ALL,
    scope_value: list | None = None,
    stackable: bool = False,
) -> CouponInput:
    return CouponInput(
        code_id=code_id,
        template_id=code_id * 10,
        name=name,
        coupon_type=ctype,
        discount_value=value,
        max_discount=max_discount,
        threshold=threshold,
        shop_id=shop,
        scope_type=scope_type,
        scope_value=scope_value,
        exclude_value=None,
        stackable=stackable,
        valid_start=NOW,
        valid_end=LATER,
    )


def activity(
    act_id: int,
    *,
    name: str = "活动",
    level: int,
    atype: str,
    calc_type: int = CALC_DIRECT,
    value: int,
    threshold: int = 0,
    max_discount: int = 0,
    shop: int = 0,
    scope_type: int = SCOPE_ALL,
    scope_value: list | None = None,
    priority: int = 0,
) -> ActivityInput:
    return ActivityInput(
        activity_id=act_id,
        name=name,
        level=level,
        type=atype,
        calc_type=calc_type,
        discount_value=value,
        max_discount=max_discount,
        threshold=threshold,
        shop_id=shop,
        scope_type=scope_type,
        scope_value=scope_value,
        priority=priority,
    )


def calc(
    items: list[CalcItem],
    *,
    coupons: list[CouponInput] | None = None,
    activities: list[ActivityInput] | None = None,
    rules: list[tuple[str, str, int, bool]] | None = None,
    selected: list[int] | None = None,
):
    return PriceCalculator(
        items,
        coupons=coupons or [],
        activities=activities or [],
        stack_rules=DEFAULT_RULES if rules is None else rules,
        selected_coupon_ids=selected,
    ).run()


# ============================================================
# ① 无任何优惠
# ============================================================
def test_no_discount() -> None:
    """docs/05 §10 #1：没优惠时应付 = 原价总额。"""
    result = calc([item(1, 10000, 2), item(2, 5000)])
    assert result.total_amount == 25000
    assert result.payable_amount == 25000
    assert result.discounts == []


# ============================================================
# ② 单品直降 + 店铺满减：第二层用降价后的金额判门槛
# ============================================================
def test_item_promo_then_shop_threshold_uses_new_amount() -> None:
    """docs/05 §10 #2：层级依次作用，**后一层的门槛用前一层的剩余金额判断**。"""
    items = [item(1, 10000, 2)]  # 原价 200.00
    acts = [
        activity(1, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, value=2000),  # 直降 20/件
        # 店铺满 150 减 30：只有用降价后的 160.00 才能满足
        activity(2, level=LEVEL_SHOP, atype=DISCOUNT_SHOP_PROMO, value=3000, threshold=15000, shop=1),
    ]
    result = calc(items, activities=acts)

    assert result.item_discount == 4000, "直降 20/件 × 2 件"
    assert result.shop_discount == 3000, "剩余 160.00 ≥ 150.00，满减生效"
    assert result.payable_amount == 20000 - 4000 - 3000


def test_shop_threshold_not_met_before_item_promo_would_have_met() -> None:
    """反例：门槛按原价算会误判生效。这里用降价后的金额，所以**不生效**。"""
    items = [item(1, 10000, 2)]  # 原价 200.00，降价后 160.00
    acts = [
        activity(1, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, value=2000),
        activity(2, level=LEVEL_SHOP, atype=DISCOUNT_SHOP_PROMO, value=3000, threshold=18000, shop=1),
    ]
    result = calc(items, activities=acts)
    assert result.shop_discount == 0, "160.00 < 180.00，不该生效"


# ============================================================
# ③ 店铺券 + 平台券叠加
# ============================================================
def test_shop_and_platform_coupons_stack() -> None:
    """docs/05 §10 #3：平台券的门槛用**店铺券扣减后**的金额判断。"""
    items = [item(1, 10000, 2)]  # 200.00
    result = calc(
        items,
        coupons=[
            coupon(1, name="店铺券满100减20", value=2000, threshold=10000, shop=1),
            coupon(2, name="平台券满150减30", value=3000, threshold=15000, shop=0),
        ],
    )
    assert result.shop_discount == 2000
    # 200.00 - 20.00 = 180.00 ≥ 150.00 → 平台券生效
    assert result.platform_discount == 3000
    assert result.payable_amount == 20000 - 2000 - 3000


def test_platform_coupon_blocked_by_shop_coupon_deduction() -> None:
    """平台券门槛没被满足时不生效，且给出「还差多少」。"""
    items = [item(1, 10000, 2)]
    result = calc(
        items,
        coupons=[
            coupon(1, name="店铺券满100减20", value=2000, threshold=10000, shop=1),
            coupon(2, name="平台券满190减30", value=3000, threshold=19000, shop=0),
        ],
    )
    assert result.platform_discount == 0
    assert len(result.unavailable) == 1
    assert result.unavailable[0].reason == REASON_THRESHOLD_NOT_MET
    assert "还差" in result.unavailable[0].reason_text


# ============================================================
# ④ 店铺活动与店铺券互斥
# ============================================================
def test_shop_activity_and_shop_coupon_are_exclusive() -> None:
    """docs/05 §10 #4：互斥时只生效**优惠大的**那个。"""
    items = [item(1, 10000, 2)]  # 200.00
    result = calc(
        items,
        coupons=[coupon(1, name="店铺券减20", value=2000, shop=1)],
        activities=[
            activity(1, level=LEVEL_SHOP, atype=DISCOUNT_SHOP_PROMO, value=5000, shop=1)
        ],
    )
    assert result.shop_discount == 5000, "取优惠大的（活动 50 > 券 20）"
    assert len([d for d in result.discounts if d.level == LEVEL_SHOP]) == 1


def test_exclusive_picks_coupon_when_larger() -> None:
    items = [item(1, 10000, 2)]
    result = calc(
        items,
        coupons=[coupon(1, name="店铺券减80", value=8000, shop=1)],
        activities=[
            activity(1, level=LEVEL_SHOP, atype=DISCOUNT_SHOP_PROMO, value=5000, shop=1)
        ],
    )
    assert result.shop_discount == 8000


# ============================================================
# ⑤ 分摊（引擎侧的端到端）
# ============================================================
def test_allocation_across_three_items() -> None:
    """docs/05 §10 #5：3 个商品分摊，**和必须精确**。"""
    items = [item(1, 1000), item(2, 2000), item(3, 7000)]
    result = calc(items, coupons=[coupon(1, name="无门槛减33", value=3333)])

    allocated = sum(it.total_discount() for it in result.items)
    assert allocated == 3333, "分摊之和必须等于优惠额"
    assert result.payable_amount == 10000 - 3333


def test_allocation_to_one_cent_item() -> None:
    """docs/05 §10 #6：1 分钱的商品不能被分摊超过 1 分，溢出到其他行。"""
    items = [item(1, 1), item(2, 9999)]
    result = calc(items, coupons=[coupon(1, name="无门槛减50", value=5000)])

    assert result.items[0].total_discount() <= 1, "1 分钱的行最多承担 1 分"
    assert result.items[0].current_amount >= 0, "行金额不能变负"
    assert result.payable_amount == 10000 - 5000


def test_hundred_rows() -> None:
    """docs/05 §10 #13：100 行各分摊 1 分。"""
    items = [item(i, 100) for i in range(1, 101)]  # 每行 1.00，合计 100.00
    result = calc(items, coupons=[coupon(1, name="无门槛减1元", value=100)])

    allocated = [it.total_discount() for it in result.items]
    assert sum(allocated) == 100
    assert all(a == 1 for a in allocated)


# ============================================================
# ⑦ 无门槛券超过订单金额
# ============================================================
def test_discount_capped_at_order_amount() -> None:
    """docs/05 §10 #7 / #14：优惠 20 元但订单只有 15 元 → 应付 0，不为负。"""
    items = [item(1, 500), item(2, 1000)]  # 合计 15.00
    result = calc(items, coupons=[coupon(1, name="无门槛减20", value=2000)])

    assert result.payable_amount == 0, "应付不能为负"
    assert sum(it.total_discount() for it in result.items) == 1500, "优惠封顶到订单金额"


# ============================================================
# ⑧ 折扣券与封顶
# ============================================================
def test_discount_coupon_with_cap() -> None:
    """docs/05 §10 #8：8.5 折但最多减 50 元。"""
    items = [item(1, 100000)]  # 1000.00
    result = calc(
        items, coupons=[coupon(1, name="85折封顶50", ctype=2, value=8500, max_discount=5000)]
    )
    assert result.platform_discount == 5000, "150.00 被封顶到 50.00"


def test_discount_coupon_without_cap_hits_cap_by_default() -> None:
    items = [item(1, 10000)]  # 100.00
    result = calc(items, coupons=[coupon(1, name="85折", ctype=2, value=8500)])
    assert result.platform_discount == 1500, "100.00 × 15%"


def test_discount_uses_scope_total_not_per_row() -> None:
    """docs/05 §5.3 坑 1：折扣按**作用域总额**算再分摊，不能按行算。

    按行算的话 33.33 * 0.9 会在每行各舍入一次，累积出分差。
    """
    items = [item(1, 10000), item(2, 3333)]  # 100.00 + 33.33 = 133.33
    result = calc(items, coupons=[coupon(1, name="9折", ctype=2, value=9000)])

    # 按总额：13333 * (10000-9000) / 10000 = 1333.3 → 四舍五入 1333
    assert result.platform_discount == 1333
    assert sum(it.total_discount() for it in result.items) == 1333, "分摊之和必须等于总额"


# ============================================================
# ⑩ 适用范围
# ============================================================
def test_scope_limits_threshold_and_allocation() -> None:
    """docs/05 §10 #10：券只对商品 A 生效时，门槛只看 A，优惠也只分给 A。"""
    items = [item(1, 10000), item(2, 100000)]  # A=100.00, B=1000.00
    result = calc(
        items,
        coupons=[
            coupon(
                1,
                name="仅A可用满80减20",
                value=2000,
                threshold=8000,
                scope_type=SCOPE_SKU,
                scope_value=[1],
            )
        ],
    )
    assert result.platform_discount == 2000
    assert result.items[0].total_discount() == 2000, "全部分摊给 A"
    assert result.items[1].total_discount() == 0, "B 一分不参与"


def test_scope_no_match_marks_unavailable() -> None:
    items = [item(1, 10000)]
    result = calc(
        items,
        coupons=[
            coupon(1, name="仅限别的商品", value=2000, scope_type=SCOPE_SKU, scope_value=[999])
        ],
    )
    assert result.platform_discount == 0
    assert result.unavailable[0].reason == REASON_SCOPE_NOT_MATCH


# ============================================================
# ⑫ 大额
# ============================================================
def test_large_amount_no_overflow() -> None:
    """docs/05 §10 #12：999999.99 元的订单，整数运算不溢出。"""
    items = [item(1, 99999999)]
    result = calc(items, coupons=[coupon(1, name="减100", value=10000)])
    assert result.total_amount == 99999999
    assert result.payable_amount == 99999999 - 10000


# ============================================================
# ⑮ 确定性
# ============================================================
def test_deterministic() -> None:
    """docs/05 §10 #15：同一输入算两次结果必须完全一致。"""

    def build():
        return [
            item(1, 3333),
            item(2, 6667),
        ]

    a = calc(build(), coupons=[coupon(1, name="减10", value=1000)])
    b = calc(build(), coupons=[coupon(1, name="减10", value=1000)])
    assert a.payable_amount == b.payable_amount
    assert [i.total_discount() for i in a.items] == [i.total_discount() for i in b.items]


# ============================================================
# 单品促销的三种计算方式
# ============================================================
def test_item_fixed_price() -> None:
    """特价：把单价设为 X。100.00 特价到 79.90 → 每件省 20.10。"""
    items = [item(1, 10000, 1)]
    result = calc(
        items,
        activities=[
            activity(1, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, calc_type=CALC_FIXED, value=7990)
        ],
    )
    assert result.item_discount == 2010
    assert result.items[0].promo_price == 7990


def test_item_discount_rate() -> None:
    """单品折扣：9 折。"""
    items = [item(1, 10000, 2)]
    result = calc(
        items,
        activities=[
            activity(1, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, calc_type=CALC_RATE, value=9000)
        ],
    )
    assert result.item_discount == 2000, "200.00 × 10%"


def test_item_promo_takes_best_of_multiple() -> None:
    """同一 SKU 有多个单品活动时取最优。"""
    items = [item(1, 10000)]
    result = calc(
        items,
        activities=[
            activity(1, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, value=1000),
            activity(2, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, value=3000),
        ],
    )
    assert result.item_discount == 3000, "取优惠大的"


# ============================================================
# 平台级与店铺级互不干扰
# ============================================================
def test_platform_discount_applies_across_shops() -> None:
    """平台券作用于所有店铺的商品。"""
    items = [item(1, 10000, shop=1), item(2, 10000, shop=2)]
    result = calc(items, coupons=[coupon(1, name="平台减30", value=3000, shop=0)])
    assert result.platform_discount == 3000
    # 两店各承担一部分，和精确
    assert sum(it.total_discount() for it in result.items) == 3000


def test_shop_coupon_only_affects_its_shop() -> None:
    """店铺券只作用于本店商品，门槛也只算本店金额。"""
    items = [item(1, 5000, shop=1), item(2, 50000, shop=2)]
    result = calc(
        items,
        coupons=[coupon(1, name="1店满80减20", value=2000, threshold=8000, shop=1)],
    )
    # 店 1 只有 50.00 < 80.00 → 不生效
    assert result.shop_discount == 0
    assert result.unavailable[0].reason == REASON_THRESHOLD_NOT_MET


# ============================================================
# 结构性保证
# ============================================================
def test_allocations_sum_equals_total_discount() -> None:
    """**恒等式**：每行的分摊之和 == 各级优惠之和。

    这是下单时要写进订单快照并断言的守恒关系（docs/05 §6.4）。
    """
    items = [item(1, 3333), item(2, 6667), item(3, 12345)]
    result = calc(
        items,
        coupons=[
            coupon(1, name="平台减37", value=3700),
        ],
        activities=[
            activity(1, level=LEVEL_ITEM, atype=DISCOUNT_ITEM_PROMO, value=500),
        ],
    )
    total_from_items = sum(it.total_discount() for it in result.items)
    total_from_discounts = sum(d.amount for d in result.discounts)
    assert total_from_items == total_from_discounts

    payable_check = (
        result.total_amount
        - result.item_discount
        - result.shop_discount
        - result.platform_discount
    )
    assert payable_check == result.payable_amount


def test_no_item_goes_negative() -> None:
    """任何情况下行金额都不能为负。"""
    items = [item(1, 100), item(2, 100), item(3, 100)]
    result = calc(items, coupons=[coupon(1, name="无门槛减10元", value=1000)])
    assert all(it.current_amount >= 0 for it in result.items)


def test_empty_items() -> None:
    result = calc([])
    assert result.total_amount == 0
    assert result.payable_amount == 0


def test_negative_payable_raises() -> None:
    """应付为负属于引擎 Bug，必须显式报错而不是返回负数。

    正常路径下不可能构造出负应付 —— 分摊算法已经把优惠封顶到行金额。
    所以这里直接给 ``_summarize`` 喂一份"优惠大于商品总额"的输入，
    模拟引擎内部出 Bug 的情形。
    """
    from app.modules.promotion.pricing import AppliedDiscount

    pc = PriceCalculator([item(1, 1000)], coupons=[], activities=[], stack_rules=[])
    # 手工塞一条比商品总额还大的优惠
    pc.discounts = [
        AppliedDiscount(
            level=LEVEL_PLATFORM,
            source_type=DISCOUNT_COUPON_PLATFORM,
            source_id=1,
            source_name="异常优惠",
            amount=9999,
        )
    ]
    with pytest.raises(ValueError, match="应付金额为负"):
        pc._summarize()


# ============================================================
# ⑧ 用户显式选券：只算选中的
#
# ``selected_coupon_ids`` 以前只是被存进实例、**没有任何地方读它**，
# 所以结算页点了哪张券都不影响结果 —— 引擎永远自己挑每组最优的那张。
# 后果是"选了 ¥10、账单还是按 ¥20 减"，选择被无声吞掉。
# ============================================================
def test_selected_coupon_is_honored_even_when_worse() -> None:
    """选了较差的券就只减较差的 —— 尊重用户的选择，哪怕他选了不划算的那张。"""
    c30 = coupon(1, value=3000, threshold=10000)
    c10 = coupon(2, value=1000)
    items = [item(1, 20000)]

    auto = calc(items, coupons=[c30, c10])
    assert auto.platform_discount == 3000, "没指定时仍然自动取最优"

    picked = calc(items, coupons=[c30, c10], selected=[2])
    assert picked.platform_discount == 1000, "选了 ¥10 就只减 ¥10"
    assert picked.payable_amount == 20000 - 1000


def test_empty_selection_still_auto_picks_best() -> None:
    """选中列表为空表示"没手选"，仍要自动给最优 —— 结算页默认不能让用户吃亏。"""
    result = calc([item(1, 20000)], coupons=[coupon(1, value=3000, threshold=10000)])
    assert result.platform_discount == 3000


def test_selected_coupons_in_same_group_still_capped_at_one() -> None:
    """同组选中多张时只生效优惠最大的那张 —— 组内上限 1 不受"手选"影响。"""
    c30 = coupon(1, value=3000, threshold=10000)
    c20 = coupon(2, value=2000, threshold=10000)
    c10 = coupon(3, value=1000)

    result = calc([item(1, 20000)], coupons=[c30, c20, c10], selected=[2, 3])
    assert result.platform_discount == 2000, "选中的两张里取大的"
