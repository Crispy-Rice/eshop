"""错误码与业务异常。

错误码是**语义化字符串**（不是数字），每个码绑定一个 HTTP 状态码和默认文案，
见 docs/15-api-and-errors.md §3。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """业务错误码。

    成员定义成 ``(code, http_status, default_message)`` 三元组，
    ``code`` 只写一次，就用成员名本身，避免两处不一致。
    """

    def __new__(cls, value: str, http_status: int, message: str) -> ErrorCode:
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.http_status = http_status
        obj.default_message = message
        return obj

    http_status: int
    default_message: str

    # ---------- 通用 ----------
    VALIDATION_ERROR = ("VALIDATION_ERROR", 400, "参数错误")
    IDEMPOTENCY_KEY_REQUIRED = ("IDEMPOTENCY_KEY_REQUIRED", 400, "缺少 Idempotency-Key 请求头")
    UNAUTHORIZED = ("UNAUTHORIZED", 401, "未登录或登录已过期")
    LOGIN_FAILED = ("LOGIN_FAILED", 401, "账号或密码错误")
    ACCOUNT_LOCKED = ("ACCOUNT_LOCKED", 423, "登录失败次数过多，账号已临时锁定")
    FORBIDDEN = ("FORBIDDEN", 403, "无权执行该操作")
    NOT_FOUND = ("NOT_FOUND", 404, "资源不存在")
    METHOD_NOT_ALLOWED = ("METHOD_NOT_ALLOWED", 405, "请求方法不被支持")
    RATE_LIMITED = ("RATE_LIMITED", 429, "请求过于频繁，请稍后再试")
    INTERNAL_ERROR = ("INTERNAL_ERROR", 500, "系统繁忙，请稍后再试")
    SYSTEM_BUSY = ("SYSTEM_BUSY", 503, "当前访问人数过多，请稍后再试")

    # ---------- 幂等 ----------
    REQUEST_PROCESSING = ("REQUEST_PROCESSING", 409, "请求处理中，请稍候")

    # ---------- 价格一致性 ----------
    PRICE_CHANGED = ("PRICE_CHANGED", 409, "商品信息发生变化，请确认后重新提交")
    PRICE_TOKEN_EXPIRED = ("PRICE_TOKEN_EXPIRED", 409, "页面已过期，请刷新后重试")
    INVALID_PRICE_TOKEN = ("INVALID_PRICE_TOKEN", 400, "价格校验失败")

    # ---------- 库存 ----------
    STOCK_INSUFFICIENT = ("STOCK_INSUFFICIENT", 410, "商品库存不足")
    STOCK_SOLD_OUT = ("STOCK_SOLD_OUT", 410, "商品已售罄")
    SECKILL_ENDED = ("SECKILL_ENDED", 410, "秒杀已结束")
    PURCHASE_LIMIT_EXCEEDED = ("PURCHASE_LIMIT_EXCEEDED", 422, "超出限购数量")

    # ---------- 排队 ----------
    SECKILL_QUEUED = ("SECKILL_QUEUED", 202, "排队中")
    ALREADY_PURCHASED = ("ALREADY_PURCHASED", 422, "您已参与过该活动")
    QUEUE_FULL = ("QUEUE_FULL", 429, "排队人数已满，请稍后再试")

    # ---------- 优惠券与活动 ----------
    COUPON_SOLD_OUT = ("COUPON_SOLD_OUT", 410, "优惠券已被抢光")
    COUPON_LIMIT_EXCEEDED = ("COUPON_LIMIT_EXCEEDED", 422, "超出每人限领数量")
    COUPON_EXPIRED = ("COUPON_EXPIRED", 422, "优惠券已过期")
    COUPON_THRESHOLD_NOT_MET = ("COUPON_THRESHOLD_NOT_MET", 422, "未达到优惠券使用门槛")
    COUPON_LOCKED = ("COUPON_LOCKED", 422, "优惠券已被其他订单占用")
    COUPON_STACK_CONFLICT = ("COUPON_STACK_CONFLICT", 422, "该优惠与已选优惠互斥")
    ACTIVITY_NOT_STARTED = ("ACTIVITY_NOT_STARTED", 422, "活动尚未开始")
    ACTIVITY_ENDED = ("ACTIVITY_ENDED", 422, "活动已结束")

    # ---------- 订单 ----------
    ORDER_STATUS_INVALID = ("ORDER_STATUS_INVALID", 422, "当前订单状态不允许该操作")
    ORDER_CLOSED = ("ORDER_CLOSED", 422, "订单已关闭")
    ORDER_ALREADY_PAID = ("ORDER_ALREADY_PAID", 422, "订单已支付")
    ORDER_ALREADY_SHIPPED = ("ORDER_ALREADY_SHIPPED", 422, "订单已发货，无法取消")
    NOT_DELIVERABLE = ("NOT_DELIVERABLE", 422, "该地区暂不支持配送")
    SKU_OFF_SHELF = ("SKU_OFF_SHELF", 422, "商品已下架")
    ORDER_ITEM_NOT_FOUND = ("ORDER_ITEM_NOT_FOUND", 404, "订单不存在")

    # ---------- 支付 ----------
    PAYMENT_CLOSED = ("PAYMENT_CLOSED", 422, "支付单已关闭")
    PAY_AMOUNT_MISMATCH = ("PAY_AMOUNT_MISMATCH", 422, "支付金额异常")
    PAY_CHANNEL_UNAVAILABLE = ("PAY_CHANNEL_UNAVAILABLE", 503, "支付渠道维护中，请稍后再试")

    # ---------- 售后 ----------
    AFTERSALE_EXPIRED = ("AFTERSALE_EXPIRED", 422, "已超过售后申请期限")
    REFUND_NUM_EXCEED = ("REFUND_NUM_EXCEED", 422, "退货数量超过可退数量")
    REFUND_AMOUNT_EXCEED = ("REFUND_AMOUNT_EXCEED", 422, "退款金额超过实付金额")
    AFTERSALE_IN_PROGRESS = ("AFTERSALE_IN_PROGRESS", 422, "该订单已有进行中的售后")
    NO_REASON_RETURN_UNSUPPORTED = ("NO_REASON_RETURN_UNSUPPORTED", 422, "该商品不支持七天无理由退货")

    # ---------- 评价 ----------
    NOT_RECEIVED = ("NOT_RECEIVED", 422, "确认收货后才能评价")
    ALREADY_REVIEWED = ("ALREADY_REVIEWED", 422, "该商品已评价")
    REVIEW_EXPIRED = ("REVIEW_EXPIRED", 422, "评价期限已过")
    ALREADY_FOLLOWED_UP = ("ALREADY_FOLLOWED_UP", 422, "该评价已追评过")
    ITEM_REFUNDED = ("ITEM_REFUNDED", 422, "该商品已退款，无法评价")
    IN_AFTERSALE = ("IN_AFTERSALE", 422, "售后处理中，暂不能评价")


class BizError(Exception):
    """可预期的业务异常。由全局异常处理器转成统一响应，不记堆栈。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        data: Any = None,
    ) -> None:
        self.code = code
        self.message = message or code.default_message
        self.data = data
        super().__init__(self.message)

    @property
    def http_status(self) -> int:
        return self.code.http_status


class PriceChangedError(BizError):
    """下单时前后端算价不一致。

    异常里带上最新的算价结果与新 priceToken，前端可以直接弹窗让用户确认，
    详见 docs/11-price-consistency.md §4.4。
    """

    def __init__(self, diffs: list[Any], new_price_token: str | None = None) -> None:
        super().__init__(
            ErrorCode.PRICE_CHANGED,
            data={"diffs": diffs, "newPriceToken": new_price_token},
        )
        self.diffs = diffs
        self.new_price_token = new_price_token


class PriceInvariantError(BizError):
    """金额恒等式被破坏。属于 Bug，必须告警，绝不能让不平的数据落库。

    注意：用显式抛异常而不是 assert —— ``python -O`` 会把 assert 全部移除。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, f"金额校验失败：{detail}")


class CachedResponse(Exception):
    """幂等命中：带着上次的响应体，由异常处理器直接 200 返回。"""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        super().__init__("idempotency hit")
