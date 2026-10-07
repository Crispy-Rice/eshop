import { del, get, post, put } from './http'

/**
 * 后台 AI 助手。
 *
 * ★ **只有一组端点**（不带 `/merchant` 或 `/admin` 前缀）：这个功能是商家和运营共用的，
 *   角色决定的是"能用哪些工具"，不是"能不能用"。所以前端不必按身份分两套。
 *
 * ★ 问答应答是**异步**的：`ask()` 立刻返回一个 messageId，然后用 `useAnswerPoll`
 *   轮询会话详情，直到那条消息的状态不再是 `pending` / `running`。
 *
 * ★ 会话是**一个用户多条**：`ask()` 不传 `conversationNo` 就是开一条新的
 *   （「新对话」按钮的实现方式就是把它清空）。
 */

/** 消息状态。只有 `pending` / `running` 需要继续轮询。 */
export const MSG_PENDING = 'pending'
export const MSG_RUNNING = 'running'
export const MSG_DONE = 'done'
export const MSG_FAILED = 'failed'
export const MSG_DEGRADED = 'degraded'

export interface AssistantMessage {
  id: string
  role: 'user' | 'assistant'
  status: string
  content: string | null
  errorCode: string | null
  createTime: string
}

export interface Conversation {
  conversationNo: string | null
  title: string | null
  /** false = 这个账号还用不了助手（比如商家还没开店），入口该藏起来 */
  available: boolean
  messages: AssistantMessage[]
  /** 还有更早的消息；有的话把 nextCursor 原样回传就能取上一页 */
  hasMore: boolean
  nextCursor: string | null
}

/** 「历史对话」侧栏的一行（不含消息，列表只要标题与时间）。 */
export interface ConversationListItem {
  conversationNo: string
  title: string
  updatedAt: string
}

export interface ConversationList {
  items: ConversationListItem[]
  hasMore: boolean
  nextCursor: string | null
}

export interface AskReceipt {
  conversationNo: string
  messageId: string
}

export interface HandoffReceipt {
  ticketNo: string
  reused: boolean
}

/** 最近动过的那条会话（面板首次打开用 —— 不必先知道会话号）。 */
export function fetchConversation(): Promise<Conversation> {
  return get<Conversation>('/assistant/conversation')
}

/** 某一条会话。`before` 给定时取**更早的一页**。 */
export function fetchConversationByNo(
  conversationNo: string,
  before?: string | null,
): Promise<Conversation> {
  return get<Conversation>(`/assistant/conversations/${conversationNo}`, {
    params: before ? { before } : undefined,
  })
}

/** 自己的会话列表（最近动过的在前）。 */
export function fetchConversations(cursor?: string | null): Promise<ConversationList> {
  return get<ConversationList>('/assistant/conversations', {
    params: cursor ? { cursor } : undefined,
  })
}

/** 提问。不传 `conversationNo` = 开一条新对话。 */
export function ask(question: string, conversationNo?: string | null): Promise<AskReceipt> {
  return post<AskReceipt>('/assistant/messages', {
    question,
    ...(conversationNo ? { conversationNo } : {}),
  })
}

/**
 * 转人工：把对话摘要交给平台客服。
 *
 * ★ 一定要带 `conversationNo`（面板当前看着的那条）—— 不传的话后端取"最近动过的
 *   那条"，而用户可能刚从「历史对话」里翻回一条旧的，转过去的摘要就张冠李戴了。
 * `note` 是可选的一句补充说明：**没有对话时它也能单独成立**（"我直接想找客服"）。
 */
export function handoff(
  note?: string,
  conversationNo?: string | null,
): Promise<HandoffReceipt> {
  return post<HandoffReceipt>('/assistant/handoff', {
    ...(note ? { note } : {}),
    ...(conversationNo ? { conversationNo } : {}),
  })
}

export function isInflight(msg: AssistantMessage): boolean {
  return msg.status === MSG_PENDING || msg.status === MSG_RUNNING
}

// ==================================================================
// 店小蜜（买家侧的智能客服）—— 商户自己维护的开关与问答。docs/20 §14
//
// ★ 与上面的悬浮助手**不是一回事**：那个帮**商家自己**查数；这一组是替商家
//   **对买家说话**。所以名字取「店小蜜 / shopBot」把它们分开。
// ★ 这一组全是**本店**范围（后端 `caller.require_shop()`，请求里没有 shopId 可传）：
//   让不让 AI 以本店的名义答买家，是商户自己的事，平台不替他决定 ——
//   运营账号调这些会拿到 403。
// ==================================================================

export interface ShopBotSetting {
  aiEnabled: boolean
}

/** 一条商户自填的问答。`enabled=false` 的不会进提示词。 */
export interface ShopFaq {
  id: string
  question: string
  answer: string
  enabled: boolean
  updatedAt: string
}

export interface ShopFaqPayload {
  question: string
  answer: string
  enabled: boolean
}

/**
 * 今日计数。
 *
 * ★ `notAnswered` 是"AI 没说话"的条数（答不了、限流、开关关了、被真人抢答），
 *   这些会话都还**留在商家的待回复队列里** —— 也就是都落到了人工。
 */
export interface ShopBotStats {
  answered: number
  notAnswered: number
  pending: number
  tokensToday: number
}

export function fetchShopBotSetting(): Promise<ShopBotSetting> {
  return get<ShopBotSetting>('/assistant/shop-setting')
}

export function updateShopBotSetting(aiEnabled: boolean): Promise<ShopBotSetting> {
  return put<ShopBotSetting>('/assistant/shop-setting', { aiEnabled })
}

export function fetchShopFaqs(): Promise<ShopFaq[]> {
  return get<ShopFaq[]>('/assistant/shop-faq')
}

export function createShopFaq(payload: ShopFaqPayload): Promise<ShopFaq> {
  return post<ShopFaq>('/assistant/shop-faq', payload)
}

export function updateShopFaq(id: string, payload: ShopFaqPayload): Promise<ShopFaq> {
  return put<ShopFaq>(`/assistant/shop-faq/${id}`, payload)
}

export function deleteShopFaq(id: string): Promise<null> {
  return del<null>(`/assistant/shop-faq/${id}`)
}

export function fetchShopBotStats(): Promise<ShopBotStats> {
  return get<ShopBotStats>('/assistant/shop-bot/stats')
}
