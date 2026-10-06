import { get, post } from './http'

/** 券模板。字段与后端 `promotion/schemas.py` 一一对应，金额单位是分。 */
export interface CouponTemplate {
  id: string
  shopId: string
  name: string
  /** 1满减 2折扣 3无门槛 */
  type: number
  /** 满减=减免额；折扣=折扣率（8500 表示 85 折） */
  discountValue: number
  /** 折扣券封顶（分），0 不限 */
  maxDiscount: number
  /** 使用门槛（分） */
  threshold: number
  perUserLimit: number
  validStart: string | null
  validEnd: string | null
  validDays: number | null
  scopeType: number
  status: number
}

export interface ReceivableCoupon {
  template: CouponTemplate
  received: number
  canReceive: boolean
  remain: number
}

export interface MyCoupon {
  id: string
  code: string
  status: number
  statusText: string
  validStart: string
  validEnd: string
  /** 按当前时间判断，可能与 status 不同步 */
  expired: boolean
  name: string
  threshold: number
  discountValue: number
  maxDiscount: number
  couponType: number
  shopId: string
}

export interface CouponReceiveResult {
  id: string
  code: string
  name: string
  validStart: string
  validEnd: string
}

export interface CalcAllocation {
  sourceType: string
  sourceName: string
  amount: number
}

export interface CalcItem {
  skuId: string
  spuId: string
  shopId: string
  title: string
  specText: string
  coverImage: string
  num: number
  unitPrice: number
  promoPrice: number
  weightG: number
  amount: number
  discountAmount: number
  payableAmount: number
  allocations: CalcAllocation[]
}

export interface CalcDiscount {
  level: number
  sourceType: string
  sourceId: string
  sourceName: string
  amount: number
}

export interface UnavailableCoupon {
  codeId: string
  name: string
  reason: string
  /** 给用户看的原因，THRESHOLD_NOT_MET 会带"还差 ¥X" */
  reasonText: string
}

export interface CalcPriceResult {
  items: CalcItem[]
  discounts: CalcDiscount[]
  totalAmount: number
  itemDiscount: number
  shopDiscount: number
  platformDiscount: number
  pointDeduction: number
  freight: number
  payableAmount: number
  unavailableCoupons: UnavailableCoupon[]
  notices: string[]
  /**
   * 能不能提交下单。**按仓**判定：某个子单的货没有哪个仓库能一次发齐时为 false
   * （理由在 `notices` 里）。缺货**不会**让算价本身失败 —— 用户可能只是想看看多少钱。
   */
  canSubmit: boolean
}

export function fetchAvailableCoupons(): Promise<ReceivableCoupon[]> {
  return get<ReceivableCoupon[]>('/coupons/available')
}

/**
 * 领券。
 *
 * ★ 必须带 `Idempotency-Key`：后端缺这个头会直接返回 400，
 *   而且它是"用户点了一次、网络卡住、浏览器重试"时唯一的防重复手段。
 */
export function receiveCoupon(
  templateId: string,
  idempotencyKey: string,
): Promise<CouponReceiveResult> {
  return post<CouponReceiveResult>(
    `/coupons/${templateId}/receive`,
    undefined,
    { headers: { 'Idempotency-Key': idempotencyKey } },
  )
}

export function fetchMyCoupons(status?: number): Promise<MyCoupon[]> {
  return get<MyCoupon[]>('/my/coupons', { params: status === undefined ? {} : { status } })
}

/**
 * 算价。
 *
 * ★ 必须带 `addressId`：运费按收货地区算，不传地址运费就只能是 0，
 *   用户会以为包邮、到下单才发现多出一笔（docs/06 §4.1）。
 */
export function calcPrice(payload: {
  items: { skuId: string; num: number }[]
  couponCodeIds?: string[]
  addressId?: string
}): Promise<CalcPriceResult> {
  return post<CalcPriceResult>('/checkout/calc', payload)
}

/** 券状态常量，与后端 `promotion/models.py` 对齐 */
export const CODE_UNUSED = 1
export const CODE_LOCKED = 2
export const CODE_USED = 3
export const CODE_EXPIRED = 4
export const CODE_VOID = 5
