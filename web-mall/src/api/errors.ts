/**
 * 后端错误码镜像。
 *
 * ★ 必须与 `backend/app/core/errors.py` 的 `ErrorCode` 逐条对齐。
 *   后端加/改错误码时，这里要同步改，CI 会加一条比对检查
 *   （docs/15-api-and-errors.md §3.1）。
 *
 * 用字符串常量而不是数字：日志里一眼可读，也不会和 HTTP 状态码混淆。
 */
export const ErrorCode = {
  OK: 'OK',

  // ---- 通用 ----
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  IDEMPOTENCY_KEY_REQUIRED: 'IDEMPOTENCY_KEY_REQUIRED',
  UNAUTHORIZED: 'UNAUTHORIZED',
  LOGIN_FAILED: 'LOGIN_FAILED',
  ACCOUNT_LOCKED: 'ACCOUNT_LOCKED',
  FORBIDDEN: 'FORBIDDEN',
  NOT_FOUND: 'NOT_FOUND',
  METHOD_NOT_ALLOWED: 'METHOD_NOT_ALLOWED',
  RATE_LIMITED: 'RATE_LIMITED',
  INTERNAL_ERROR: 'INTERNAL_ERROR',
  SYSTEM_BUSY: 'SYSTEM_BUSY',

  // ---- 幂等 ----
  REQUEST_PROCESSING: 'REQUEST_PROCESSING',

  // ---- 价格一致性 ----
  PRICE_CHANGED: 'PRICE_CHANGED',
  PRICE_TOKEN_EXPIRED: 'PRICE_TOKEN_EXPIRED',
  INVALID_PRICE_TOKEN: 'INVALID_PRICE_TOKEN',

  // ---- 库存 ----
  STOCK_INSUFFICIENT: 'STOCK_INSUFFICIENT',
  STOCK_SOLD_OUT: 'STOCK_SOLD_OUT',
  SECKILL_ENDED: 'SECKILL_ENDED',
  PURCHASE_LIMIT_EXCEEDED: 'PURCHASE_LIMIT_EXCEEDED',

  // ---- 排队 ----
  SECKILL_QUEUED: 'SECKILL_QUEUED',
  ALREADY_PURCHASED: 'ALREADY_PURCHASED',
  QUEUE_FULL: 'QUEUE_FULL',

  // ---- 优惠券与活动 ----
  COUPON_SOLD_OUT: 'COUPON_SOLD_OUT',
  COUPON_LIMIT_EXCEEDED: 'COUPON_LIMIT_EXCEEDED',
  COUPON_EXPIRED: 'COUPON_EXPIRED',
  COUPON_THRESHOLD_NOT_MET: 'COUPON_THRESHOLD_NOT_MET',
  COUPON_LOCKED: 'COUPON_LOCKED',
  COUPON_STACK_CONFLICT: 'COUPON_STACK_CONFLICT',
  ACTIVITY_NOT_STARTED: 'ACTIVITY_NOT_STARTED',
  ACTIVITY_ENDED: 'ACTIVITY_ENDED',

  // ---- 订单 ----
  ORDER_STATUS_INVALID: 'ORDER_STATUS_INVALID',
  ORDER_CLOSED: 'ORDER_CLOSED',
  ORDER_ALREADY_PAID: 'ORDER_ALREADY_PAID',
  ORDER_ALREADY_SHIPPED: 'ORDER_ALREADY_SHIPPED',
  NOT_DELIVERABLE: 'NOT_DELIVERABLE',
  SKU_OFF_SHELF: 'SKU_OFF_SHELF',
  ORDER_ITEM_NOT_FOUND: 'ORDER_ITEM_NOT_FOUND',

  // ---- 支付 ----
  PAYMENT_CLOSED: 'PAYMENT_CLOSED',
  PAY_AMOUNT_MISMATCH: 'PAY_AMOUNT_MISMATCH',
  PAY_CHANNEL_UNAVAILABLE: 'PAY_CHANNEL_UNAVAILABLE',

  // ---- 售后 ----
  AFTERSALE_EXPIRED: 'AFTERSALE_EXPIRED',
  AFTERSALE_STATUS_INVALID: 'AFTERSALE_STATUS_INVALID',
  REFUND_NUM_EXCEED: 'REFUND_NUM_EXCEED',
  REFUND_AMOUNT_EXCEED: 'REFUND_AMOUNT_EXCEED',
  AFTERSALE_IN_PROGRESS: 'AFTERSALE_IN_PROGRESS',
  NO_REASON_RETURN_UNSUPPORTED: 'NO_REASON_RETURN_UNSUPPORTED',

  // ---- 评价 ----
  NOT_RECEIVED: 'NOT_RECEIVED',
  ORDER_NOT_FINISHED: 'ORDER_NOT_FINISHED',
  ALREADY_REVIEWED: 'ALREADY_REVIEWED',
  REVIEW_EXPIRED: 'REVIEW_EXPIRED',
  ALREADY_FOLLOWED_UP: 'ALREADY_FOLLOWED_UP',
  ITEM_REFUNDED: 'ITEM_REFUNDED',
  IN_AFTERSALE: 'IN_AFTERSALE',
  REPLY_LIMIT_EXCEEDED: 'REPLY_LIMIT_EXCEEDED',
  REVIEW_STATUS_INVALID: 'REVIEW_STATUS_INVALID',

  // ---- 文件上传 ----
  INVALID_IMAGE: 'INVALID_IMAGE',
  IMAGE_TOO_LARGE: 'IMAGE_TOO_LARGE',

  // ---- 仅前端使用的本地错误码 ----
  NETWORK_ERROR: 'NETWORK_ERROR',
  TIMEOUT: 'TIMEOUT',
} as const

export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode]

/** 后端统一响应体 */
export interface ApiResponse<T> {
  code: string
  message: string
  data: T | null
  requestId: string
}

/** 业务异常：带上错误码与 requestId，方便用户报障时定位日志 */
export class BizError extends Error {
  readonly code: string
  readonly data: unknown
  readonly requestId: string

  constructor(body: { code: string; message: string; data?: unknown; requestId?: string }) {
    super(body.message)
    this.name = 'BizError'
    this.code = body.code
    this.data = body.data
    this.requestId = body.requestId ?? ''
    Object.setPrototypeOf(this, BizError.prototype)
  }
}

/** 用法：`isBizError(e, ErrorCode.REQUEST_PROCESSING)` 或只判是不是业务异常 */
export function isBizError(e: unknown, code?: string): e is BizError {
  if (!(e instanceof BizError)) return false
  return code === undefined || e.code === code
}
