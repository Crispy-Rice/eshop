import { get, post } from './http'

/** 与后端 `core/enums.py` 的 `UserStatus` 对齐（account/models.py 的 User.status） */
export const USER_NORMAL = 1
export const USER_BANNED = 2
export const USER_CLOSED = 3

export const USER_STATUS_TEXT: Record<number, string> = {
  [USER_NORMAL]: '正常',
  [USER_BANNED]: '已封禁',
  [USER_CLOSED]: '已注销',
}

/** 标签配色：正常=绿、封禁=红、注销=灰 */
export function userTagType(status: number): 'success' | 'danger' | 'info' {
  if (status === USER_NORMAL) return 'success'
  if (status === USER_BANNED) return 'danger'
  return 'info'
}

export interface AdminUser {
  id: string
  nickname: string
  /** 脱敏手机号（138****8888）。后台刻意不提供解密查看 */
  phone: string
  avatar: string | null
  gender: number
  role: string
  status: number
  statusText: string
  memberLevel: number
  creditScore: number
  /** 名下有店铺 —— 店主不能自助注销，看到标记才知道该先让商家处理店铺 */
  isShopOwner: boolean
  registerTime: string
  closedAt: string | null
}

export interface AdminUserDetail extends AdminUser {
  /** 最近一次封禁的理由（解封时清空） */
  banReason: string | null
}

export interface AdminUserList {
  items: AdminUser[]
  nextCursor: string | null
  hasMore: boolean
}

/**
 * 用户列表。关键词**两种走法**（后端按形状分派）：11 位手机号走精确匹配
 * （库里存的是哈希，没法 LIKE），其它按昵称模糊搜。
 */
export function fetchAdminUsers(params: {
  status?: number
  keyword?: string
  cursor?: string
  limit?: number
}): Promise<AdminUserList> {
  return get<AdminUserList>('/admin/users', { params })
}

export function fetchAdminUser(userId: string): Promise<AdminUserDetail> {
  return get<AdminUserDetail>(`/admin/users/${userId}`)
}

/**
 * 封禁（第一版只禁登录）。理由**必填**，而且会展示给被封的人 —— 文案要能见人。
 *
 * ★ 生效时机：登录与刷新立刻被拒（并吊销该用户全部 refresh token），
 *   但**已签发的 access token 最长还能用 30 分钟**。所以界面文案别写"立即生效"。
 */
export function banUser(userId: string, reason: string): Promise<null> {
  return post<null>(`/admin/users/${userId}/ban`, { reason })
}

export function unbanUser(userId: string): Promise<null> {
  return post<null>(`/admin/users/${userId}/unban`)
}

/**
 * 重置密码。后端把该用户改成一个随机临时口令并**只返回这一次** ——
 * 运营要转告用户，并让他登录后立即自行修改。
 */
export function resetUserPassword(userId: string): Promise<{ tempPassword: string }> {
  return post<{ tempPassword: string }>(`/admin/users/${userId}/reset-password`)
}
