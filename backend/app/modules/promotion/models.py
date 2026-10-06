"""promotion 模块的 ORM 模型。

对应 docs/04-coupon.md（优惠券）与 docs/05-promotion-engine.md（促销活动、叠加规则）。

三条贯穿全模块的设计：

1. **超发有两道防线**：Redis Lua 拦第一道（原子），DB 的条件更新
   （``WHERE issued_count < total_count``）+ 表上的 ``CHECK`` 约束拦第二道。
   即使 Redis 完全失效，DB 也不会超发（docs/04 §5.2）。
2. **限领的 DB 保证不能靠流水表上的唯一索引**（限领 N 张时会误拦），
   所以单独建 ``coupon_user_quota`` 计数表，限领 1 张和 N 张统一处理。
3. **叠加规则数据化**，写在 ``promo_stack_rule`` 里，不写死在代码里——
   新增优惠类型时默认是"互斥"的（安全），要叠加必须显式声明（docs/05 §4.1）。
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
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# ---------------- 券模板 ----------------
# 类型
COUPON_TYPE_THRESHOLD = 1  # 满减券
COUPON_TYPE_DISCOUNT = 2  # 折扣券
COUPON_TYPE_NO_THRESHOLD = 3  # 无门槛券
COUPON_TYPE_EXCHANGE = 4  # 兑换券
COUPON_TYPE_FREIGHT = 5  # 运费券

# 获取方式
COUPON_GET_MANUAL = 1  # 主动领取
COUPON_GET_SYSTEM = 2  # 系统发放（客服补发）
COUPON_GET_CODE = 3  # 兑换码
COUPON_GET_ACTIVITY = 4  # 活动赠送
COUPON_GET_NEW_USER = 5  # 新客自动发

# 有效期类型
VALID_FIXED = 1  # 固定区间
VALID_DAYS_AFTER = 2  # 领取后 N 天

# 适用范围
SCOPE_ALL = 1  # 全场
SCOPE_SKU = 2  # 指定商品
SCOPE_CATEGORY = 3  # 指定类目
SCOPE_SHOP = 4  # 指定店铺

# 状态
COUPON_TPL_NOT_STARTED = 1
COUPON_TPL_ONGOING = 2
COUPON_TPL_ENDED = 3
COUPON_TPL_VOID = 4

# 展示文案。集中在这里，避免路由、前端各写一套中文
COUPON_TYPE_TEXT: dict[int, str] = {
    COUPON_TYPE_THRESHOLD: "满减券",
    COUPON_TYPE_DISCOUNT: "折扣券",
    COUPON_TYPE_NO_THRESHOLD: "无门槛券",
    COUPON_TYPE_EXCHANGE: "兑换券",
    COUPON_TYPE_FREIGHT: "运费券",
}

COUPON_TPL_STATUS_TEXT: dict[int, str] = {
    COUPON_TPL_NOT_STARTED: "未开始",
    COUPON_TPL_ONGOING: "进行中",
    COUPON_TPL_ENDED: "已结束",
    COUPON_TPL_VOID: "已作废",
}

# ---------------- 券实例 ----------------
CODE_UNUSED = 1
CODE_LOCKED = 2
CODE_USED = 3
CODE_EXPIRED = 4
CODE_VOID = 5

CODE_STATUS_TEXT: dict[int, str] = {
    CODE_UNUSED: "未使用",
    CODE_LOCKED: "已锁定",
    CODE_USED: "已使用",
    CODE_EXPIRED: "已过期",
    CODE_VOID: "已作废",
}

# 券来源（券实例）
CODE_SOURCE_MANUAL = 1
CODE_SOURCE_SYSTEM = 2
CODE_SOURCE_EXCHANGE = 3
CODE_SOURCE_ACTIVITY = 4
CODE_SOURCE_REFUND = 5  # 退回

# ---------------- 客服补发的两道闸 ----------------
# ★ 补发**不占活动额度**（``issued_count`` 不变，理由见 issue_by_admin），
#   所以"活动库存"本身不构成约束 —— 这两条是补发自己该有的配额。
#   它们是**事前拒**：事后再查，券已经发出去了。

# 同一个用户、同一个模板，累计最多被补发多少张。
# 不限时间窗 —— 要挡的是"给一个小号反复刷"，那是累积行为，加窗口反而能隔天绕过。
ISSUE_MAX_PER_USER_TPL = 3

# 单个运营 24 小时内最多补发多少张。
# 用**滚动 24 小时**而不是"自然日"：没有零点重置的悬崖，也不用纠结时区
# （全库时间戳都是 UTC，按自然日会变成北京时间早上 8 点重置，很奇怪）。
ISSUE_MAX_PER_OPERATOR_24H = 200

# 补发流水的 biz_key 前缀。用它把"补发"从券的其它流水（锁/核销/退回）里筛出来。
ISSUE_BIZ_KEY_PREFIX = "ISSUE:"

# ---------------- 促销活动 ----------------
ACTIVITY_STATUS_NOT_STARTED = 1
ACTIVITY_STATUS_ONGOING = 2
ACTIVITY_STATUS_ENDED = 3
ACTIVITY_STATUS_VOID = 4

ACTIVITY_STATUS_TEXT: dict[int, str] = {
    ACTIVITY_STATUS_NOT_STARTED: "未开始",
    ACTIVITY_STATUS_ONGOING: "进行中",
    ACTIVITY_STATUS_ENDED: "已结束",
    ACTIVITY_STATUS_VOID: "已作废",
}

# 优惠类型标识。**这些字符串是叠加规则矩阵的键**，改名会破坏历史规则
DISCOUNT_ITEM_PROMO = "PROMO_ITEM"
DISCOUNT_SHOP_PROMO = "PROMO_ORDER_SHOP"
DISCOUNT_PLATFORM_PROMO = "PROMO_ORDER_PLATFORM"
DISCOUNT_COUPON_SHOP = "COUPON_SHOP"
DISCOUNT_COUPON_PLATFORM = "COUPON_PLATFORM"
DISCOUNT_POINT = "POINT"

DISCOUNT_TYPE_TEXT: dict[str, str] = {
    DISCOUNT_ITEM_PROMO: "单品促销",
    DISCOUNT_SHOP_PROMO: "店铺活动",
    DISCOUNT_PLATFORM_PROMO: "平台活动",
    DISCOUNT_COUPON_SHOP: "店铺券",
    DISCOUNT_COUPON_PLATFORM: "平台券",
    DISCOUNT_POINT: "积分抵扣",
}

# 优惠层级（docs/05 §3）。**顺序是语义的一部分**，不能随意调换
LEVEL_ITEM = 0
LEVEL_SHOP = 1
LEVEL_PLATFORM = 2
LEVEL_POINT = 3

LEVEL_TEXT: dict[int, str] = {
    LEVEL_ITEM: "单品级",
    LEVEL_SHOP: "店铺级",
    LEVEL_PLATFORM: "平台级",
    LEVEL_POINT: "积分级",
}

# 层级 → 活动类型标识。**必须一一对应**：``level`` 决定"在哪一层算"，
# ``type`` 只用来查冲突组与叠加矩阵。两者配歪了的后果，两层还不一样 ——
# 单品层压根不看 ``type``（照样生效），订单层按 ``type`` 过滤（静默不生效）。
# 同一种错、两种表现，属于最难查的那类，所以建活动的接口照这张表卡死。
PROMO_TYPE_BY_LEVEL: dict[int, str] = {
    LEVEL_ITEM: DISCOUNT_ITEM_PROMO,
    LEVEL_SHOP: DISCOUNT_SHOP_PROMO,
    LEVEL_PLATFORM: DISCOUNT_PLATFORM_PROMO,
}

# 活动计算方式。含义决定 discount_value 的单位：直降是分、折扣是万分比、特价是分
CALC_DIRECT = 1  # 直降：减 discount_value
CALC_RATE = 2  # 折扣：discount_value 是折扣率（8500 = 85 折）
CALC_FIXED = 3  # 特价：discount_value 就是定价

CALC_TYPE_TEXT: dict[int, str] = {
    CALC_DIRECT: "直降",
    CALC_RATE: "折扣",
    CALC_FIXED: "特价",
}


class CouponTemplate(Base):
    """优惠券模板。

    ★ ``CHECK (issued_count <= total_count)`` 是 DB 层的超发硬约束。
    配合领取时的条件更新 ``WHERE issued_count < total_count``，
    即使 Redis 挂掉、即使有人绕过应用直接写库，也不会超发（docs/04 §5.2）。

    ★ 模板一旦有券发出，**核心字段不可修改**（docs/04 §11 的推荐做法）：
    已发出的券必须使用领取时的规则，否则历史券的退款逻辑会跟着新规则漂移。
    修改入口由 service 层拦（``issued_count > 0`` 就拒绝），只能作废后新建。

    ``status`` 的 ``1/2/3`` 是有效期的投影，由 ``tasks.refresh_promo_status``
    推进（建模板时一律先写"进行中"，见 ``router.create_template``）；
    ``4``（已作废）只由运营的作废端点写入，定时任务不会把它复活。
    ``valid_start`` / ``valid_end`` 为 ``NULL`` 的「领取后 N 天」型没有统一时间窗，
    恒为进行中。
    """

    __tablename__ = "coupon_template"
    __table_args__ = (
        Index("idx_coupon_tpl_status_time", "status", "valid_end"),
        Index("idx_coupon_tpl_shop", "shop_id", "status"),
        CheckConstraint("issued_count <= total_count", name="issued_not_exceed"),
        CheckConstraint("issued_count >= 0 AND used_count >= 0", name="counts_non_negative"),
        {"schema": "promotion", "comment": "优惠券模板"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="0=平台券，>0=店铺券"
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment='如"满200减30"')
    type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1满减 2折扣 3无门槛 4兑换 5运费"
    )
    get_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1主动领取 2系统发放 3兑换码 4活动 5新客"
    )

    # 面额规则
    discount_value: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="满减=减免额(分)；折扣=折扣率(8500=85折)"
    )
    max_discount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="折扣券封顶(分)，0=不限"
    )
    threshold: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="使用门槛(分)，0=无门槛"
    )

    # 发放规则
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="发行总量")
    issued_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="已发放（DB 账本）"
    )
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    per_user_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), comment="每人限领"
    )

    # 有效期
    valid_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1固定区间 2领取后N天"
    )
    valid_start: Mapped[datetime | None] = mapped_column(TS)
    valid_end: Mapped[datetime | None] = mapped_column(TS)
    valid_days: Mapped[int | None] = mapped_column(Integer, comment="领取后有效天数")

    # 适用范围
    scope_type: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("1"),
        comment="1全场 2指定商品 3指定类目 4指定店铺",
    )
    scope_value: Mapped[list[Any] | None] = mapped_column(JSONB, comment="spuId/skuId/categoryId/shopId 数组")
    exclude_value: Mapped[list[Any] | None] = mapped_column(JSONB, comment="排除范围")

    # 叠加规则
    stackable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="可与同层其它优惠叠加"
    )
    exclusive_with_promo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="与活动互斥"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1未开始 2进行中 3已结束 4已作废"
    )

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class CouponCode(Base):
    """用户领取到的券实例。

    同一张券可能被不同的券模板复用（改版后），所以面额/门槛/有效期都**冗余在实例上**
    或在领取时确定 —— 模板改了不影响已发出的券。
    """

    __tablename__ = "coupon_code"
    __table_args__ = (
        UniqueConstraint("code", name="uk_coupon_code"),
        Index("idx_coupon_code_user_status", "user_id", "status", "valid_end"),
        Index("idx_coupon_code_tpl_user", "coupon_template_id", "user_id"),
        Index(
            "idx_coupon_code_locked",
            "locked_order_no",
            postgresql_where=text("locked_order_no IS NOT NULL"),
        ),
        # 过期扫描只扫"未使用"的券，部分索引体积小
        Index(
            "idx_coupon_code_expire",
            "valid_end",
            postgresql_where=text("status = 1"),
        ),
        CheckConstraint("valid_end >= valid_start", name="valid_range"),
        {"schema": "promotion", "comment": "用户优惠券实例"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    coupon_template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("promotion.coupon_template.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False, comment="可读券码，客服核销用")
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="见 CODE_STATUS_TEXT"
    )

    # 冗余自模板，解决"领取后 N 天"
    valid_start: Mapped[datetime] = mapped_column(TS, nullable=False)
    valid_end: Mapped[datetime] = mapped_column(TS, nullable=False)

    locked_order_no: Mapped[str | None] = mapped_column(String(32))
    locked_at: Mapped[datetime | None] = mapped_column(TS)
    used_order_no: Mapped[str | None] = mapped_column(String(32))
    used_at: Mapped[datetime | None] = mapped_column(TS)
    use_amount: Mapped[int | None] = mapped_column(BigInteger, comment="实际抵扣金额(分)")

    source: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1领取 2系统 3兑换 4活动 5退回"
    )
    # 退款退回时，若剩余有效期不足 7 天就延长一次。**最多延长 1 次** ——
    # 否则"下单 → 退款"就能无限刷新券的有效期，是个明摆着的套利口子（docs/08 §4.1）
    extend_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="有效期被延长的次数"
    )
    received_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class CouponUserQuota(Base):
    """用户领取计数。

    ★ 为什么单独一张表：限领的 DB 级保证**不能**直接在流水表上建
    ``(coupon_template_id, user_id)`` 唯一索引 —— 限领 N 张时唯一索引会误拦。
    用计数值 + 条件更新（``WHERE received < per_user_limit``）统一处理限领 1 张和 N 张
    （docs/04 §5.2）。
    """

    __tablename__ = "coupon_user_quota"
    __table_args__ = (
        PrimaryKeyConstraint("coupon_template_id", "user_id", name="pk_coupon_user_quota"),
        {"schema": "promotion"},
    )

    coupon_template_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class CouponReceiveLog(Base):
    """领券流水。用于限领统计与防刷分析。

    ``UNIQUE (idempotency_key)`` 是"重试放大"的 DB 兜底：即使 Redis 幂等键过期，
    DB 也不会因为客户端重试而多发一张。
    """

    __tablename__ = "coupon_receive_log"
    __table_args__ = (
        Index("idx_coupon_receive_tpl_user", "coupon_template_id", "user_id"),
        Index("idx_coupon_receive_ip_time", "ip", "created_at"),
        {"schema": "promotion", "comment": "领券流水"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    coupon_template_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    coupon_code_id: Mapped[int | None] = mapped_column(BigInteger)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("''"))
    ip: Mapped[str | None] = mapped_column(INET)
    device_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), unique=True, comment="客户端重试的幂等键"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class CouponFlow(Base):
    """券状态流水（锁 / 解锁 / 核销 / 退回 / 过期）。

    与 inventory 的 ``stock_flow`` 同一套路：``biz_key`` 是主键的一部分，
    **主键冲突即表示这笔转换已处理过**，天然幂等。
    券的状态变更金额不大但事件频繁，是排查"这张券怎么没了"的唯一依据。
    """

    __tablename__ = "coupon_flow"
    __table_args__ = (
        Index("idx_coupon_flow_code", "coupon_code_id", "created_at"),
        Index("idx_coupon_flow_order", "order_no"),
        {"schema": "promotion"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    coupon_code_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    to_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    order_no: Mapped[str | None] = mapped_column(String(32))
    # ★ Python 侧也要有默认值：只写 server_default 的话，显式传 None 会绕过它
    use_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    biz_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="幂等键，主键冲突即已处理"
    )
    operator: Mapped[str | None] = mapped_column(String(64))
    remark: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class PromoActivity(Base):
    """促销活动。

    覆盖 docs/05 §2 的两类：

    - **单品促销**（``level=0``）：直降 / 折扣 / 特价，作用在单个 SKU 上
    - **订单促销**（``level=1/2``）：满减 / 满折，作用在店铺或平台的符合范围金额上

    ``level`` 决定它参与哪一层计算（docs/05 §3），这是引擎的调度依据。

    ``status`` 由两处维护：建活动时按当时的窗口算一次，之后由
    ``tasks.refresh_promo_status`` 按窗口推进（1 → 2 → 3）。``4``（已作废）
    只能由运营的作废端点写入，且定时任务**不会**把它复活。
    ★ 算价查询要求 ``status = 2``（``repository.list_active_activities``），
      所以这一列就是活动的开关 —— 时间窗是"什么时候自动关"，作废是"现在就关"。
    """

    __tablename__ = "promo_activity"
    __table_args__ = (
        Index("idx_promo_act_status_time", "status", "start_at", "end_at"),
        Index("idx_promo_act_level", "level", "status"),
        Index("idx_promo_act_shop", "shop_id", "status"),
        CheckConstraint("end_at > start_at", name="time_range"),
        CheckConstraint("discount_value >= 0", name="value_non_negative"),
        {"schema": "promotion", "comment": "促销活动"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 0单品 1店铺 2平台。决定参与哪一层计算
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="0单品 1店铺 2平台")
    # 与 DISCOUNT_* 常量对齐，是叠加规则的键
    type: Mapped[str] = mapped_column(String(32), nullable=False, comment="PROMO_ITEM / PROMO_ORDER_SHOP …")
    # 计算方式：1直降(减 discount_value) 2折扣(discount_value 是折扣率) 3特价(固定价)
    calc_type: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="1直降 2折扣 3特价")
    discount_value: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="直降=减免额(分)；折扣=折扣率(8500=85折)；特价=定价(分)"
    )
    max_discount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="封顶(分)，0=不限"
    )
    threshold: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="订单级门槛(分)"
    )
    shop_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="0=平台活动"
    )

    scope_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="同券的 scope_type"
    )
    scope_value: Mapped[list[Any] | None] = mapped_column(JSONB)

    start_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    end_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1未开始 2进行中 3已结束 4已作废"
    )
    # 同一层级内多个活动时的取舍顺序，越大越优先
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class PromoStackRule(Base):
    """优惠叠加规则矩阵（docs/05 §4.1）。

    ★ **数据化，不写死在代码里**。设计原则是**默认互斥、显式声明可叠**：
    新增一种优惠类型时，若忘了配置规则，默认结果是"不能叠加"（安全的），
    而不是"能叠加"（可能导致资损）。

    ``type_a``/``type_b`` 用 ``DISCOUNT_*`` 常量，``shop_id=0`` 表示全局规则。
    """

    __tablename__ = "promo_stack_rule"
    __table_args__ = (
        UniqueConstraint("type_a", "type_b", "shop_id", name="uk_stack_rule_pair"),
        {"schema": "promotion", "comment": "优惠叠加规则矩阵"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_name: Mapped[str] = mapped_column(String(64), nullable=False)
    type_a: Mapped[str] = mapped_column(String(32), nullable=False, comment="优惠类型A")
    type_b: Mapped[str] = mapped_column(String(32), nullable=False, comment="优惠类型B")
    stackable: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="true可叠 false互斥")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    shop_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="0=全局规则"
    )
    effective_from: Mapped[datetime | None] = mapped_column(TS)
    effective_to: Mapped[datetime | None] = mapped_column(TS)


# ============================================================
# 首页 Banner
# ============================================================
BANNER_ENABLED = 1
BANNER_DISABLED = 2

BANNER_STATUS_TEXT: dict[int, str] = {
    BANNER_ENABLED: "启用",
    BANNER_DISABLED: "停用",
}


class Banner(Base):
    """首页轮播图。

    放在 promotion schema 下的理由：它是**运营投放内容**，归属和营销一致，
    管理端点也沿用同一套角色（admin / finance），没必要为一张表新建模块。

    ★ ``image`` 存的是**完整 url**（``/media/banners/<uid>/xxx.webp``），
      与 products / shops / avatars 同一套契约 —— 它直接当 ``<img src>`` 用。

    ★ ``link_url`` **只接受站内路径**（以 ``/`` 开头），空表示这张图不可点。
      外链要处理新窗口、referrer、白名单，当前场景用不到。

    没有"生效时间窗"：没人要预约投放，加了就是给不存在的需求写代码。
    """

    __tablename__ = "banner"
    __table_args__ = (
        # 公开接口的查询就是 WHERE status=1 ORDER BY sort, id，正好吃这个索引
        Index("idx_banner_status_sort", "status", "sort", "id"),
        {"schema": "promotion", "comment": "首页轮播图"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(64), nullable=False, comment="运营看的名字，也当 alt")
    image: Mapped[str] = mapped_column(String(255), nullable=False, comment="完整 url")
    link_url: Mapped[str | None] = mapped_column(String(255), comment="站内路径，空=不可点")
    sort: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1启用 2停用"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
