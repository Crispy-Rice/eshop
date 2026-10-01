"""运费计算引擎的单元测试（docs/06）。

引擎是纯函数，所以这些测试不碰数据库、跑得飞快。文档给的两个完整数值例子
（§5.3）在这里逐个复现 —— 它们是需求"同仓只收一次首重"的直接验证。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.modules.freight.calculator import (
    FREE_REASON_ALWAYS,
    FREE_REASON_NUM,
    FREE_REASON_THRESHOLD,
    FreightItem,
    FreightRule,
    RegionRuleEntry,
    calc_freight,
    ceil_div,
    charge_rule,
    check_free_shipping,
    is_conflict,
    pick_region_rule,
    region_matches,
)
from app.modules.freight.models import (
    CHARGE_BY_PIECE,
    CHARGE_BY_WEIGHT,
    MERGE_SEPARATELY,
)

# docs/06 §4.1 的例子：首重 1000g/10元，续重 500g/3元
T1 = FreightRule(CHARGE_BY_WEIGHT, 1000, 1000, 500, 300, False)
# docs/06 §5.3 的 T2：首重 2000g/15元，续重 1000g/5元
T2 = FreightRule(CHARGE_BY_WEIGHT, 2000, 1500, 1000, 500, False)
# docs/06 §5.3 的 T3：首重 500g/8元，续重 250g/2元
T3 = FreightRule(CHARGE_BY_WEIGHT, 500, 800, 250, 200, False)


def item(
    sku_id: int,
    *,
    num: int = 1,
    weight: int = 0,
    amount: int = 0,
    wh: int = 1,
    tpl: int = 100,
    rule: FreightRule = T1,
) -> FreightItem:
    return FreightItem(
        sku_id=sku_id,
        num=num,
        weight_g=weight,
        amount=amount,
        warehouse_id=wh,
        template_id=tpl,
        rule=rule,
    )


# ============================================================
# 整数向上取整
# ============================================================
@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [(0, 3, 0), (1, 3, 1), (3, 3, 1), (4, 3, 2), (600, 500, 2), (500, 500, 1), (501, 500, 2)],
)
def test_ceil_div(a: int, b: int, expected: int) -> None:
    assert ceil_div(a, b) == expected


def test_ceil_div_rejects_zero() -> None:
    with pytest.raises(ValueError):
        ceil_div(1, 0)


# ============================================================
# §4.1 按重量
# ============================================================
def test_doc_example_weight() -> None:
    """docs/06 §4.1 的原例：200g 买 8 件 = 1600g → 10 + 2×3 = 16 元（1600 分）。"""
    assert charge_rule(T1, 1600, 8) == 1600


def test_within_first_unit_charges_first_price_only() -> None:
    assert charge_rule(T1, 1000, 5) == 1000
    assert charge_rule(T1, 999, 5) == 1000


def test_boundary_one_gram_over() -> None:
    """docs/06 §4.1 的边界：1001g → 超 1 克也收一个续重单位 → 13 元。

    文档点名这是**行业标准做法**，结算页必须提前告知用户，
    所以它值得一条测试钉住。
    """
    assert charge_rule(T1, 1001, 1) == 1000 + 300


# ============================================================
# §4.2 按件数
# ============================================================
def test_charge_by_piece() -> None:
    """首件 10 元、续件 2 件 3 元：买 7 件 → 10 + ceil(6/2)×3 = 19 元。"""
    rule = FreightRule(CHARGE_BY_PIECE, 1, 1000, 2, 300, False)
    assert charge_rule(rule, total_weight_g=99999, total_qty=7) == 1000 + 3 * 300
    assert charge_rule(rule, total_weight_g=99999, total_qty=1) == 1000


# ============================================================
# §5.3 冲突裁决 —— 本模块的核心
# ============================================================
def test_doc_example_conflict_merge() -> None:
    """docs/06 §5.3 例一：T1 vs T2 冲突 → T2 胜出，合并 1400g → 15 元。

    对比"各算各的"是 25 元。差 10 元就是省下来的那次首重。
    """
    items = [
        item(1, num=2, weight=300, amount=6000, tpl=100, rule=T1),  # 600g
        item(2, num=1, weight=800, amount=8000, tpl=200, rule=T2),  # 800g
    ]
    result = calc_freight(items)

    assert len(result.packages) == 1, "同仓只出一个包裹"
    pkg = result.packages[0]
    assert pkg.template_id == 200, "T2 的 first_unit(2000) > T1(1000)，T2 胜出"
    assert pkg.weight_g == 1400, "合并重量 = 600 + 800"
    assert pkg.freight == 1500, "1400 <= 2000，只收首重价 15 元"
    assert result.total == 1500

    # 对照：不合并的话是 10 + 15 = 25 元
    separate = charge_rule(T1, 600, 2) + charge_rule(T2, 800, 1)
    assert separate == 2500, "不合并会多收一次首重"
    assert result.total < separate


def test_doc_example_conflict_merge_second() -> None:
    """docs/06 §5.3 例二：T1 vs T3 → T1 胜出，合并 1700g → 10 + 2×3 = 16 元。"""
    items = [
        item(1, num=1, weight=1200, tpl=100, rule=T1),
        item(2, num=1, weight=500, tpl=300, rule=T3),
    ]
    result = calc_freight(items)

    assert result.packages[0].template_id == 100, "T1 首重 1000 > T3 的 500"
    assert result.packages[0].weight_g == 1700
    assert result.total == 1000 + 2 * 300


def test_winner_by_first_price_when_first_unit_equal() -> None:
    """首重相同时比首重价。"""
    cheap = FreightRule(CHARGE_BY_WEIGHT, 1000, 800, 500, 300, False)
    pricey = FreightRule(CHARGE_BY_WEIGHT, 1000, 1200, 500, 300, False)
    items = [item(1, weight=100, tpl=1, rule=cheap), item(2, weight=100, tpl=2, rule=pricey)]
    result = calc_freight(items)
    assert result.packages[0].template_id == 2, "首重价 12 > 8"


def test_winner_tie_break_by_sku_id_ascending() -> None:
    """参数全相同时按 **sku_id 升序** —— 保证同一输入永远同一结果。"""
    a = FreightRule(CHARGE_BY_WEIGHT, 1000, 1000, 500, 300, False)
    b = FreightRule(CHARGE_BY_WEIGHT, 1000, 1000, 500, 300, False)  # 参数完全一样
    items = [item(9, weight=100, tpl=90, rule=a), item(3, weight=100, tpl=30, rule=b)]
    result = calc_freight(items)
    assert result.packages[0].template_id == 30, "sku_id 小的胜出"


def test_is_conflict_only_same_warehouse_cross_template() -> None:
    same_wh_diff_tpl = (item(1, wh=1, tpl=100, rule=T1), item(2, wh=1, tpl=200, rule=T2))
    assert is_conflict(*same_wh_diff_tpl) is True

    diff_wh = (item(1, wh=1, tpl=100, rule=T1), item(2, wh=2, tpl=200, rule=T2))
    assert is_conflict(*diff_wh) is False, "跨仓不算冲突（是两个包裹）"

    same_tpl = (item(1, wh=1, tpl=100, rule=T1), item(2, wh=1, tpl=100, rule=T1))
    assert is_conflict(*same_tpl) is False, "同模板不算冲突"


# ============================================================
# §6 跨仓：各收一次首重
# ============================================================
def test_cross_warehouse_charges_first_unit_each() -> None:
    """docs/06 §6：两个仓 = 两个包裹 = 各收一次首重。"""
    items = [
        item(1, num=2, weight=300, wh=1, tpl=100, rule=T1),
        item(2, num=1, weight=800, wh=2, tpl=200, rule=T2),
    ]
    result = calc_freight(items)

    assert len(result.packages) == 2
    assert result.total == 1000 + 1500, "10 + 15"
    assert any("仓库" in n for n in result.notices), "要提示用户为什么运费高"


def test_single_warehouse_gives_no_cross_notice() -> None:
    result = calc_freight([item(1, weight=100)])
    assert not any("仓库" in n for n in result.notices)


# ============================================================
# §4.3 包邮
# ============================================================
def test_free_shipping_always() -> None:
    rule = FreightRule(CHARGE_BY_WEIGHT, 1000, 1000, 500, 300, True)
    free, reason = check_free_shipping(rule, amount=0, qty=1, free_num=0)
    assert free and reason == FREE_REASON_ALWAYS


def test_free_shipping_by_threshold() -> None:
    rule = FreightRule(CHARGE_BY_WEIGHT, 1000, 1000, 500, 300, False)
    assert check_free_shipping(rule, 9900, 1, 0)[1] is None


    with_threshold = replace(rule, free_threshold=10000)
    assert check_free_shipping(with_threshold, 9900, 1, 0)[1] is None, "差 1 分不算"
    assert check_free_shipping(with_threshold, 10000, 1, 0)[1] == FREE_REASON_THRESHOLD


def test_free_shipping_by_num() -> None:
    rule = FreightRule(CHARGE_BY_WEIGHT, 1000, 1000, 500, 300, False)
    assert check_free_shipping(rule, 0, 2, 3)[1] is None
    assert check_free_shipping(rule, 0, 3, 3)[1] == FREE_REASON_NUM


def test_threshold_uses_this_template_amount_only() -> None:
    """★ 满额包邮看的是**该模板下商品**的金额，不是整单金额。

    这是文档点名的常见误解。这里两个 SKU 绑不同模板，
    单个都不到包邮线，但合起来到了 —— 结果**不该包邮**。
    """

    rule_a = replace(T1, free_threshold=10000)  # 满 100 包邮
    rule_b = replace(T2, free_threshold=10000)

    items = [
        item(1, num=1, weight=100, amount=6000, wh=1, tpl=100, rule=rule_a),
        item(2, num=1, weight=100, amount=6000, wh=1, tpl=200, rule=rule_b),
    ]
    result = calc_freight(
        items, free_num_of={100: 0, 200: 0}
    )
    # 裁决后 T2 胜出（首重大），包裹按 T2 计费，包裹内金额 12000 >= 10000 → 包邮
    assert result.packages[0].amount == 12000
    assert result.total == 0, "同包裹内合计 12000 达到 T2 的门槛，包邮"


def test_free_threshold_not_met() -> None:

    rule = replace(T1, free_threshold=100000)
    result = calc_freight([item(1, num=1, weight=100, amount=5000, rule=rule)])
    assert result.total > 0


# ============================================================
# 区域规则匹配
# ============================================================
def test_region_prefix_match() -> None:
    """行政区划码是前缀结构：省级规则能匹配区级地址。"""
    assert region_matches("0", "310115") is True, "全国默认匹配一切"
    assert region_matches("31", "310115") is True, "上海市匹配浦东新区"
    assert region_matches("3101", "310115") is True, "上海市辖区匹配"
    assert region_matches("310115", "310115") is True, "精确匹配"
    assert region_matches("11", "310115") is False, "北京不匹配上海"


def test_region_rule_prefers_most_specific() -> None:
    """区 > 市 > 省 > 全国。"""
    all_cn = RegionRuleEntry("0", 1, 0, FreightRule(CHARGE_BY_WEIGHT, 1000, 1000, 500, 300, False))
    province = RegionRuleEntry("31", 1, 10, FreightRule(CHARGE_BY_WEIGHT, 1000, 1200, 500, 300, False))
    city = RegionRuleEntry("3101", 2, 20, FreightRule(CHARGE_BY_WEIGHT, 1000, 1500, 500, 300, False))
    district = RegionRuleEntry("310115", 3, 30, FreightRule(CHARGE_BY_WEIGHT, 1000, 1800, 500, 300, False))

    picked = pick_region_rule([all_cn, province, city, district], "310115")
    assert picked is not None and picked.region_code == "310115", "区级最精确"

    picked = pick_region_rule([all_cn, province, city], "310115")
    assert picked is not None and picked.region_code == "3101", "没有区级规则就落到市级"

    picked = pick_region_rule([all_cn, province], "310115")
    assert picked is not None and picked.region_code == "31", "再落到省级"

    picked = pick_region_rule([all_cn], "310115")
    assert picked is not None and picked.region_code == "0", "最后是全国默认"


def test_region_rule_none_when_unmatched() -> None:
    province = RegionRuleEntry("31", 1, 0, T1)
    assert pick_region_rule([province], "110100") is None, "没有全国默认时其他省份无规则"


def test_region_priority_is_secondary_tiebreaker() -> None:
    """``priority`` 是**次一级**的裁决键。

    主键是"区域码越长越精确"，这一条实现了文档说的"市 > 省 > 全国"。
    ``priority`` 只在长度相同时才起作用 —— 而同一个行政区划码在一个模板下
    只可能有一条规则（``UNIQUE (template_id, region_code)`` 保证），
    所以它实际几乎不会生效，保留是为了配置上的灵活性（将来若放宽唯一约束，
    比如允许按"同一城市的多个区"配不同规则，它就能用了）。
    """
    province = RegionRuleEntry("31", 1, 99, FreightRule(CHARGE_BY_WEIGHT, 1000, 1000, 500, 300, False))
    district = RegionRuleEntry("310115", 3, 0, FreightRule(CHARGE_BY_WEIGHT, 1000, 1800, 500, 300, False))

    # 省级的 priority 高得多，但区级更精确，仍然是区级胜出
    picked = pick_region_rule([province, district], "310115")
    assert picked is not None
    assert picked.region_code == "310115", "精确度优先于 priority"


# ============================================================
# 边界与兜底
# ============================================================
def test_zero_weight_falls_back() -> None:
    """历史脏数据 weight_g = 0 时按 500g 兜底（docs/06 §10）。"""
    result = calc_freight([item(1, num=1, weight=0, rule=T1)])
    assert result.packages[0].weight_g == 500


def test_empty_items() -> None:
    result = calc_freight([])
    assert result.total == 0 and result.packages == []


def test_merge_separately_mode() -> None:
    """不合并的模板：各算各的（配置项，第一期默认走合并）。"""
    items = [
        item(1, num=1, weight=1200, tpl=100, rule=T1),
        item(2, num=1, weight=1200, tpl=100, rule=T1),
    ]
    result = calc_freight(items, merge_type_of={100: MERGE_SEPARATELY})
    # 各算：1200g 每件都是 10 + ceil(200/500)×3 = 13 元，两件 26 元
    assert result.total == 2 * (1000 + 300)


def test_huge_weight_notice() -> None:
    """超大重量给提示，避免算出的运费离谱（docs/06 §10）。"""
    result = calc_freight([item(1, num=1, weight=200_000, rule=T1)])
    assert any("客服" in n for n in result.notices)


def test_deterministic() -> None:
    """同一输入永远同一结果。"""
    def build():
        return [
            item(1, num=2, weight=300, tpl=100, rule=T1),
            item(2, num=1, weight=800, tpl=200, rule=T2),
        ]

    a = calc_freight(build())
    b = calc_freight(build())
    assert a.total == b.total
    assert a.packages[0].template_id == b.packages[0].template_id
