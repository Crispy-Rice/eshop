import { get } from './http'

export interface Banner {
  id: string
  title: string
  image: string
  /** 站内路径；null 表示这张图不可点 */
  linkUrl: string | null
  sort: number
  status: number
}

/**
 * 首页轮播图。
 *
 * 公开接口，不需要登录；**只返回启用中的**，并且已经在后端按 sort 排好序，
 * 前端不要再排一遍（否则两处顺序逻辑会各自漂移）。
 */
export const fetchBanners = () => get<Banner[]>('/banners')
