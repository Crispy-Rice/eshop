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

/** 售后类型。与后端 `aftersale/models.py` 对齐 */
export const TYPE_REFUND_ONLY = 1
export const TYPE_RETURN_REFUND = 2

/** 申请原因 */
export const REASONS: { value: number; label: string }[] = [
  { value: 1, label: '质量问题' },
  { value: 2, label: '不想要了' },
  { value: 3, label: '发错货' },
  { value: 4, label: '少件/漏发' },
  { value: 5, label: '假冒品牌' },
  { value: 6, label: '其他' },
]

export type StatusTone = 'warn' | 'accent' | 'success' | 'muted'

/**
 * 售后状态 → 视觉色调。
 *
 * 「还在路上」用 accent，「有人在等对方动手」用 warn，「结束」按成败分成功/中性。
 * 买卖两端共用一套 —— 需要动作的那几个状态对谁都是"要我处理"。
 */
export function refundStatusTone(status: number): StatusTone {
  switch (status) {
    case REFUND_APPLYING: // 等商家审核
    case REFUND_WAIT_RETURN: // 等买家寄回
    case REFUND_WAIT_RECEIVE: // 等商家收货
    case REFUND_QUALITY_CHECKING: // 等商家质检
      return 'warn'
    case REFUND_WAIT_REFUND:
    case REFUND_REFUNDING:
      return 'accent'
    case REFUND_SUCCESS:
      return 'success'
    default:
      return 'muted'
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

export interface RefundableItem {
  orderItemId: string
  skuId: string
  title: string
  specText: string
  coverImage: string
  num: number
  refundedNum: number
  refundingNum: number
  maxNum: number
  unitPayable: number
}

export interface RefundCheck {
  refundable: boolean
  reason: string | null
  refundType: number
  refundTypeText: string
  maxItemAmount: number
  maxFreight: number
  deadline: string | null
  items: RefundableItem[]
  notices: string[]
}

/** 售后资格预检。**进申请页第一件事就是问它** —— 让用户填表前就知道能退多少 */
export function checkRefund(orderSubNo: string): Promise<RefundCheck> {
  return post<RefundCheck>('/aftersales/check', { orderSubNo })
}

/**
 * 申请售后。
 *
 * ★ 必须带 `Idempotency-Key`：申请页连点两次很常见，没有它就会提交出两张售后单。
 *   售后类型由后端按子单状态推导，这里传的 refundType 只作参考。
 */
export function applyRefund(
  payload: {
    orderSubNo: string
    items: { orderItemId: string; num: number }[]
    reasonType: number
    reasonDesc?: string
    images?: string[]
    refundType?: number
  },
  idempotencyKey: string,
): Promise<Refund> {
  return post<Refund>('/aftersales', payload, {
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

export function fetchRefunds(params: {
  status?: number
  cursor?: string
  limit?: number
}): Promise<RefundList> {
  return get<RefundList>('/aftersales', { params })
}

export function fetchRefund(refundNo: string): Promise<Refund> {
  return get<Refund>(`/aftersales/${refundNo}`)
}

/** 填写退货物流。寄回后商家才能签收 */
export function fillReturnExpress(
  refundNo: string,
  payload: { expressCompany: string; expressNo: string },
): Promise<void> {
  return post<void>(`/aftersales/${refundNo}/return`, payload)
}

/** 撤销申请。商品已寄出后就不允许了 */
export function revokeRefund(refundNo: string): Promise<void> {
  return post<void>(`/aftersales/${refundNo}/revoke`)
}
