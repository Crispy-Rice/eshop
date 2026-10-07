import { get, post } from './http'

/**
 * 客服会话。
 *
 * ★ 商家与平台是**两组端点**：`/merchant/support/*` 按店铺归属判权（CurrentShopIdDep），
 *   `/admin/support/*` 要 admin 角色。页面按身份选一组 —— 后端守卫各不相同，
 *   前端不要自己"合并"成一套。
 */

export const TICKET_OPEN = 10
export const TICKET_CLOSED = 30

export interface TicketMessage {
  id: string
  senderType: number
  senderTypeText: string
  senderId: string | null
  body: string
  images: string[]
  createdAt: string
}

export interface TicketContext {
  orderMainNo: string | null
  orderSubNo: string | null
  refundNo: string | null
}

export interface TicketListItem {
  ticketNo: string
  shopId: string
  shopName: string
  userId: string
  /** 平台刻意不提供解密查看，这里**只有打码手机号** */
  buyerNickname: string
  buyerPhone: string
  subject: string
  source: number
  sourceText: string
  status: number
  statusText: string
  lastMessageAt: string
  lastSenderType: number
  lastSenderText: string
  unread: number
  staffOwesReply: boolean
  createdAt: string
}

export interface TicketDetail extends Omit<TicketListItem, 'unread'> {
  context: TicketContext
  closeByText: string | null
  closeReason: string | null
  closeTime: string | null
  messages: TicketMessage[]
  hasMoreMessages: boolean
  nextMessageCursor: string | null
}

export interface TicketList {
  items: TicketListItem[]
  nextCursor: string | null
  hasMore: boolean
}

function listParams(params: {
  pendingOnly?: boolean
  status?: number
  cursor?: string
  limit?: number
}): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {}
  if (params.pendingOnly) out.pendingOnly = true
  if (params.status !== undefined) out.status = params.status
  if (params.cursor) out.cursor = params.cursor
  if (params.limit !== undefined) out.limit = params.limit
  return out
}

// ---------------- 商家 ----------------
export function fetchMerchantTickets(params: {
  pendingOnly?: boolean
  cursor?: string
  limit?: number
}): Promise<TicketList> {
  return get<TicketList>('/merchant/support/tickets', { params: listParams(params) })
}

export function fetchMerchantTicket(ticketNo: string, before?: string): Promise<TicketDetail> {
  return get<TicketDetail>(`/merchant/support/tickets/${ticketNo}`, {
    params: before ? { before } : undefined,
  })
}

export function replyAsMerchant(
  ticketNo: string,
  body: string,
  images: string[] = [],
): Promise<TicketMessage> {
  return post<TicketMessage>(`/merchant/support/tickets/${ticketNo}/messages`, { body, images })
}

export function closeAsMerchant(ticketNo: string, reason?: string): Promise<void> {
  return post<void>(`/merchant/support/tickets/${ticketNo}/close`, { reason })
}

/** 待回复条数（导航角标）。列表是游标分页的，数一页会少报，所以单独取。 */
export function fetchMerchantPendingCount(): Promise<{ count: number }> {
  return get<{ count: number }>('/merchant/support/pending-count')
}

// ---------------- 平台（只做平台级会话）----------------
//
// ★ 平台**看不到店里买家的会话**（后端的 `service.list_platform` 把范围钉在平台级）：
//   原来这里有个 `shopId` 过滤参数，但页面从来没传过 —— 等于"能看全部店铺"，
//   而平台以「平台客服」身份插进买家与商家的对话里，买家连在跟谁说话都分不清。
//   参数已删：接口不接受它，前端也不该留一个发不出去的参数。
export function fetchAdminTickets(params: {
  status?: number
  pendingOnly?: boolean
  cursor?: string
  limit?: number
}): Promise<TicketList> {
  return get<TicketList>('/admin/support/tickets', { params: listParams(params) })
}

export function fetchAdminTicket(ticketNo: string, before?: string): Promise<TicketDetail> {
  return get<TicketDetail>(`/admin/support/tickets/${ticketNo}`, {
    params: before ? { before } : undefined,
  })
}

export function replyAsAdmin(
  ticketNo: string,
  body: string,
  images: string[] = [],
): Promise<TicketMessage> {
  return post<TicketMessage>(`/admin/support/tickets/${ticketNo}/messages`, { body, images })
}

export function closeAsAdmin(ticketNo: string, reason?: string): Promise<void> {
  return post<void>(`/admin/support/tickets/${ticketNo}/close`, { reason })
}

export function fetchAdminPendingCount(): Promise<{ count: number }> {
  return get<{ count: number }>('/admin/support/pending-count')
}

// ---------------- 我提交给平台的（买家侧接口，商家/运营都在用）----------------
//
// ★ 后台的人**也会是提问方**：助手答不了时可以「转人工」，那条会话落在平台队列里，
//   而开单与读回复走的都是买家那组接口（`/api/support/*`）。没有这几个函数，
//   用户开了单就看不到回复 —— 那是"只写不读"。

export function fetchMyTickets(params: {
  cursor?: string
  limit?: number
}): Promise<TicketList> {
  const query: Record<string, string | number> = {}
  if (params.cursor) query.cursor = params.cursor
  if (params.limit !== undefined) query.limit = params.limit
  return get<TicketList>('/support/tickets', { params: query })
}

export function fetchMyTicket(ticketNo: string): Promise<TicketDetail> {
  return get<TicketDetail>(`/support/tickets/${ticketNo}`)
}

export function replyMyTicket(
  ticketNo: string,
  body: string,
  images: string[] = [],
): Promise<TicketMessage> {
  return post<TicketMessage>(`/support/tickets/${ticketNo}/messages`, { body, images })
}

export function closeMyTicket(ticketNo: string, reason?: string): Promise<void> {
  return post<void>(`/support/tickets/${ticketNo}/close`, { reason })
}

