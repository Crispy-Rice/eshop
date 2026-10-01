import { get, post } from './http'

/** 订单状态。与后端 `trade/models.py` 的 STATUS_TEXT 对齐 */
export const ORDER_WAIT_PAY = 10
export const ORDER_WAIT_DELIVER = 20
export const ORDER_WAIT_RECEIVE = 30
export const ORDER_FINISHED = 40
export const ORDER_CLOSED = 50
export const ORDER_REFUNDING = 60
export const ORDER_REFUNDED = 70

/** 支付单状态。与后端 `payment/models.py` 对齐 */
export const PAY_WAIT = 0
export const PAY_SUCCESS = 1
export const PAY_CLOSED = 2
export const PAY_REFUNDED = 3

/**
 * 状态 → 视觉色调。
 *
 * 统一在这里定，避免每个页面各写一套映射导致同一个状态在不同页面颜色不同。
 * 待付款用警示色（**要用户做事**），已完成用成功色，已关闭/已退款用中性色。
 */
export type StatusTone = 'warn' | 'accent' | 'success' | 'muted'

export function orderStatusTone(status: number): StatusTone {
  switch (status) {
    case ORDER_WAIT_PAY:
      return 'warn'
    case ORDER_WAIT_DELIVER:
    case ORDER_WAIT_RECEIVE:
      return 'accent'
    case ORDER_FINISHED:
      return 'success'
    default:
      return 'muted'
  }
}

export interface OrderItem {
  skuId: string
  spuId: string
  title: string
  specText: string
  coverImage: string
  /** 下单时的单价（分）—— 商家现在改价也不影响历史订单 */
  unitPrice: number
  num: number
  itemAmount: number
  discountAmount: number
  payableAmount: number
}

export interface OrderDelivery {
  deliveryNo: string
  expressCompany: string
  expressNo: string
  status: number
  statusText: string
  deliverTime: string | null
}

export interface OrderSub {
  orderSubNo: string
  shopId: string
  shopName: string
  status: number
  statusText: string
  deliveryStatus: number
  deliveryStatusText: string
  totalAmount: number
  discountAmount: number
  freightAmount: number
  payableAmount: number
  deliverTime: string | null
  receiveTime: string | null
  createTime: string
  canAftersale: boolean
  items: OrderItem[]
  deliveries: OrderDelivery[]
}

export interface OrderMain {
  orderMainNo: string
  status: number
  statusText: string
  payStatus: number
  payStatusText: string

  shopCount: number
  totalAmount: number
  discountAmount: number
  freightAmount: number
  payableAmount: number
  paidAmount: number
  couponAmount: number
  pointDeduction: number

  receiverName: string
  receiverPhone: string
  receiverProvince: string
  receiverCity: string
  receiverDistrict: string
  receiverDetail: string

  buyerRemark: string | null
  freightDetail: { total?: number; notices?: string[] }

  createTime: string
  payDeadline: string
  payTime: string | null
  finishTime: string | null

  /** 剩余支付秒数，<= 0 表示已超时。**由服务端算**，前端直接拿来倒计时 */
  payRemainSeconds: number
  canCancel: boolean
  canPay: boolean
  canAftersale: boolean

  subs: OrderSub[]
}

export interface OrderListItem {
  orderMainNo: string
  status: number
  statusText: string
  payStatus: number
  payStatusText: string

  payableAmount: number
  totalAmount: number

  shopCount: number
  itemKindCount: number
  totalNum: number
  previewTitles: string[]
  previewImages: string[]

  createTime: string
  payDeadline: string
  payRemainSeconds: number
  canCancel: boolean
  canPay: boolean
}

export interface OrderList {
  items: OrderListItem[]
  nextCursor: string | null
  hasMore: boolean
}

export interface Payment {
  payNo: string
  orderMainNo: string
  amount: number
  paidAmount: number
  channel: string
  status: number
  statusText: string
  payTime: string | null
  createdAt: string
  /** 模拟支付的入口。仅 mock 模式返回 */
  mockPayUrl: string | null
}

/** 商家看到的子单。字段与后端 `MerchantOrderOut` 对齐 */
export interface MerchantOrder {
  orderSubNo: string
  orderMainNo: string
  status: number
  statusText: string
  deliveryStatus: number
  deliveryStatusText: string

  shopId: string
  buyerName: string
  buyerPhone: string

  totalAmount: number
  discountAmount: number
  freightAmount: number
  payableAmount: number

  itemKindCount: number
  totalNum: number
  previewTitles: string[]
  previewImages: string[]

  createTime: string
  payTime: string | null
  deliverTime: string | null
  receiverFull: string

  canShip: boolean
  deliveries: OrderDelivery[]
}

export interface MerchantOrderList {
  items: MerchantOrder[]
  nextCursor: string | null
  hasMore: boolean
}

// ============================================================
// 买家端
// ============================================================
/**
 * 下单。
 *
 * ★ 必须带 `Idempotency-Key`：后端缺这个头直接 400，而且它是
 *   "用户连点两次提交" 时唯一的防重复手段（会下出两个订单、扣两次库存）。
 */
export function createOrder(
  payload: {
    items: { skuId: string; num: number }[]
    addressId: string
    couponCodeIds?: string[]
    buyerRemark?: string
  },
  idempotencyKey: string,
): Promise<OrderMain> {
  return post<OrderMain>('/orders', payload, {
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

export function fetchOrders(params: {
  status?: number
  cursor?: string
  limit?: number
}): Promise<OrderList> {
  return get<OrderList>('/orders', { params })
}

export function fetchOrder(orderMainNo: string): Promise<OrderMain> {
  return get<OrderMain>(`/orders/${orderMainNo}`)
}

/** 取消订单。**幂等** —— 重复调用不报错，前端刷新重试可以放心重发 */
export function cancelOrder(orderMainNo: string): Promise<void> {
  return post<void>(`/orders/${orderMainNo}/cancel`)
}

/** 确认收货。按**子单**确认 —— 跨店订单可能有的店还没发货 */
export function receiveOrder(orderSubNo: string): Promise<void> {
  return post<void>(`/order-subs/${orderSubNo}/receive`)
}

// ============================================================
// 支付
// ============================================================
/** 发起支付。幂等：同一母单重复调用返回同一张支付单 */
export function createPayment(orderMainNo: string): Promise<Payment> {
  return post<Payment>('/payments', { orderMainNo })
}

/**
 * 模拟渠道回调（**演示用**）。
 *
 * 真实渠道里这一步是微信/支付宝的服务器回调我们的接口；模拟环境下
 * 由前端的"确认支付"按钮直接触发，把它当成"用户在收银台点了我已付款"。
 */
export function mockPayCallback(payNo: string): Promise<Payment> {
  return post<Payment>(`/payments/${payNo}/mock-callback`, {})
}

export function fetchPayment(payNo: string): Promise<Payment> {
  return get<Payment>(`/payments/${payNo}`)
}
