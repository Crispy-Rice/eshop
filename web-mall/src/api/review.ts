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

/** 商品详情页的评价列表排序 / 筛选 */
export const SORTS = [
  { value: 'latest', label: '最新' },
  { value: 'recommend', label: '推荐' },
] as const

export const FILTERS = [
  { value: 'all', label: '全部' },
  { value: 'good', label: '好评' },
  { value: 'with_image', label: '有图' },
] as const

export type ReviewSort = (typeof SORTS)[number]['value']
export type ReviewFilter = (typeof FILTERS)[number]['value']

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
  /** 首评下方挂着的追评 */
  followUp: Review | null
  replies: ReviewReply[]
}

export interface ReviewList {
  items: Review[]
  nextCursor: string | null
  hasMore: boolean
}

export interface ReviewStats {
  reviewCount: number
  /** ★ 没有评价时是 null —— 不要显示成 5.0 或 0 */
  avgScore: number | null
  goodRate: number | null
  goodCount: number
  scoreDistribution: Record<string, number>
}

export interface PendingReviewItem {
  orderItemId: string
  spuId: string
  skuId: string
  title: string
  specText: string
  coverImage: string
  num: number
  receiveTime: string | null
}

export interface PendingReviewList {
  items: PendingReviewItem[]
  nextCursor: string | null
  hasMore: boolean
}

export interface ReviewEligibility {
  eligible: boolean
  reason: string | null
  message: string | null
  orderItemId: string | null
  spuId: string | null
  skuId: string | null
  title: string | null
  specText: string | null
  coverImage: string | null
  num: number | null
  /** 已评价过时带回首评 id，前端可直接去追评 */
  existingReviewId: string | null
  canFollowUp: boolean
}

// ============================================================
// 买家
// ============================================================
export function fetchPendingReviews(params: {
  cursor?: string
  limit?: number
}): Promise<PendingReviewList> {
  return get<PendingReviewList>('/reviews/pending', { params })
}

export function checkReviewEligibility(orderItemId: string): Promise<ReviewEligibility> {
  return post<ReviewEligibility>('/reviews/eligibility', { orderItemId })
}

/**
 * 提交评价。
 *
 * ★ 幂等由后端的 `uk_review_order_item`（每个订单项一条首评）保证 ——
 *   同一订单项重复提交会返回 `ALREADY_REVIEWED`，不是 500。
 */
export function submitReview(payload: {
  orderItemId: string
  score: number
  content?: string
  images?: string[]
  anonymous?: boolean
}, idempotencyKey: string): Promise<Review> {
  return post<Review>('/reviews', payload, {
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

export function submitFollowUp(
  reviewId: string,
  payload: { content: string; images?: string[]; anonymous?: boolean },
): Promise<Review> {
  return post<Review>(`/reviews/${reviewId}/follow-up`, payload)
}

export function fetchMyReviews(params: {
  cursor?: string
  limit?: number
}): Promise<ReviewList> {
  return get<ReviewList>('/reviews/mine', { params })
}

// ============================================================
// 商品详情页（公开接口，不需要登录）
// ============================================================
export function fetchSpuReviews(
  spuId: string,
  params: { sort?: ReviewSort; filter?: ReviewFilter; cursor?: string; limit?: number },
): Promise<ReviewList> {
  return get<ReviewList>(`/spus/${spuId}/reviews`, { params })
}

export function fetchSpuReviewStats(spuId: string): Promise<ReviewStats> {
  return get<ReviewStats>(`/spus/${spuId}/review-stats`)
}
