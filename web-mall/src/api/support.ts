import { get, post } from './http'

/** 会话来源。与后端 `support/models.py` 的 `SOURCE_*` 一一对应。 */
export const TICKET_SOURCE = {
  PRODUCT: 1,
  ORDER: 2,
  AFTERSALE: 3,
  APPEAL: 4,
  OTHER: 5,
} as const

export interface TicketMessage {
  id: string
  senderType: number
  senderTypeText: string
  senderId: string | null
  body: string
  images: string[]
  createdAt: string
}

/** 弱关联的上下文。**只用来渲染跳转** —— 不参与任何鉴权。 */
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

export interface OpenTicketPayload {
  /** 不传 = 平台级会话 */
  shopId?: string
  source?: number
  subject?: string
  orderMainNo?: string
  orderSubNo?: string
  refundNo?: string
}

/**
 * 开会话。**已有进行中的会话就把那一条还给你** —— 所以可以放心当作"进入会话"
 * 的入口来调，不必先查有没有。首条消息另发（`sendTicketMessage`）。
 */
export function openTicket(payload: OpenTicketPayload): Promise<TicketDetail> {
  return post<TicketDetail>('/support/tickets', payload)
}

export function fetchMyTickets(params: {
  cursor?: string
  limit?: number
}): Promise<TicketList> {
  return get<TicketList>('/support/tickets', { params })
}

/** 会话详情。带 `before`（当前最早那条消息的 id）就是**往前翻**更早的消息。 */
export function fetchTicket(ticketNo: string, before?: string): Promise<TicketDetail> {
  return get<TicketDetail>(`/support/tickets/${ticketNo}`, {
    params: before ? { before } : undefined,
  })
}

export function sendTicketMessage(
  ticketNo: string,
  body: string,
  images: string[] = [],
): Promise<TicketMessage> {
  return post<TicketMessage>(`/support/tickets/${ticketNo}/messages`, { body, images })
}

/** 结束会话。已关闭时重复调用**不报错**（后端幂等）。之后再发消息会自动重开。 */
export function closeTicket(ticketNo: string, reason?: string): Promise<void> {
  return post<void>(`/support/tickets/${ticketNo}/close`, { reason })
}
