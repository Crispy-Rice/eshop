"""payment 模块的 ORM 模型。

对应 docs/09-payment.md。**本期只做模拟渠道**（`MockChannel`）——
docs/09 说得很清楚：它的作用是"走通下单 → 支付 → 回调 → 查单 → 退款 →
对账的完整链路"，真实渠道二期接入时只需新增渠道适配器，链路其余部分不变。

三张表：

- ``payment`` —— 支付单。**一个母单一张支付单**
- ``mock_channel_trade`` —— 模拟渠道的交易记录（docs/13 §1 标注"仅 mock 模式"）
- ``payment_refund`` —— 资金退款单。售后模块要它才能把退款打回渠道

真实渠道才需要的 `pay_notify_log`（回调原文）、`reconcile_diff`（对账差异）
本期仍不建 —— 没有真实渠道就没有对账对象。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# 支付单状态
PAY_WAIT = 0
PAY_SUCCESS = 1
PAY_CLOSED = 2
PAY_REFUNDED = 3

PAY_STATUS_TEXT: dict[int, str] = {
    PAY_WAIT: "待支付",
    PAY_SUCCESS: "已支付",
    PAY_CLOSED: "已关闭",
    PAY_REFUNDED: "已退款",
}

# 渠道
CHANNEL_MOCK = "mock"

# 资金退款单状态
REFUND_WAIT = 0
REFUND_PROCESSING = 1
REFUND_SUCCESS = 2
REFUND_FAILED = 3
REFUND_CLOSED = 4

PAY_REFUND_STATUS_TEXT: dict[int, str] = {
    REFUND_WAIT: "待退款",
    REFUND_PROCESSING: "退款中",
    REFUND_SUCCESS: "退款成功",
    REFUND_FAILED: "退款失败",
    REFUND_CLOSED: "已关闭",
}

# 重试到这么多次就进人工，不再自动重试（docs/09 §8）
MAX_REFUND_RETRY = 10


class Payment(Base):
    """支付单。**一个母单一张**（``UNIQUE (order_main_no)``）。

    金额从母单带过来并冗余存储 —— 支付回调时渠道会带金额回来，
    要能独立校验"回调金额与支付单金额是否一致"，不能只信渠道。
    """

    __tablename__ = "payment"
    __table_args__ = (
        UniqueConstraint("pay_no", name="uk_payment_pay_no"),
        UniqueConstraint("order_main_no", name="uk_payment_order_main_no"),
        Index("idx_payment_user", "user_id", "status", text("created_at DESC")),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint("paid_amount >= 0", name="paid_non_negative"),
        # 超退兜底：退款累加绝不会超过实付。条件更新已经拦了一道，这是最后一道
        CheckConstraint("refunded_amount >= 0 AND refunded_amount <= paid_amount", name="refund_conserved"),
        {"schema": "payment", "comment": "支付单"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pay_no: Mapped[str] = mapped_column(String(32), nullable=False, comment="支付单号")
    order_main_no: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="跨 schema 不建外键（docs/13 §0.1）"
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="应付金额（分）")
    paid_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    refunded_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="累计已退（分）"
    )
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'mock'"), comment="第一期只有 mock"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="见 PAY_STATUS_TEXT"
    )
    # 渠道侧的交易号。回调幂等的依据之一
    channel_trade_no: Mapped[str | None] = mapped_column(String(64))
    pay_time: Mapped[datetime | None] = mapped_column(TS)
    close_time: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class MockChannelTrade(Base):
    """模拟渠道的交易记录。

    真实渠道下这一步是"向微信/支付宝发起统一下单"，由渠道维护交易状态；
    模拟渠道就在这里落一条记录，`mock-callback` 接口据此完成"支付成功"的回调。

    ``UNIQUE (out_trade_no)``：一个支付单在渠道侧只能有一笔交易 ——
    重复发起支付时复用已有的那条，而不是新建（真实渠道也是这个语义）。
    """

    __tablename__ = "mock_channel_trade"
    __table_args__ = (
        UniqueConstraint("out_trade_no", name="uk_mock_trade_out_trade_no"),
        {"schema": "payment", "comment": "模拟渠道交易（仅 mock 模式）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    out_trade_no: Mapped[str] = mapped_column(String(32), nullable=False, comment="商户订单号")
    pay_no: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="0待支付 1已支付"
    )
    trade_no: Mapped[str | None] = mapped_column(String(64), comment="渠道交易号")
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(TS)


class PaymentRefund(Base):
    """资金退款单 —— **一次渠道退款调用**（docs/09 §8）。

    与 ``aftersale.refund_order``（业务视角）分开：那一张是用户和商家的处理流程，
    这一张是"我们向渠道发起了一笔退款"的技术记录。一个售后单对应一次资金退款。

    ★ **这张表本身就是退款的持久队列**：``status`` 0/3 + ``next_retry_at`` +
    ``retry_count`` 三件套让"渠道退款失败要重试"不需要额外的 MQ ——
    即时触发靠 ``after_commit``，可靠性靠每分钟扫一次 ``status IN (0,3)``。
    本项目的 outbox（``core.local_message``）目前只写不读，用它投递会写出
    "消息写了没人投"。
    """

    __tablename__ = "payment_refund"
    __table_args__ = (
        UniqueConstraint("refund_no", name="uk_payment_refund_no"),
        # ★ 幂等的核心：一个售后单（或一笔晚付）只能有一次资金退款
        UniqueConstraint("refund_biz_no", name="uk_payment_refund_biz"),
        Index("idx_payment_refund_pay", "pay_no"),
        # 重试扫描：只含待退款与失败的，体积极小
        Index(
            "idx_payment_refund_retry",
            "next_retry_at",
            postgresql_where=text("status IN (0, 3)"),
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        {"schema": "payment", "comment": "资金退款单"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    refund_no: Mapped[str] = mapped_column(String(32), nullable=False, comment="资金退款单号")
    refund_biz_no: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="业务退款号：售后单号，或 LATE:{pay_no}"
    )
    pay_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_main_no: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="退款金额（分）")
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'mock'"), comment="与 payment.channel 一致"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="见 PAY_REFUND_STATUS_TEXT"
    )
    channel_refund_no: Mapped[str | None] = mapped_column(String(64), comment="渠道退款单号")
    fail_reason: Mapped[str | None] = mapped_column(String(255))

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())

    create_time: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    success_time: Mapped[datetime | None] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
