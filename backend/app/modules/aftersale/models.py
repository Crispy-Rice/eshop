"""aftersale 模块的 ORM 模型。

对应 docs/08-aftersale.md 与 docs/13-schema.md §3。

**两张表，业务视角与技术视角分开**：

- ``refund_order`` —— 售后单。用户/商家/平台处理流程的载体，有完整状态机
- ``refund_item`` —— 售后单项。**金额在申请时冻结在这里**，退款成功时只读不算

资金侧的一次渠道退款调用记在 ``payment.payment_refund``（另一个模块）——
一个售后单对应一次资金退款，两者用 ``refund_biz_no`` 关联。

**为什么 ``refund_amount`` 与 ``refund_freight`` 分开记**（docs/08 §2.1）：
商品款的上限是「订单项的实付分摊额之和」，运费的上限是「子单的运费」，
两个上限不同。合在一起记就需要"含运费分摊时调整"的特例，而分开记每一层的
约束都清晰 —— ``order_item.refunded_amount`` 只记商品款，子单的
``refunded_amount`` 才是商品款 + 运费之和。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# ---------------- 售后类型（docs/08 §1）----------------
REFUND_ONLY = 1
RETURN_REFUND = 2

REFUND_TYPE_TEXT: dict[int, str] = {
    REFUND_ONLY: "仅退款",
    RETURN_REFUND: "退货退款",
}

# 售后单状态文案。**与 core.enums.RefundStatus 的取值一一对应**，
# 放在这里是为了让 schemas 与异常消息共用同一份文案
REFUND_STATUS_TEXT: dict[int, str] = {
    10: "待商家审核",
    11: "商家已拒绝",
    20: "待退款",
    30: "待买家寄回",
    40: "待商家收货",
    50: "质检中",
    51: "质检不通过",
    60: "退款中",
    70: "退款成功",
    80: "已关闭",
    81: "已撤销",
    90: "平台介入中",
}

# ---------------- 申请原因 ----------------
REASON_QUALITY = 1
REASON_NO_LONGER_WANT = 2
REASON_WRONG_ITEM = 3
REASON_MISSING = 4
REASON_COUNTERFEIT = 5
REASON_OTHER = 6

REASON_TEXT: dict[int, str] = {
    REASON_QUALITY: "质量问题",
    REASON_NO_LONGER_WANT: "不想要了",
    REASON_WRONG_ITEM: "发错货",
    REASON_MISSING: "少件/漏发",
    REASON_COUNTERFEIT: "假冒品牌",
    REASON_OTHER: "其他",
}

# 质量相关的原因享受更长的售后窗口（15 天 vs 7 天）
QUALITY_REASONS = frozenset({REASON_QUALITY, REASON_WRONG_ITEM, REASON_MISSING, REASON_COUNTERFEIT})

# ---------------- 退货运费承担方 ----------------
BEARER_USER = 1
BEARER_MERCHANT = 2
BEARER_PLATFORM = 3

BEARER_TEXT: dict[int, str] = {
    BEARER_USER: "买家承担",
    BEARER_MERCHANT: "商家承担",
    BEARER_PLATFORM: "平台承担",
}

# ---------------- 质检结果 ----------------
QUALITY_PASS = 1
QUALITY_FAIL = 2

QUALITY_RESULT_TEXT: dict[int, str] = {
    QUALITY_PASS: "合格",
    QUALITY_FAIL: "不合格",
}

# ★ 「进行中」的状态集合。部分唯一索引 uk_refund_sub_active 用它 ——
#   同一子单同时只能有一个进行中的售后；终态不占用，所以可以再发起新的。
ACTIVE_STATUSES = (10, 20, 30, 40, 50, 60)

# 各环节的时限（docs/13 §3 的 DEADLINES）
REVIEW_DEADLINE_HOURS = 48  # 商家审核
RETURN_DEADLINE_DAYS = 7  # 用户寄回
RECEIVE_DEADLINE_DAYS = 7  # 商家收货
QUALITY_DEADLINE_HOURS = 48  # 质检

# 售后申请窗口（从 order_sub.receive_time 起算，docs/08 §7）
NO_REASON_WINDOW_DAYS = 7
QUALITY_WINDOW_DAYS = 15


class RefundOrder(Base):
    """售后单：用户/商家处理流程的载体。

    ``source_status`` 记下**申请那一刻子单的状态**（20/30/40）—— 拒绝、撤销、
    质检不通过时，子单要恢复到各自的原状态。单看目标状态表达不了这一点
    （20→60 和 30→60 都得回到各自的原处），所以必须存下来。
    """

    __tablename__ = "refund_order"
    __table_args__ = (
        UniqueConstraint("refund_no", name="uk_refund_no"),
        # 申请幂等：与下单的 uk_order_main_request 同构
        Index("uk_refund_request", "user_id", "request_id", unique=True),
        # ★ 一个子单同时只能有一个进行中的售后。终态不占用，可以再发起新的
        Index(
            "uk_refund_sub_active",
            "order_sub_no",
            unique=True,
            postgresql_where=text(f"status IN {ACTIVE_STATUSES}"),
        ),
        Index("idx_refund_user_status", "user_id", "status", text("created_at DESC")),
        Index("idx_refund_shop_status", "shop_id", "status", text("created_at DESC")),
        Index("idx_refund_main", "order_main_no"),
        Index("idx_refund_sub", "order_sub_no"),
        # 超时扫描：只含还带 deadline 的进行中售后，体积极小
        Index("idx_refund_deadline", "deadline", postgresql_where=text("deadline IS NOT NULL")),
        CheckConstraint("refund_amount >= 0", name="refund_amount_non_negative"),
        CheckConstraint("refund_freight >= 0", name="refund_freight_non_negative"),
        CheckConstraint("refund_points >= 0", name="refund_points_non_negative"),
        {"schema": "aftersale", "comment": "售后单（退款与退货）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    refund_no: Mapped[str] = mapped_column(String(32), nullable=False, comment="售后单号")
    order_sub_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_main_no: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_id: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="申请售后的幂等键"
    )

    refund_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="见 REFUND_TYPE_TEXT"
    )
    reason_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="见 REASON_TEXT"
    )
    reason_desc: Mapped[str | None] = mapped_column(String(255))
    images: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="凭证图片"
    )

    # 金额。**商品款与运费分开记**，各自的上限不同（docs/08 §2.1）
    refund_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="商品退款金额（不含运费）"
    )
    refund_freight: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="退还的运费"
    )
    freight_bearer: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("2"),
        comment="退货运费承担方 1买家 2商家 3平台",
    )
    refund_points: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
        comment="返还积分。本期无积分体系，恒为 0",
    )

    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("10"), comment="见 core.enums.RefundStatus"
    )
    source_status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="申请时的子单状态，拒绝/撤销时恢复用"
    )

    merchant_remark: Mapped[str | None] = mapped_column(String(255))
    reject_reason: Mapped[str | None] = mapped_column(String(255))

    quality_result: Mapped[int | None] = mapped_column(
        SmallInteger, comment="1合格 2不合格，见 QUALITY_RESULT_TEXT"
    )
    quality_remark: Mapped[str | None] = mapped_column(String(255))
    quality_images: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="质检留证照片"
    )

    return_express: Mapped[str | None] = mapped_column(String(32), comment="退货快递公司")
    return_express_no: Mapped[str | None] = mapped_column(String(64), comment="退货快递单号")

    apply_time: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    merchant_handle_time: Mapped[datetime | None] = mapped_column(TS)
    return_time: Mapped[datetime | None] = mapped_column(TS)
    receive_time: Mapped[datetime | None] = mapped_column(TS)
    quality_time: Mapped[datetime | None] = mapped_column(TS)
    refund_time: Mapped[datetime | None] = mapped_column(TS)
    close_time: Mapped[datetime | None] = mapped_column(TS)

    # ★ 当前环节的截止时间。一个部分索引 + 一个 cron 就覆盖了所有环节的超时，
    #   不必给每个环节各开一个时间列（docs/13 §3）。终态时为 NULL。
    deadline: Mapped[datetime | None] = mapped_column(
        TS, comment="当前环节的截止时间，终态为 NULL"
    )

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class RefundItem(Base):
    """售后单项。**金额在申请时冻结**，退款成功时直接读，不重算。

    重算会在并发与重放下产生"审批金额 ≠ 打款金额"，而这份金额是报给用户、
    商家审批过的契约。
    """

    __tablename__ = "refund_item"
    __table_args__ = (
        UniqueConstraint("refund_no", "order_item_id", name="uk_refund_item"),
        Index("idx_refund_item_order_item", "order_item_id"),
        CheckConstraint("refund_num > 0", name="refund_num_positive"),
        CheckConstraint("refund_amount >= 0", name="refund_amount_non_negative"),
        {"schema": "aftersale", "comment": "售后单项"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    refund_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("aftersale.refund_order.refund_no"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    refund_num: Mapped[int] = mapped_column(Integer, nullable=False, comment="本次退的件数")
    refund_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="该行的商品退款金额（分）"
    )
    refund_points: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="本期恒为 0"
    )

    # 商品快照：售后详情要展示用户当时买的是什么，不能读 SKU 的当前值
    spu_title_snap: Mapped[str] = mapped_column(String(120), nullable=False)
    sku_spec_snap: Mapped[str] = mapped_column(String(255), nullable=False)
    cover_image_snap: Mapped[str] = mapped_column(String(255), nullable=False)

    # 仅用于排查：幂等其实由 inventory.stock_biz_key 保证
    stock_restored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="库存是否已回补"
    )
    restore_time: Mapped[datetime | None] = mapped_column(TS)

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
