"""优惠分摊算法（docs/05 §6）。

**为什么分摊必须精确到分**（docs/05 §6.4）：用户买了 A、B 两件，用 30 元券，只退 A。
退款金额 = A 的实付 = A 原价 - A 分摊到的优惠。如果三行分摊之和是 29.99 而不是 30.00，
那么全额退款时退款 = 100 - 29.99 = 70.01，比实付多 1 分 —— **平台资损，
而且对账永远对不平**。

所以这个模块的三条约束必须恒成立（docs/05 §6.3）：

1. **守恒**：``Σ result == total``（在未被 cap 截断时）
2. **非负**：``result[i] >= 0``
3. **不超行金额**：``result[i] <= cap[i]``

全部用整数运算。**禁用浮点**（``0.1 + 0.2 != 0.3``）和内置 ``round()``
（它是银行家舍入，``round(2.5) == 2``）。四舍五入用 ``(x * rate + 5000) // 10000``。

本模块是纯函数、零依赖 —— 不 import 任何东西，可以被任意模块安全复用。
"""

from __future__ import annotations


def allocate(total: int, eligible: list[int]) -> list[int]:
    """**最大余数法**分摊：把 ``total`` 按 ``eligible`` 的比例分给每一行。

    算法（docs/05 §6.2）：

    1. 算每行的"精确值" ``total * eligible_i / Σeligible``
    2. 取整数部分，余数留着
    3. 整数部分之和与 ``total`` 的缺口（0 <= gap < n）按**余数降序**补 1 分
    4. 余数相同时按**下标升序**（确定性，保证同一输入永远同一结果）

    :param total: 要分摊的总优惠（分），必须 >= 0
    :param eligible: 每行的分摊基数（分），必须 >= 0。下标顺序即平局裁决顺序
    :return: 每行分到的金额（分），``Σ == total``（当 ``Σeligible > 0``）
    """
    n = len(eligible)
    if n == 0:
        return []
    if total < 0:
        raise ValueError("分摊总额不能为负")

    total_eligible = sum(eligible)
    if total_eligible <= 0:
        # 没有任何可分摊的基数：全为 0，不能凭空分配
        return [0] * n

    # ① 整数部分与余数。全整数运算，没有浮点误差
    result: list[int] = []
    remainders: list[int] = []
    for e in eligible:
        quotient, remainder = divmod(total * e, total_eligible)
        result.append(quotient)
        remainders.append(remainder)

    # ② 补足缺口。gap 一定 < n（每个余数都 < divisor，最多累积 n-1）
    gap = total - sum(result)
    order = sorted(range(n), key=lambda i: (-remainders[i], i))
    for i in order[:gap]:
        result[i] += 1

    return result


def allocate_with_cap(total: int, eligible: list[int], cap: list[int]) -> list[int]:
    """带**行上限**的最大余数法分摊（docs/05 §6.3）。

    解决的问题：按比例分摊可能超过某行自身的金额，把它变成负数。

        无门槛券 20 元，商品 A=5.00、B=10.00（合计 15.00）
        按比例：A → 6.67，B → 13.33   ← A 分摊 6.67 > A 的 5.00，A 变成负价

    做法：**逐行截断 + 迭代重分摊**。某行超上限就截断到上限，把溢出量在
    未满的行中重新按比例分摊，直到溢出为 0 或所有行都到上限。最后一轮
    用最大余数法补齐分差。

    循环一定终止：每一轮要么溢出为 0（结束），要么至少有一行被标记为已满
    （活跃行数严格递减）。

    :param total: 要分摊的总优惠（分），>= 0
    :param eligible: 每行的分摊基数（分），>= 0
    :param cap: 每行能被分摊的上限（分），通常就是该行的当前金额
    :return: 每行分到的金额，满足 ``Σ == min(total, Σcap)`` 且 ``0 <= result[i] <= cap[i]``
    """
    n = len(eligible)
    if n == 0:
        return []
    if total < 0:
        raise ValueError("分摊总额不能为负")
    if len(cap) != n:
        raise ValueError("eligible 与 cap 长度必须一致")

    result = [0] * n
    # 优惠封顶到"所有行能承受的总额"：优惠超过订单金额时不能让应付变成负数
    remaining = min(total, sum(cap))
    done = [cap[i] <= 0 for i in range(n)]

    while remaining > 0:
        active = [i for i in range(n) if not done[i]]
        if not active:
            break  # 全部达上限

        active_eligible = sum(eligible[i] for i in active)
        if active_eligible <= 0:
            # 剩余行的基数都是 0：没法按比例分，按行序依次填满以保证守恒
            for i in active:
                take = min(cap[i] - result[i], remaining)
                result[i] += take
                remaining -= take
            break

        # 本轮按比例分摊 remaining（复用 allocate，保证本轮内部守恒）
        this_round = allocate(remaining, [eligible[i] for i in active])

        overflow = 0
        for i, amount in zip(active, this_round, strict=True):
            space = cap[i] - result[i]
            if amount >= space:
                # 超出上限：截断，多出来的进下一轮
                overflow += amount - space
                result[i] = cap[i]
                done[i] = True
            else:
                result[i] += amount
        remaining = overflow

    return result


def apply_rate(amount: int, rate: int, *, cap: int = 0) -> int:
    """按折扣率算优惠额。``rate = 8500`` 表示 85 折（即优惠 15%）。

    docs/05 §5.3：四舍五入到分，用整数实现。

        (amount * rate + 5000) // 10000

    **不能用内置 round()**：它是银行家舍入（``round(2.5) == 2``），
    在金额上会造成可复现但难以解释的偏差。

    :param amount: 基数（分）
    :param rate: 折扣率，万分之一为单位（8500 = 85 折）
    :param cap: 封顶（分），0 表示不限
    :return: 优惠金额（分），不超过 amount
    """
    if amount <= 0 or rate <= 0:
        return 0
    # 优惠额 = 基数 × (10000 - rate) / 10000，四舍五入
    discount = (amount * (10000 - rate) + 5000) // 10000
    if cap > 0:
        discount = min(discount, cap)
    # 优惠不能超过基数本身
    return min(discount, amount)
