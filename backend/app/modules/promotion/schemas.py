"""promotion 模块的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.core.schemas import CamelModel, Quantity, SnowflakeId


# ============================================================
# 券：对外视图
# ============================================================
class CouponTemplateOut(CamelModel):
    """券模板的展示视图。字段名与业务含义对齐，前端不必再算。"""

    id: SnowflakeId
    shop_id: SnowflakeId
    name: str
    type: int
    discount_value: int
    max_discount: int
    threshold: int
    per_user_limit: int
    valid_start: datetime | None
    valid_end: datetime | None
    valid_days: int | None
    scope_type: int
    status: int


class ReceivableCouponOut(CamelModel):
    """券中心的一项：模板 + 当前用户已领几张。"""

    template: CouponTemplateOut
    received: int = Field(description="当前用户已领张数")
    can_receive: bool
    remain: int = Field(description="剩余可发量")


class MyCouponOut(CamelModel):
    id: SnowflakeId
    code: str
    status: int
    status_text: str
    valid_start: datetime
    valid_end: datetime
    # 已过期（按当前时间判断，与 status 可能不同步）
    expired: bool
    # 券的规则从模板带过来，前端展示"满100减20"这类文案要用
    name: str
    threshold: int
    discount_value: int
    max_discount: int
    coupon_type: int
    shop_id: SnowflakeId


class CouponReceiveOut(CamelModel):
    id: SnowflakeId
    code: str
    name: str
    valid_start: datetime
    valid_end: datetime


# ============================================================
# 运营视图
# ============================================================
class AdminCouponTemplateOut(CamelModel):
    """运营看的券模板。

    比 :class:`CouponTemplateOut` 多出**发行量、已发量、适用范围、获取方式、
    有效期类型** —— 运营要判断"还剩多少可发""这是固定区间还是领取后 N 天"，
    买家侧用不到这些。旧出参恰好一项都没给，所以另开一个模型而不是去改它
    （改了会动到券中心的响应契约）。
    """

    id: SnowflakeId
    shop_id: SnowflakeId
    name: str
    type: int
    type_text: str
    get_type: int
    discount_value: int
    max_discount: int
    threshold: int
    total_count: int
    issued_count: int
    used_count: int
    per_user_limit: int
    valid_type: int
    valid_start: datetime | None
    valid_end: datetime | None
    valid_days: int | None
    scope_type: int
    # ★ 是**字符串数组**：scope_value 里存的是雪花 ID，超过 2^53，
    #   序列化成 JSON number 会让前端（JS 只有 double）静默丢精度。
    scope_value: list[str] | None
    status: int
    status_text: str
    created_at: datetime


class AdminCouponTemplateListOut(CamelModel):
    items: list[AdminCouponTemplateOut]
    next_cursor: str | None = None
    has_more: bool = False


class AdminPromoActivityOut(CamelModel):
    id: SnowflakeId
    name: str
    level: int
    level_text: str
    # 与叠加规则矩阵对齐的字符串键（PROMO_ITEM 等），不是数字
    type: str
    type_text: str
    calc_type: int
    calc_type_text: str
    discount_value: int
    max_discount: int
    threshold: int
    shop_id: SnowflakeId
    scope_type: int
    scope_value: list[str] | None
    start_at: datetime
    end_at: datetime
    priority: int
    status: int
    status_text: str
    created_at: datetime


class AdminPromoActivityListOut(CamelModel):
    items: list[AdminPromoActivityOut]
    next_cursor: str | None = None
    has_more: bool = False


# ============================================================
# 算价
# ============================================================
class CalcItemIn(CamelModel):
    sku_id: SnowflakeId
    num: Quantity = 1


class CalcPriceRequest(CamelModel):
    items: list[CalcItemIn] = Field(min_length=1, max_length=100)
    coupon_code_ids: list[SnowflakeId] = Field(default_factory=list, max_length=10)
    # 运费模块还没实现，这个字段先留着但当前不参与计算
    address_id: SnowflakeId | None = None


class CalcAllocationOut(CamelModel):
    """某条优惠分摊到这一行的金额。退款时按这个算每行退多少。"""

    source_type: str
    source_name: str
    amount: int


class CalcItemOut(CamelModel):
    sku_id: SnowflakeId
    spu_id: SnowflakeId = Field(default=0, description="用来跳商品详情页")
    shop_id: SnowflakeId = Field(default=0, description="结算页按店铺分组")
    title: str
    spec_text: str
    cover_image: str

    num: int
    unit_price: int = Field(description="原价（分）")
    promo_price: int = Field(description="单品促销后的单价（分）")
    weight_g: int = Field(default=0, description="下单时快照用，也是续重规则的提示依据")
    amount: int = Field(description="原价小计 = unitPrice × num")
    discount_amount: int = Field(description="本行承担的总优惠（分）")
    payable_amount: int = Field(description="本行实付 = amount - discountAmount")
    allocations: list[CalcAllocationOut] = Field(default_factory=list)


class CalcDiscountOut(CamelModel):
    level: int
    source_type: str
    source_id: SnowflakeId = Field(
        default=0, description="来源 id：券是 couponCode.id，活动是 promoActivity.id"
    )
    source_name: str
    amount: int


class UnavailableCouponOut(CamelModel):
    code_id: SnowflakeId
    name: str
    reason: str
    reason_text: str = Field(description='给用户看的原因，如"还差 ¥12.00 可用"')


class CalcPriceOut(CamelModel):
    items: list[CalcItemOut]
    discounts: list[CalcDiscountOut]

    total_amount: int = Field(description="商品总额（原价，分）")
    item_discount: int = Field(description="单品促销优惠")
    shop_discount: int = Field(description="店铺级优惠")
    platform_discount: int = Field(description="平台级优惠")
    point_deduction: int = Field(default=0, description="积分抵扣。本期未实现，恒为 0")
    freight: int = Field(default=0, description="运费。本期未实现，恒为 0")
    payable_amount: int = Field(description="应付 = 总额 - 各级优惠 + 运费")

    unavailable_coupons: list[UnavailableCouponOut] = Field(default_factory=list)

    # 让前端明确知道哪些优惠没参与计算，不要以为算错了
    notices: list[str] = Field(default_factory=list)


# ============================================================
# 运营端
# ============================================================
class CouponTemplateCreateRequest(CamelModel):
    name: str = Field(min_length=2, max_length=64)
    shop_id: SnowflakeId = 0
    type: int = Field(default=1, ge=1, le=5, description="1满减 2折扣 3无门槛")
    discount_value: int = Field(ge=1, description="满减=减免额(分)；折扣=折扣率(8500=85折)")
    max_discount: int = Field(default=0, ge=0)
    threshold: int = Field(default=0, ge=0)
    total_count: int = Field(ge=1, le=1_000_000)
    per_user_limit: int = Field(default=1, ge=1, le=100)
    valid_type: int = Field(default=1, description="1固定区间 2领取后N天")
    valid_start: datetime | None = None
    valid_end: datetime | None = None
    valid_days: int | None = Field(default=None, ge=1, le=365)
    scope_type: int = Field(default=1, ge=1, le=4)
    scope_value: list[int] | None = None


class AdminIssueRequest(CamelModel):
    """客服补发。**不占活动额度**（docs/04 §11）。"""

    template_id: SnowflakeId
    user_id: SnowflakeId
    count: int = Field(default=1, ge=1, le=100)


class UserLookupOut(CamelModel):
    """按手机号定位到的用户 —— 定向发券前用来让运营确认"发给谁"。

    只含定位与确认所需的最少字段：运营手上有的是**手机号**，需要换回 ``userId``
    才能发券；昵称与打码号用来核对没找错人。
    """

    user_id: SnowflakeId
    nickname: str
    phone_masked: str


# ---------------- 客服补发的记录与配额 ----------------
class CouponIssueOperatorOut(CamelModel):
    """补发汇总里的一个操作人。"""

    operator_name: str
    count: int


class CouponIssueRecordOut(CamelModel):
    """一条补发记录。

    **一行 = 一张券** —— 审计就该到这个粒度：出了问题能顺着 ``code``
    查到具体那一张，而不是只知道"某人某天发了一批"。
    """

    id: SnowflakeId
    created_at: datetime
    template_name: str
    # 券码：出问题时能顺着它去查这张券的完整流水
    code: str
    # 操作人：解析不出昵称时退回原始标识（如 admin:123），不显示空白
    operator_name: str
    # 收件人
    user_id: SnowflakeId
    nickname: str
    phone_masked: str
    remark: str | None = None


class CouponIssueSummaryOut(CamelModel):
    """最近 24 小时的补发汇总 —— 一眼看出发了多少、谁发得多。"""

    total: int
    by_operator: list[CouponIssueOperatorOut]


class CouponIssuePageOut(CamelModel):
    items: list[CouponIssueRecordOut]
    has_more: bool
    next_cursor: str | None = None
    summary: CouponIssueSummaryOut


class PromoActivityCreateRequest(CamelModel):
    name: str = Field(min_length=2, max_length=64)
    level: int = Field(ge=0, le=2, description="0单品 1店铺 2平台")
    type: str = Field(max_length=32, description="PROMO_ITEM / PROMO_ORDER_SHOP / PROMO_ORDER_PLATFORM")
    calc_type: int = Field(ge=1, le=3, description="1直降 2折扣 3特价")
    discount_value: int = Field(ge=0)
    max_discount: int = Field(default=0, ge=0)
    threshold: int = Field(default=0, ge=0)
    shop_id: SnowflakeId = 0
    scope_type: int = Field(default=1, ge=1, le=4)
    scope_value: list[int] | None = None
    start_at: datetime
    end_at: datetime
    priority: int = 0


# ============================================================
# 首页 Banner
# ============================================================
class BannerOut(CamelModel):
    """轮播图。公开接口与管理端共用 —— 字段就这么几个，没必要维护两套。"""

    id: SnowflakeId
    title: str
    image: str
    link_url: str | None = None
    sort: int = 0
    status: int = 1


class BannerCreateRequest(CamelModel):
    title: str = Field(min_length=1, max_length=64, description="运营看的名字，也当图片 alt")
    image: str = Field(min_length=1, max_length=255, description="完整 url")
    # ★ 只接受**站内路径**（或以空串清空）。外链要处理新窗口、referrer、白名单，
    #   当前场景用不到；不限制的话运营填个 https:// 就会在前端变成死链。
    link_url: str | None = Field(default=None, max_length=255, pattern=r"^(/.*)?$")
    sort: int = 0


class BannerUpdateRequest(CamelModel):
    """部分更新：只写传了的字段（``link_url`` 传空串表示清空）。"""

    title: str | None = Field(default=None, min_length=1, max_length=64)
    image: str | None = Field(default=None, min_length=1, max_length=255)
    link_url: str | None = Field(default=None, max_length=255, pattern=r"^(/.*)?$")
    sort: int | None = None
    status: int | None = Field(default=None, ge=1, le=2)
