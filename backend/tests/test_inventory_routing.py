"""发货仓路由的**纯函数**单测（不连库）。

``inventory/routing.py`` 是多仓这一块里唯一"算错也不会报错、只会**发错货**"的地方，
所以它的择优规则要单独钉死 —— 集成测试跑不到所有组合。

三条不变量：

1. 前缀匹配：地址码 ``440305`` 命中规则 ``"44"``（广东）
2. **码越长越具体**：``"4403"``（深圳）比 ``"44"`` 具体，两条都命中时取前者
3. ``"0"``（全国兜底）**只在前缀都不匹配时**才生效
4. **候选链的顺序**：规则仓 → 默认仓 → 其余启用仓（id 升序）—— 规则仓没货时按它往后退
"""

from __future__ import annotations

from app.modules.inventory.models import (
    WAREHOUSE_ENABLED,
    Warehouse,
    WarehouseRegionRule,
)
from app.modules.inventory.routing import (
    REGION_ALL,
    pick_rule,
    pick_warehouse,
    region_level_of,
    warehouse_candidates,
)


def _rule(code: str, warehouse_id: int) -> WarehouseRegionRule:
    """内存里的规则对象。纯函数只要属性，不需要 session。"""
    return WarehouseRegionRule(
        id=warehouse_id,  # 用 warehouse_id 当规则 id：测试里够用且好读
        shop_id=1,
        warehouse_id=warehouse_id,
        region_code=code,
        region_level=region_level_of(code),
    )


def _wh(warehouse_id: int, *, is_default: bool = False, status: int = WAREHOUSE_ENABLED) -> Warehouse:
    return Warehouse(
        id=warehouse_id, shop_id=1, name=f"仓{warehouse_id}", is_default=is_default, status=status
    )


# ============================================================
# pick_rule
# ============================================================
def test_longer_prefix_wins() -> None:
    """深圳仓（4403）比广州仓（44）具体 —— 两条都命中时取码长的那个。"""
    rules = [_rule("44", 1), _rule("4403", 2)]
    picked = pick_rule(rules, "440305")
    assert picked is not None and picked.warehouse_id == 2


def test_nationwide_is_only_the_last_resort() -> None:
    """``"0"`` 是兜底，不是"命中一切"：有更具体的前缀时轮不到它。"""
    rules = [_rule(REGION_ALL, 1), _rule("44", 2)]
    assert pick_rule(rules, "440305").warehouse_id == 2  # 命中广东
    assert pick_rule(rules, "110108").warehouse_id == 1  # 北京 → 只有全国兜底


def test_nationwide_matches_any_address() -> None:
    """``"0"`` 是"这个仓谁都能发"：任何地址都命中它（作为兜底）。"""
    rules = [_rule(REGION_ALL, 1)]
    for region_code in ("110108", "440305", "010100", "999999"):
        assert pick_rule(rules, region_code).warehouse_id == 1


def test_no_match_returns_none() -> None:
    assert pick_rule([_rule("44", 1)], "110108") is None


def test_empty_region_code_never_matches() -> None:
    """空码**不能**匹配到任何规则。

    这是防呆：`region_code` 为空（老数据 / 没填地址）时，若被当成"前缀匹配一切"，
    所有订单都会被路由到随便一个仓。
    """
    assert pick_rule([_rule(REGION_ALL, 1), _rule("44", 2)], "") is None


def test_result_is_deterministic_regardless_of_rule_order() -> None:
    """规则的返回顺序不该影响结果（比较里带 id 就是为了这个）。"""
    a, b = _rule("44", 1), _rule("4403", 2)
    assert pick_rule([a, b], "440305").warehouse_id == pick_rule([b, a], "440305").warehouse_id


# ============================================================
# pick_warehouse
# ============================================================
def test_falls_back_to_default_warehouse() -> None:
    default = _wh(9, is_default=True)
    picked = pick_warehouse(
        rules=[_rule("44", 1)],
        enabled={9: default, 1: _wh(1)},
        default_wh=default,
        region_code="110108",
    )
    assert picked is default


def test_rule_target_that_is_disabled_falls_back_to_default() -> None:
    """规则指向的仓停用了 → 视为没命中、走默认仓。

    停用的语义是"这个仓不再接新单"，但不该让**整个广东**都下不了单。
    （历史订单落在停用仓的照常发货 —— 那条路径读订单上记录的仓，不走这里。）
    """
    default = _wh(9, is_default=True)
    picked = pick_warehouse(
        rules=[_rule("44", 1)],
        enabled={9: default},  # 停用的仓不在 enabled 里
        default_wh=default,
        region_code="440305",
    )
    assert picked is default


