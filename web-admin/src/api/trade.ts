import { get, post } from './http'

/** 订单状态。与后端 `trade/models.py` 的 STATUS_TEXT 对齐 */
export const ORDER_WAIT_PAY = 10
export const ORDER_WAIT_DELIVER = 20
export const ORDER_WAIT_RECEIVE = 30
export const ORDER_FINISHED = 40
export const ORDER_CLOSED = 50
export const ORDER_REFUNDING = 60
export const ORDER_REFUNDED = 70

/** 商家侧最关心的分栏：**待发货**是唯一需要商家动手的 */
export const MERCHANT_TABS: { label: string; status: number | undefined }[] = [
  { label: '待发货', status: ORDER_WAIT_DELIVER },
  { label: '待收货', status: ORDER_WAIT_RECEIVE },
  { label: '已完成', status: ORDER_FINISHED },
  { label: '全部', status: undefined },
]

/** 状态 → el-tag 的类型。集中一处，避免每个页面各写一套映射 */
export function orderTagType(status: number): 'warning' | 'primary' | 'success' | 'info' {
  switch (status) {
    case ORDER_WAIT_PAY:
      return 'warning'
    case ORDER_WAIT_DELIVER:
    case ORDER_WAIT_RECEIVE:
      return 'primary'
    case ORDER_FINISHED:
      return 'success'
    default:
      return 'info'
  }
}

export interface OrderDelivery {
  deliveryNo: string
  expressCompany: string
  expressNo: string
  status: number
  statusText: string
  deliverTime: string | null
}

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
  /** 本期应结算给商家的金额（分） */
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

export function fetchMerchantOrders(params: {
  status?: number
  cursor?: string
  limit?: number
}): Promise<MerchantOrderList> {
  return get<MerchantOrderList>('/merchant/orders', { params })
}

/**
 * 发货。
 *
 * 只能对**待发货**的子单操作 —— 重复发货会被状态机拒绝（422），
 * 这是刻意的：不能"静默成功"，商家要能知道快递单号没被改掉。
 */
export function shipOrder(
  orderSubNo: string,
  payload: { expressCompany: string; expressNo: string },
): Promise<void> {
  return post<void>(`/merchant/order-subs/${orderSubNo}/ship`, payload)
}
