"""退款金额的计算 —— **纯函数，不碰数据库**。

放在单独文件里是为了能直接单测（与 ``promotion/allocation.py``、``freight/calculator.py``
同样的做法）。金额算错是资损，必须能脱离数据库反复验证。

三个核心规则：

1. **退款金额不是"原价"，是"实付分摊额"**（``order_item.payable_amount``）。
   分摊在下单时已经算好并持久化，这里直接读，不重算 —— 重算会因基数变化而
   和用户实际付的钱对不上（docs/05 §6.4）。

2. **最后一笔退款用差额法**：按件比例退会在多次退款后累计少几分钱，
   最后一笔取"总额 − 已退"来消除误差。这是退款对账能平的根本（docs/08 §2.2）。

3. **运费以子单为单位整体退或不退，且总在最后一笔售后退**，
   所以天然不会重复退（docs/08 §2.3）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import BizError, ErrorCode, PriceInvariantError
from app.modules.aftersale.models import (
    NO_REASON_WINDOW_DAYS,
    QUALITY_REASONS,
    QUALITY_WINDOW_DAYS,
)
from app.modules.trade.models import OrderItem, OrderSub


def calc_item_refund(item: OrderItem, refund_num: int) -> int:
    """某个订单项退 ``refund_num`` 件应退的商品款（分，不含运费）。

    ★ **最后一笔用差额法**：

    ```
    实付 10.00 元、3 件，分 3 次各退 1 件
    纯比例：1000 * 1 // 3 = 333 → 三次共 999，少 1 分
    差额法：第 1 次 333、第 2 次 333、第 3 次 = 1000 - 666 = 334  ✅
    ```
    """
    if refund_num <= 0:
        raise BizError(ErrorCode.REFUND_NUM_EXCEED, "退货数量必须大于 0")
    if item.refunded_num + refund_num > item.num:
        raise BizError(
            ErrorCode.REFUND_NUM_EXCEED,
            f"退货数量超过可退数量（可退 {item.num - item.refunded_num} 件）",
        )

    # ① 最后一笔：总额减已退，消除分摊误差
    if item.refunded_num + refund_num == item.num:
        return item.payable_amount - item.refunded_amount

    # ② 非最后一笔：按件比例向下取整。用 (num, refund_num) 的精确比例，
    #    不用已退金额反推 —— 反推会把上一笔的取整误差放大
    return item.payable_amount * refund_num // item.num


def is_whole_sub_refund(items: list[OrderItem], applying: dict[int, int]) -> bool:
    """本次退完后，该子单是否**全部**退完。

    ``applying`` 是本次申请的 ``{order_item_id: 退货件数}``。

    ★ 判定要连 ``refunding_num`` 一起算进去：如果另一笔售后正在退同一行，
    那部分货也已经"名花有主"，不能算作"还没退"。
    """
    return all(
        i.refunded_num + i.refunding_num + applying.get(int(i.id), 0) >= i.num for i in items
    )


def calc_freight_refund(sub: OrderSub, *, whole: bool) -> int:
    """退还的运费（分）。**只有整单退才退运费**。

    质量全退与七天无理由在"退多少运费"上是同一个值（都退发货运费），
    差异只体现在 ``freight_bearer``（退货运费由谁承担）—— 那是一个记录字段，
    不改变退款金额（docs/08 §2.3）。
    """
    return int(sub.freight_amount) if whole else 0


def check_refund_limits(sub: OrderSub, items: list[OrderItem], this_refund: int) -> None:
    """退款前的守恒校验（docs/08 §10）。**显式抛异常，不用 assert**。

    ``assert`` 在 ``python -O`` 下会被整个移除，而资金校验绝不能依赖启动参数。

    ``this_refund`` 是本次要退的**总额**（商品款 + 运费），与 ``order_sub`` 对账
    （子单的 ``refunded_amount`` 记的就是"商品款 + 运费"的口径）。
    """
    if int(sub.refunded_amount) + this_refund > int(sub.payable_amount):
        raise BizError(
            ErrorCode.REFUND_AMOUNT_EXCEED,
            f"退款金额超限：已退 {sub.refunded_amount} 分，本次 {this_refund} 分，"
            f"子单应付 {sub.payable_amount} 分",
        )
    for item in items:
        if item.refunded_num + item.refunding_num > item.num:
            raise PriceInvariantError(
                f"订单项 {item.id} 的退款数量超过购买数量"
                f"（已退 {item.refunded_num} + 在退 {item.refunding_num} > {item.num}）"
            )


def assert_within_window(sub: OrderSub, reason_type: int, *, now: datetime | None = None) -> None:
    """售后申请窗口校验（docs/08 §7）。

    - **未发货**（还没签收）随时可退，不看时间
    - 已签收：不想要 7 天、质量问题 15 天，从 ``receive_time`` 起算

    本期不做按类目配置（生鲜 24 小时那种）。
    """
    now = now or datetime.now(UTC)
    if sub.receive_time is None:
        return  # 还没签收 —— 也就是还没发货，随时可退

    days = QUALITY_WINDOW_DAYS if reason_type in QUALITY_REASONS else NO_REASON_WINDOW_DAYS
    received = sub.receive_time
    # 库里存的是带时区的 UTC；这里统一到 aware，避免 naive/aware 相减报错
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    elapsed_days = (now - received).days
    if elapsed_days >= days:
        raise BizError(
            ErrorCode.AFTERSALE_EXPIRED,
            f"已超过售后申请期限（签收后 {days} 天内可申请，已过 {elapsed_days} 天）",
        )