def test_returns_none_when_shop_has_no_warehouse() -> None:
    """一个仓都没有 → None（调用方报"该店铺未配置发货仓库"）。"""
    assert (
        pick_warehouse(rules=[], enabled={}, default_wh=None, region_code="440305") is None
    )


def test_disabled_rule_target_without_default_returns_none() -> None:
    assert (
        pick_warehouse(rules=[_rule("44", 1)], enabled={}, default_wh=None, region_code="440305")
        is None
    )


# ============================================================
# region_level_of
# ============================================================
# ============================================================
# warehouse_candidates：候选链的顺序（缺货兜底的依据）
# ============================================================
def _candidates(*, rules, enabled, default_wh, region_code):
    return warehouse_candidates(
        rules=rules, enabled=enabled, default_wh=default_wh, region_code=region_code
    )


def test_candidates_put_the_rule_hit_first() -> None:
    """规则仓在最前 —— 它有货时走的就是它，与 v1 行为一致。"""
    out = _candidates(
        rules=[_rule("44", 2)],
        enabled={1: _wh(1, is_default=True), 2: _wh(2)},
        default_wh=_wh(1, is_default=True),
        region_code="440305",
    )
    assert [int(w.id) for w in out] == [2, 1]


def test_candidates_continue_with_other_enabled_warehouses() -> None:
    """★ 兜底的依据：规则仓与默认仓之后还跟着**其余启用仓**（id 升序）。

    v1 只有"规则仓 → 默认仓"这一跳，所以那两个都没货就整单失败。
    """
    out = _candidates(
        rules=[_rule("44", 3)],
        enabled={1: _wh(1, is_default=True), 2: _wh(2), 3: _wh(3)},
        default_wh=_wh(1, is_default=True),
        region_code="440305",
    )
    assert [int(w.id) for w in out] == [3, 1, 2]


def test_candidates_dedupe_and_skip_disabled() -> None:
    """规则仓**就是**默认仓时不重复；停用的仓不出现（``enabled`` 里本来就没有）。"""
    same = _candidates(
        rules=[_rule("44", 1)],
        enabled={1: _wh(1, is_default=True)},
        default_wh=_wh(1, is_default=True),
        region_code="440305",
    )
    assert [int(w.id) for w in same] == [1], "规则仓与默认仓是同一个，不该出现两次"

    stopped = _candidates(
        rules=[_rule("44", 2)],
        enabled={1: _wh(1, is_default=True)},
        default_wh=_wh(1, is_default=True),
        region_code="440305",
    )
    assert [int(w.id) for w in stopped] == [1], "停用的规则仓不该进候选"


def test_candidates_without_rules_or_default_are_just_enabled() -> None:
    out = _candidates(
        rules=[], enabled={2: _wh(2), 1: _wh(1)}, default_wh=None, region_code="440305"
    )
    assert [int(w.id) for w in out] == [1, 2], "没有规则也没有默认仓时，按 id 升序"


def test_candidates_first_element_matches_pick_warehouse() -> None:
    """★ 两者必须一致：只要该店有规则或默认仓，``pick_warehouse`` 与候选链的第一个就是
    同一个仓。不一致就意味着"只按规则算"的调用方与真正下单会给出不同的仓。"""
    rules = [_rule("44", 2)]
    enabled = {1: _wh(1, is_default=True), 2: _wh(2)}
    default_wh = _wh(1, is_default=True)
    for region in ("440305", "310115", ""):
        picked = pick_warehouse(
            rules=rules, enabled=enabled, default_wh=default_wh, region_code=region
        )
        candidates = _candidates(
            rules=rules, enabled=enabled, default_wh=default_wh, region_code=region
        )
        assert (picked is None and not candidates) or (
            picked is not None
            and candidates
            and int(picked.id) == int(candidates[0].id)
        )


def test_region_level_is_derived_from_code_length() -> None:
    assert region_level_of("44") == 1  # 省
    assert region_level_of("4403") == 2  # 市
    assert region_level_of("440305") == 3  # 区
    assert region_level_of(REGION_ALL) == 1  # 全国按省级记
