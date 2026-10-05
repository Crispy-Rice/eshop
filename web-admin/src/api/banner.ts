import { del, get, post, put } from './http'

export interface Banner {
  id: string
  title: string
  image: string
  /** 站内路径（以 / 开头）；null 表示这张图不可点 */
  linkUrl: string | null
  sort: number
  status: number
}

export interface BannerInput {
  title: string
  image: string
  linkUrl?: string | null
  sort?: number
}

/** 状态：1 启用 2 停用。与后端 `promotion/models.py` 的 BANNER_* 一致 */
export const BANNER_STATUS_TEXT: Record<number, string> = {
  1: '启用',
  2: '停用',
}

/** 后台：含停用（否则停用一张图就再也找不回来） */
export const listBanners = () => get<Banner[]>('/admin/banners')

export const createBanner = (body: BannerInput) => post<Banner>('/admin/banners', body)

/** 部分更新：只传要改的字段。`linkUrl` 传 `null` 表示清空链接 */
export const updateBanner = (id: string, body: Partial<BannerInput> & { status?: number }) =>
  put<Banner>(`/admin/banners/${id}`, body)

export const deleteBanner = (id: string) => del<null>(`/admin/banners/${id}`)
