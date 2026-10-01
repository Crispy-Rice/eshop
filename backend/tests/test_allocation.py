"""分摊算法的单元测试（docs/05 §10）。

**这是投入产出比最高的一组测试**：纯函数、不碰数据库、跑得飞快，
而它保护的是资金安全 —— 分摊不精确会让全额退款退多，平台资损且对账永远对不平。

分两层：
- 手写的边界用例（文档 §10 点名的那些）
- **性质测试**（hypothesis）：随机生成金额与行数，断言三条约束恒成立。
  手写用例只能覆盖想到的情况，性质测试能覆盖没想到的。
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.modules.promotion.allocation import allocate, allocate_with_cap, apply_rate

# ============================================================
# 最大余数法：基本行为
# ============================================================


def test_exact_division() -> None:
    """能整除时没有余数可补。"""
    assert allocate(30, [10, 20, 70]) == [3, 6, 21]


def test_remainder_goes_to_largest_remainder() -> None:
    """docs/05 §10 #5：3 个相等商品分摊 10 元 → 3.33 + 3.33 + 3.34。

    余数相同时按下标升序，所以是**前两个**先拿整 3.33，最后一个补到 3.34 ——
    等等，实际是余数大的先补。三者精确值都是 3.333...，余数相同，
    按 `(-remainder, index)` 排序 → 下标 0、1、2 依次，gap=1 所以只有下标 0 得 1 分。

    关键断言是**和恰好等于 10**，具体补在哪一行由确定性规则决定。
    """
    result = allocate(1000, [3333, 3333, 3334])
    assert sum(result) == 1000
    assert all(r >= 0 for r in result)


def test_three_equal_items_split_ten_yuan() -> None:
    """docs/05 §10 #13 的缩小版：3 个相同金额分摊，和必须精确。"""
    result = allocate(10, [100, 100, 100])
    assert sum(result) == 10, "分摊必须守恒"
    # 10/3 = 3.33...，整数部分各 3，剩 1 分给余数最大的（三者余数相同，给下标最小的）
    assert result == [4, 3, 3]


def test_hundred_rows_one_yuan() -> None:
    """docs/05 §10 #13：100 行各分摊 1 分，正好 100 分。"""
    result = allocate(100, [10000] * 100)
    assert len(result) == 100
    assert sum(result) == 100
    assert all(r == 1 for r in result)


def test_zero_total() -> None:
    assert allocate(0, [10, 20]) == [0, 0]


def test_zero_eligible_returns_zeros() -> None:
    """没有任何可分摊基数时不能凭空分配。"""
    assert allocate(100, [0, 0, 0]) == [0, 0, 0]


def test_empty_input() -> None:
    assert allocate(100, []) == []


def test_negative_total_rejected() -> None:
    with pytest.raises(ValueError):
        allocate(-1, [10])


def test_all_goes_to_only_eligible_row() -> None:
    """只有一行有基数时，全部给它。"""
    assert allocate(50, [0, 100, 0]) == [0, 50, 0]


def test_deterministic() -> None:
    """docs/05 §10 #15 的基础：同一输入必须永远同一结果。"""
    args = (7777, [1234, 5678, 9012, 345])
    assert allocate(*args) == allocate(*args)


# ============================================================
# 带行上限：截断与溢出
# ============================================================


def test_cap_truncates_and_overflows() -> None:
    """docs/05 §10 #6：某行金额小于按比例分摊额 → 截断，溢出到其他行。

    无门槛券 20 元，商品 A=5.00、B=10.00（合计 15.00）
    按比例 A 该分 6.67，但 A 只有 5.00 → A 拿 5.00，剩下的给 B。
    """
    result = allocate_with_cap(2000, [500, 1000], [500, 1000])
    assert result == [500, 1000], "A 被截断到 5.00，其余全部给 B"
    assert sum(result) == 1500, "优惠封顶到订单金额（不能超过可分摊总额）"


def test_cap_not_reached_behaves_like_plain_allocate() -> None:
    """上限宽松时结果应当与不带上限一致。"""
    eligible = [1000, 2000, 7000]
    cap = [99999] * 3
    assert allocate_with_cap(3300, eligible, cap) == allocate(3300, eligible)


def test_cap_one_cent_row() -> None:
    """docs/05 §10 #6：1 分钱的商品只能被分摊 1 分，剩余溢出到其他行。"""
    result = allocate_with_cap(500, [1, 999], [1, 999])
    assert result[0] <= 1, "1 分钱的行不能被分摊超过 1 分"
    assert result[0] >= 0
    assert sum(result) == 500


def test_cap_total_exceeds_order() -> None:
    """docs/05 §10 #14：优惠超过订单金额 → 封顶到订单金额，不会出现负价。"""
    result = allocate_with_cap(999999, [300, 700], [300, 700])
    assert result == [300, 700]
    assert sum(result) == 1000, "最多只能分摊掉订单本身的金额"


