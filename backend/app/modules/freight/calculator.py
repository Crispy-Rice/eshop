"""运费计算引擎（docs/06）。

**纯函数**：不查库、不碰 Redis、没有任何 ``await``。模板与绑定由调用方预加载好传进来
（docs/06 §11 明确要求"计算阶段不允许 await"）。

三块逻辑：

1. **单段计费**——首重 + 续重的整数向上取整
2. **区域规则解析**——按行政区划码的前缀关系匹配（区 > 市 > 省 > 全国）
3. **多 SKU 冲突裁决**——同仓取"首重最高"的模板，**只收一次首重**

第 3 条是需求里最特殊的一条。背后的物理事实是：同一个仓库发出的商品是一个包裹，
首重成本只发生一次。按每个 SKU 各收一次首重是重复收费，用户会炸。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.modules.freight.models import (
    CHARGE_BY_PIECE,
    CHARGE_BY_WEIGHT,
    FALLBACK_WEIGHT_G,
    HUGE_WEIGHT_G,
    MERGE_BY_HIGHEST_FIRST_UNIT,
    REGION_ALL,
)

# 运费为 0 的原因，前端据此给出人话提示
FREE_REASON_ALWAYS = "ALWAYS"  # 模板配了全场包邮
FREE_REASON_THRESHOLD = "THRESHOLD"  # 满额包邮
FREE_REASON_NUM = "NUM"  # 满件包邮


def ceil_div(a: int, b: int) -> int:
    """``a >= 0``、``b > 0`` 时的整数向上取整。

    ★ 不用 ``math.ceil(a / b)``：那会走一次浮点除法，金额和重量上没必要冒这个险。
    """
    if b <= 0:
        raise ValueError("除数必须为正")
    return -(-a // b)


# ============================================================
# 输入 / 输出
# ============================================================
@dataclass(frozen=True, slots=True)
class FreightRule:
    """一段计费规则。模板默认值经区域规则覆盖后的**最终值**。"""

    charge_type: int
    first_unit: int
    first_price: int
    add_unit: int
    add_price: int
    free_shipping: bool
    # 满额包邮属于"该模板下商品"的门槛，语义上归规则；
    # free_num（满件）是模板级的营销选项，所以单独传，见 check_free_shipping
    free_threshold: int = 0


@dataclass(slots=True)
class FreightItem:
    """参与运费计算的一行商品。

    ``warehouse_id`` 决定它属于哪个包裹；``rule`` 是**已解析好**的计费规则
    （调用方已经按 SKU 的绑定优先级 + 收货区域算出来了）。
    """

    sku_id: int
    num: int
    weight_g: int
    amount: int  # 这一行的商品金额（分），满额包邮要用
    warehouse_id: int
    template_id: int
    title: str = ""
    rule: FreightRule = field(
        default_factory=lambda: FreightRule(CHARGE_BY_WEIGHT, 1000, 0, 1000, 0, False)
    )
    def effective_weight(self) -> int:
        """单件重量。历史脏数据 ``weight_g = 0`` 时按 500g 兜底（docs/06 §10）。"""
        return self.weight_g if self.weight_g > 0 else FALLBACK_WEIGHT_G


@dataclass(slots=True)
class PackageResult:
    """一个包裹（= 一个仓库）的运费。"""

    warehouse_id: int
    template_id: int
    template_name: str
    freight: int
    weight_g: int
    qty: int
    amount: int
    is_free: bool
    free_reason: str | None = None
    sku_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class FreightResult:
    total: int
    packages: list[PackageResult]
    notices: list[str] = field(default_factory=list)


# ============================================================
# 单段计费
# ============================================================
def charge_rule(rule: FreightRule, total_weight_g: int, total_qty: int) -> int:
    """按规则算一个包裹的运费。

    按重量：``W <= F ? P_f : P_f + ceil((W - F) / A) * P_a``
    按件数：把 ``W`` 换成件数，公式完全一样。
    """
    if rule.charge_type == CHARGE_BY_PIECE:
        base, first, add = total_qty, rule.first_unit, rule.add_unit
    else:
        # 体积计费第一期没实现，落到重量上（字段留了，等有体积数据再说）
        base, first, add = total_weight_g, rule.first_unit, rule.add_unit

    if base <= first:
        return rule.first_price
    return rule.first_price + ceil_div(base - first, add) * rule.add_price


def check_free_shipping(
    rule: FreightRule, amount: int, qty: int, free_num: int
) -> tuple[bool, str | None]:
    """包邮判定（docs/06 §4.3）。

    ★ ``amount`` / ``qty`` 是**该模板下商品**的金额与件数，不是整单的 ——
    这是文档点名的常见误解。不同模板的商品各自判断包邮。

    ``free_num``（满件包邮）不在 ``FreightRule`` 里：它是模板级的营销选项，
    而 ``FreightRule`` 是计费参数。分开传让两边的职责各自单一。
    """
    if rule.free_shipping:
        return True, FREE_REASON_ALWAYS
    if rule.free_threshold > 0 and amount >= rule.free_threshold:
        return True, FREE_REASON_THRESHOLD
    if free_num > 0 and qty >= free_num:
        return True, FREE_REASON_NUM
    return False, None


# ============================================================
# 区域规则解析
# ============================================================
@dataclass(frozen=True, slots=True)
class RegionRuleEntry:
    """一条候选区域规则（调用方已从库里取出来）。"""

    region_code: str
    region_level: int
    priority: int
    rule: FreightRule


def region_matches(rule_region_code: str, address_region_code: str) -> bool:
    """区域码是否命中收货地址。

    中国行政区划码（GB/T 2260）是前缀结构：``310115``（浦东新区）的前 2 位是上海市、
    前 4 位是上海市辖区。所以一条省级规则（``31``）能匹配区级地址（``310115``），
    一条市级规则（``3101``）也能。

    ``"0"`` 是全国默认，匹配一切。
    """
    if rule_region_code == REGION_ALL:
        return True
    if not address_region_code:
        return False
    return address_region_code.startswith(rule_region_code)


def pick_region_rule(
    entries: list[RegionRuleEntry], address_region_code: str
) -> RegionRuleEntry | None:
    """从候选里选出最匹配的一条。

    排序：**区域码越长越精确**（区 > 市 > 省 > 全国），同长度时 ``priority`` 大的优先，
    再平局按区域码升序保证确定性。
    """
    matched = [e for e in entries if region_matches(e.region_code, address_region_code)]
    if not matched:
        return None
    return max(
        matched,
        key=lambda e: (len(e.region_code) if e.region_code != REGION_ALL else 0, e.priority, e.region_code),
    )


# ============================================================
# 冲突裁决 + 主流程
# ============================================================
def _winner_key(item: FreightItem) -> tuple[int, int, int, int]:
    """裁决排序键（docs/06 §5.2）。

    ① 首重（``first_unit``）大者优先
    ② 相同则首重价大者优先
    ③ 再相同则续重价大者优先
    ④ 全相同则 **sku_id 升序** —— 取负数让它和"越大越优先"的语义一致，
       同时保证结果确定（同样的输入永远同样的输出）
    """
    r = item.rule
    return (r.first_unit, r.first_price, r.add_price, -item.sku_id)


def is_conflict(a: FreightItem, b: FreightItem) -> bool:
    """同仓、跨模板、且计费参数不一致 —— 才算冲突。"""
    if a.warehouse_id != b.warehouse_id or a.template_id == b.template_id:
        return False
    pa = (a.rule.charge_type, a.rule.first_unit, a.rule.first_price, a.rule.add_unit, a.rule.add_price)
    pb = (b.rule.charge_type, b.rule.first_unit, b.rule.first_price, b.rule.add_unit, b.rule.add_price)
    return pa != pb


def calc_freight(
    items: list[FreightItem],
    *,
    template_names: dict[int, str] | None = None,
    free_num_of: dict[int, int] | None = None,
    merge_type_of: dict[int, int] | None = None,
) -> FreightResult:
    """整单运费。按仓库分组，每组一个包裹。

    ``items`` 里的每一项都必须已经解析好 ``rule``（区域规则已应用）。
    没解析出规则的 SKU 不该出现在这里 —— 调用方应当先拒绝掉（``NOT_DELIVERABLE``）。
    """
    if not items:
        return FreightResult(total=0, packages=[], notices=[])

    names = template_names or {}
    free_nums = free_num_of or {}
    merge_types = merge_type_of or {}

    packages: list[PackageResult] = []
    notices: list[str] = []

    # ① 按仓库分组。**按 id 排序**保证结果确定
    by_warehouse: dict[int, list[FreightItem]] = {}
    for it in items:
        by_warehouse.setdefault(it.warehouse_id, []).append(it)

    for wh_id in sorted(by_warehouse):
        group = by_warehouse[wh_id]

        # ② 裁决出本包裹的计费模板（取首重最高者）
        winner = max(group, key=_winner_key)
        rule = winner.rule
        merge_type = merge_types.get(winner.template_id, MERGE_BY_HIGHEST_FIRST_UNIT)

        # ③ 合并该仓所有商品的重量与金额。
        #    ★ 即使某些 SKU 本来绑的是别的模板，它们的重量也计入 ——
        #      因为同仓一起发，物理上就是一个包裹（docs/06 §5.2 R3）
        total_weight = sum(it.effective_weight() * it.num for it in group)
        total_qty = sum(it.num for it in group)
        total_amount = sum(it.amount for it in group)

        if merge_type != MERGE_BY_HIGHEST_FIRST_UNIT:
            # 不合并的模板：各算各的（文档里提到但第一期默认走合并）
            freight = sum(
                charge_rule(it.rule, it.effective_weight() * it.num, it.num) for it in group
            )
            packages.append(
                PackageResult(
                    warehouse_id=wh_id,
                    template_id=winner.template_id,
                    template_name=names.get(winner.template_id, ""),
                    freight=freight,
                    weight_g=total_weight,
                    qty=total_qty,
                    amount=total_amount,
                    is_free=False,
                    sku_ids=[it.sku_id for it in group],
                )
            )
            continue

        # ④ 包邮判定。用**该包裹内**（即全部按 winner 规则计费的商品）的金额与件数
        free, reason = check_free_shipping(
            rule, total_amount, total_qty, free_nums.get(winner.template_id, 0)
        )
        freight = 0 if free else charge_rule(rule, total_weight, total_qty)

        if not free and total_weight > HUGE_WEIGHT_G:
            notices.append("该包裹重量较大，运费可能偏高，建议联系客服确认")

        packages.append(
            PackageResult(
                warehouse_id=wh_id,
                template_id=winner.template_id,
                template_name=names.get(winner.template_id, ""),
                freight=freight,
                weight_g=total_weight,
                qty=total_qty,
                amount=total_amount,
                is_free=free,
                free_reason=reason,
                sku_ids=[it.sku_id for it in group],
            )
        )

    # ⑤ 跨仓提示：不同仓库各收一次首重，用户需要知道为什么运费比预期高
    if len(packages) > 1:
        notices.append(f"商品由 {len(packages)} 个仓库发出，运费按包裹分别计算（各计一次首重）")

    return FreightResult(
        total=sum(p.freight for p in packages), packages=packages, notices=notices
    )
