import { get, post } from './http'

/** 评价状态。与后端 `core/enums.py` 的 ReviewStatus 对齐 */
export const REVIEW_PENDING = 0
export const REVIEW_PUBLISHED = 1
export const REVIEW_BLOCKED = 2
export const REVIEW_REJECTED = 3

export const REVIEW_STATUS_TEXT: Record<number, string> = {
  0: '待审核',
  1: '已发布',
  2: '已屏蔽',
  3: '审核不通过',
}

/** 处置动作。与后端 `review/state_machine.py` 的 AuditAction 对齐 */
export const AUDIT_ACTIONS = {
  APPROVE: 'APPROVE',
  REJECT: 'REJECT',
  BLOCK: 'BLOCK',
  UNBLOCK: 'UNBLOCK',
} as const

export type AuditAction = (typeof AUDIT_ACTIONS)[keyof typeof AUDIT_ACTIONS]

/** 状态 → el-tag 类型 */
export function reviewTagType(status: number): 'warning' | 'success' | 'danger' | 'info' {
  switch (status) {
    case REVIEW_PENDING:
      return 'warning'
    case REVIEW_PUBLISHED:
      return 'success'
    case REVIEW_REJECTED:
      return 'danger'
    default:
      return 'info'
  }
}

export interface ReviewImage {
  path: string
  url: string
  thumbUrl: string
}

export interface ReviewReply {
  replyType: number
  replyTypeText: string
  content: string
  createdAt: string
}

export interface Review {
  reviewId: string
  score: number
  content: string | null
  images: ReviewImage[]
  anonymous: boolean
  nickname: string
  avatar: string | null
  specText: string
  buyCount: number
  likeCount: number
  status: number
  statusText: string
  createdAt: string
  isFollowUp: boolean
  followUp: Review | null
  replies: ReviewReply[]
  suspect: boolean
  needSecondAudit: boolean
  auditRemark: string | null
}

export interface ReviewList {
  items: Review[]
  nextCursor: string | null
  hasMore: boolean
}

export interface AuditQueueItem {
  review: Review
  shopId: string
  orderMainNo: string
  reasonText: string
}

export interface AuditQueue {
  items: AuditQueueItem[]
  nextCursor: string | null
  hasMore: boolean
  pendingCount: number
  secondAuditCount: number
}

// ============================================================
// 商家
// ============================================================
export function fetchMerchantReviews(params: {
  status?: number
  cursor?: string
  limit?: number
}): Promise<ReviewList> {
  return get<ReviewList>('/merchant/reviews', { params })
}

/**
 * 回复评价。
 *
 * 一条评价最多 3 条商家回复 —— 第 4 次会返回 `REPLY_LIMIT_EXCEEDED`，
 * 这是服务端用行锁 + 计数保证的，不是前端限制。
 */
export function replyReview(reviewId: string, content: string): Promise<void> {
  return post<void>(`/merchant/reviews/${reviewId}/reply`, { content })
}

// ============================================================
// 运营（admin 角色）
// ============================================================
export function fetchAuditQueue(params: {
  status?: number
  secondAuditOnly?: boolean
  cursor?: string
  limit?: number
}): Promise<AuditQueue> {
  return get<AuditQueue>('/admin/reviews/audit-queue', { params })
}

/**
 * 处置评价。
 *
 * ★ 后端会**同步更新商品的评价统计**（发布 +1、屏蔽 −1、解除 +1、驳回不动），
 *   所以处置完刷新商品列表，评分就已经变了。
 */
export function auditReview(
  reviewId: string,
  action: AuditAction,
  remark?: string,
): Promise<void> {
  return post<void>(`/admin/reviews/${reviewId}/audit`, { action, remark })
}
