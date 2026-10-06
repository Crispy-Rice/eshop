"""订单号生成（docs/07 §6）。

```
母单号：M + yyyyMMdd + 雪花后 10 位 + Luhn 校验位   → 共 20 位
子单号：母单号 + "-" + 序号
```

**日期前缀**：排查问题时从订单号就能知道下单日期，按时间归档时可以直接路由。

**Luhn 校验位**：防止用户手输错订单号查出别人的订单（配合登录态校验归属）。
校验位算错时直接拒绝，不必查库。

**为什么不用自增 ID**：会泄露业务量 —— 竞争对手下两单就能估算日单量。
"""

from __future__ import annotations

from datetime import datetime

MAIN_PREFIX = "M"
MAIN_NO_LENGTH = 20  # M(1) + 日期(8) + 序号(10) + 校验位(1)


def luhn_check_digit(digits: str) -> int:
    """算 Luhn 校验位。

    算法：从右往左，偶数位（从 0 数起）乘 2，大于 9 则减 9，全部相加；
    校验位 = (10 - 和 % 10) % 10。
    """
    total = 0
    # 从右往左遍历；因为校验位待补，这里从最后一位开始按"隔一位翻倍"处理
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return (10 - total % 10) % 10


def is_valid_luhn(full: str) -> bool:
    """校验一个完整串（含校验位）是否满足 Luhn。"""
    if not full.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(full)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def build_main_no(snowflake_id: int, *, now: datetime | None = None) -> str:
    """生成母单号。

    雪花 ID 取**后 10 位**：雪花本身是 64 位，直接放进去太长；
    后 10 位在单进程内的唯一性足够（同一毫秒内的序列号在低位）。
    """
    now = now or datetime.now()
    body = f"{now:%Y%m%d}{snowflake_id % 10_000_000_000:010d}"
    assert len(body) == 18, "日期 + 序号必须是 18 位"
    return f"{MAIN_PREFIX}{body}{luhn_check_digit(body)}"


def build_sub_no(main_no: str, index: int) -> str:
    """子单号 = 母单号 + '-' + 序号（从 1 开始）。

    序号不用补零：一个母单的子单数是个位数到几十，补零反而不好读。
    """
    if index < 1:
        raise ValueError("子单序号从 1 开始")
    return f"{main_no}-{index}"


def parse_main_no(main_no: str) -> datetime:
    """从母单号里解出下单日期。

    格式不对或校验位不通过就抛 ``ValueError`` —— 调用方（查询接口）据此
    返回"订单号格式不对"，而不是拿一个错误的号去查库。
    """
    if not main_no.startswith(MAIN_PREFIX) or len(main_no) != MAIN_NO_LENGTH:
        raise ValueError("订单号格式不对")
    body = main_no[1:-1]
    if not body.isdigit():
        raise ValueError("订单号格式不对")
    if not is_valid_luhn(body + main_no[-1]):
        raise ValueError("订单号校验位不通过")
    return datetime.strptime(body[:8], "%Y%m%d")


def is_valid_main_no(main_no: str) -> bool:
    try:
        parse_main_no(main_no)
    except ValueError:
        return False
    return True


def build_pay_no(snowflake_id: int, *, now: datetime | None = None) -> str:
    """支付单号。``P`` 前缀 + 同样的日期 + 序号 + 校验位，便于和订单号区分。"""
    now = now or datetime.now()
    body = f"{now:%Y%m%d}{snowflake_id % 10_000_000_000:010d}"
    return f"P{body}{luhn_check_digit(body)}"


def build_delivery_no(snowflake_id: int, *, now: datetime | None = None) -> str:
    """发货单号。``D`` 前缀。"""
    now = now or datetime.now()
    body = f"{now:%Y%m%d}{snowflake_id % 10_000_000_000:010d}"
    return f"D{body}{luhn_check_digit(body)}"


def build_refund_no(snowflake_id: int, *, now: datetime | None = None) -> str:
    """售后单号。``R`` 前缀。

    与订单号同构（同一天日期 + 序号 + Luhn），排查时一眼能看出是哪天发生的，
    和 ``M`` / ``P`` / ``D`` 放到一起也不会混淆。
    """
    now = now or datetime.now()
    body = f"{now:%Y%m%d}{snowflake_id % 10_000_000_000:010d}"
    return f"R{body}{luhn_check_digit(body)}"


def build_ticket_no(snowflake_id: int, *, now: datetime | None = None) -> str:
    """客服会话号。``T`` 前缀，同样与订单号同构。"""
    now = now or datetime.now()
    body = f"{now:%Y%m%d}{snowflake_id % 10_000_000_000:010d}"
    return f"T{body}{luhn_check_digit(body)}"