def test_cap_all_zero() -> None:
    assert allocate_with_cap(100, [0, 0], [0, 0]) == [0, 0]


def test_cap_length_mismatch_rejected() -> None:
    with pytest.raises(ValueError):
        allocate_with_cap(100, [1, 2], [1])


def test_cap_zero_eligible_but_positive_cap() -> None:
    """基数为 0 但有上限空间：按行序填满，保证守恒。"""
    result = allocate_with_cap(30, [0, 0], [10, 20])
    assert sum(result) == 30
    assert result == [10, 20]


# ============================================================
# 折扣率
# ============================================================


def test_apply_rate_basic() -> None:
    """85 折：100.00 元优惠 15.00 元。"""
    assert apply_rate(10000, 8500) == 1500


def test_apply_rate_rounds_half_up() -> None:
    """33.33 元打 9 折：优惠 3.333 元 → 四舍五入 3.33 元（333 分）。"""
    assert apply_rate(3333, 9000) == 333


def test_apply_rate_not_bankers_rounding() -> None:
    """内置 round() 是银行家舍入，金额上必须用四舍五入。

    构造一个刚好落在 .5 上的例子：基数 1 分、折扣率 5000（5 折），
    优惠 = 1 * 5000 / 10000 = 0.5 → 四舍五入应当进 1。
    用 round(0.5) 会得到 0。
    """
    assert apply_rate(1, 5000) == 1


def test_apply_rate_with_cap() -> None:
    """封顶生效：9 折但最多减 50 元。"""
    assert apply_rate(100000, 9000, cap=5000) == 5000


def test_apply_rate_cannot_exceed_amount() -> None:
    """优惠不能超过基数本身。"""
    assert apply_rate(100, 1) <= 100


def test_apply_rate_zero_rate() -> None:
    """rate=0 表示不打折（不是 100% off）。"""
    assert apply_rate(10000, 0) == 0


# ============================================================
# 性质测试：三条约束必须恒成立
# ============================================================


@given(
    total=st.integers(min_value=0, max_value=10_000_00),
    eligible=st.lists(st.integers(min_value=0, max_value=1_000_00), min_size=1, max_size=50),
)
@settings(max_examples=300, deadline=None)
def test_property_conservation_and_non_negative(total: int, eligible: list[int]) -> None:
    """**守恒**与**非负**：任意随机输入下都必须成立。

    这是分摊算法最重要的性质 —— 只要有一条不满足，退款就会对不上账。
    """
    result = allocate(total, eligible)

    assert len(result) == len(eligible)
    assert all(r >= 0 for r in result), "不能出现负分摊"

    if sum(eligible) > 0:
        assert sum(result) == total, "分摊必须精确守恒"
    else:
        assert sum(result) == 0, "没有基数时不能凭空分配"


@given(
    total=st.integers(min_value=0, max_value=10_000_00),
    eligible=st.lists(st.integers(min_value=0, max_value=1_000_00), min_size=1, max_size=30),
)
@settings(max_examples=300, deadline=None)
def test_property_cap_respected(total: int, eligible: list[int]) -> None:
    """带上限时分摊额永远不超过 cap，且总额封顶到 Σcap。"""
    cap = eligible  # 用行金额本身当上限，这是最常见的用法
    result = allocate_with_cap(total, eligible, cap)

    assert len(result) == len(eligible)
    assert all(r >= 0 for r in result), "不能出现负分摊"
    assert all(r <= c for r, c in zip(result, cap, strict=True)), "分摊不能超过行金额"
    assert sum(result) == min(total, sum(cap)), "总额封顶到可分摊总额"


@given(
    total=st.integers(min_value=0, max_value=1_000_00),
    eligible=st.lists(st.integers(min_value=1, max_value=1_000_00), min_size=1, max_size=30),
    cap_ratio=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=300, deadline=None)
def test_property_cap_partial(total: int, eligible: list[int], cap_ratio: int) -> None:
    """上限**故意设小**（基数的 1/cap_ratio）时，截断与重分摊仍然守恒。"""
    cap = [max(1, e // cap_ratio) for e in eligible]
    result = allocate_with_cap(total, eligible, cap)

    assert all(0 <= r <= c for r, c in zip(result, cap, strict=True))
    assert sum(result) == min(total, sum(cap))


@given(
    amount=st.integers(min_value=0, max_value=10_000_000),
    rate=st.integers(min_value=0, max_value=10000),
    cap=st.integers(min_value=0, max_value=10_000_000),
)
@settings(max_examples=300, deadline=None)
def test_property_rate_bounds(amount: int, rate: int, cap: int) -> None:
    """折扣额永远在 [0, amount] 内，且不超过封顶。"""
    discount = apply_rate(amount, rate, cap=cap)
    assert 0 <= discount <= amount
    if cap > 0:
        assert discount <= cap
