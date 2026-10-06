import { get, post } from './http'

export interface SiteMessage {
  id: string
  msgType: string
  msgTypeText: string
  title: string
  body: string
  /** ORDER / REFUND / TICKET / NONE */
  linkType: string
  linkValue: string | null
  isRead: boolean
  createdAt: string
}

export interface SiteMessageList {
  items: SiteMessage[]
  nextCursor: string | null
  hasMore: boolean
}

export function fetchNotifications(params: {
  unreadOnly?: boolean
  cursor?: string
  limit?: number
}): Promise<SiteMessageList> {
  return get<SiteMessageList>('/notifications', { params })
}

/** 未读数（导航角标）。 */
export function fetchUnreadCount(): Promise<{ count: number }> {
  return get<{ count: number }>('/notifications/unread-count')
}

export function markNotificationRead(id: string): Promise<void> {
  return post<void>(`/notifications/${id}/read`)
}

export function markAllNotificationsRead(): Promise<void> {
  return post<void>('/notifications/read-all')
}
