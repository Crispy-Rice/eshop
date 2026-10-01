import { get, post } from './http'

/** 售后单状态。与后端 `core/enums.py` 的 RefundStatus 对齐 */
export const REFUND_APPLYING = 10
export const REFUND_MERCHANT_REJECTED = 11
export const REFUND_WAIT_REFUND = 20
export const REFUND_WAIT_RETURN = 30
export const REFUND_WAIT_RECEIVE = 40
export const REFUND_QUALITY_CHECKING = 50
export const REFUND_QUALITY_FAILED = 51
export const REFUND_REFUNDING = 60
export const REFUND_SUCCESS = 70
export const REFUND_CLOSED = 80
export const REFUND_USER_REVOKED = 81

/**
 * 商家侧最关心的分栏。
 *
 * **待处理**是三个状态的并集（待审核 / 待收货 / 质检中）—— 这三件事都要商家动手，
 * 分开成三个 tab 反而要来回点。
 */
export const MERCHANT_TABS: { label: string; status?: number; pendingOnly?: boolean }[] = [
  { label: '待我处理', pendingOnly: true },
  { label: '待买家寄回', status: REFUND_WAIT_RETURN },
  { label: '已退款', status: REFUND_SUCCESS },
  { label: '全部' },
]

/** 状态 → el-tag 类型。集中一处，避免每个页面各写一套映射 */
export function refundTagType(
  status: number,
): 'warning' | 'primary' | 'success' | 'info' {
  switch (status) {
    case REFUND_APPLYING:
    case REFUND_WAIT_RETURN:
    case REFUND_WAIT_RECEIVE:
    case REFUND_QUALITY_CHECKING:
      return 'warning'
    case REFUND_WAIT_REFUND:
    case REFUND_REFUNDING:
      return 'primary'
    case REFUND_SUCCESS:
      return 'success'
    default:
      return 'info'
  }
}

export interface RefundItem {
  orderItemId: string
  skuId: string
  title: string
  specText: string
  coverImage: string
  refundNum: number
  refundAmount: number
  stockRestored: boolean
}

export interface Refund {
  refundNo: string
  orderSubNo: string
  orderMainNo: string
  shopId: string
  shopName: string

  refundType: number
  refundTypeText: string
  reasonType: number
  reasonTypeText: string
  reasonDesc: string | null
  images: string[]

  refundAmount: number
  refundFreight: number
  totalRefund: number
  freightBearer: number
  freightBearerText: string

  status: number
  statusText: string
  sourceStatus: number

  merchantRemark: string | null
  rejectReason: string | null

  qualityResult: number | null
  qualityResultText: string | null
  qualityRemark: string | null
  qualityImages: string[]

  returnExpress: string | null
  returnExpressNo: string | null

  applyTime: string
  merchantHandleTime: string | null
  returnTime: string | null
  receiveTime: string | null
  qualityTime: string | null
  refundTime: string | null
  closeTime: string | null
  deadline: string | null

  canRevoke: boolean
  canFillReturn: boolean
  canApprove: boolean
  canReceive: boolean
  canQuality: boolean

  items: RefundItem[]
}

export interface RefundListItem {
  refundNo: string
  orderSubNo: string
  orderMainNo: string
  shopId: string
  shopName: string
  refundType: number
  refundTypeText: string
  status: number
  statusText: string
  totalRefund: number
  itemCount: number
  previewTitle: string
  previewImage: string
  applyTime: string
  deadline: string | null
  canRevoke: boolean
  canFillReturn: boolean
  canApprove: boolean
  canReceive: boolean
  canQuality: boolean
}

export interface RefundList {
  items: RefundListItem[]
  nextCursor: string | null
  hasMore: boolean
}

export function fetchMerchantRefunds(params: {
  status?: number
  pendingOnly?: boolean
  cursor?: string
  limit?: number
}): Promise<RefundList> {
  return get<RefundList>('/merchant/aftersales', { params })
}

export function fetchMerchantRefund(refundNo: string): Promise<Refund> {
  return get<Refund>(`/merchant/aftersales/${refundNo}`)
}

/**
 * 同意售后。
 *
 * 退货退款：等买家寄回；仅退款：立即回补库存并开始退款。
 */
export function approveRefund(refundNo: string, remark?: string): Promise<void> {
  return post<void>(`/merchant/aftersales/${refundNo}/approve`, { remark })
}

export function rejectRefund(refundNo: string, reason: string): Promise<void> {
  return post<void>(`/merchant/aftersales/${refundNo}/reject`, { reason })
}

/** 确认收到退货。**还不回补库存** —— 要等质检合格 */
export function receiveRefund(refundNo: string): Promise<void> {
  return post<void>(`/merchant/aftersales/${refundNo}/receive`)
}

/**
 * 提交质检结果。
 *
 * ★ 合格才会回补库存并发起退款；不合格商品进残次品池、不回补可售库存，
 *   售后单直接终结。
 */
export function submitQuality(
  refundNo: string,
  // images 是上传接口返回的**相对 path**（不是 url）：入库存的是相对路径
  payload: { passed: boolean; remark?: string; images?: string[] },
): Promise<void> {
  return post<void>(`/merchant/aftersales/${refundNo}/quality`, payload)
}
