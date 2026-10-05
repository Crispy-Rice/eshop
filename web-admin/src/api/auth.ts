import { del, get, post, put } from './http'

export interface TokenResponse {
  accessToken: string
  refreshToken: string
  tokenType: string
  expiresIn: number
}

export interface UserInfo {
  id: string
  phone: string
  nickname: string
  avatar: string | null
  gender: number
  role: string
  memberLevel: number
  creditScore: number
  shopId: string | null
  registerTime: string
}

export interface Address {
  id: string
  receiverName: string
  phone: string
  province: string
  city: string
  district: string
  detail: string
  regionCode: string
  tag: string | null
  isDefault: boolean
}

export interface AddressInput {
  receiverName: string
  phone: string
  province: string
  city: string
  district: string
  detail: string
  regionCode: string
  tag?: string | null
  isDefault: boolean
}

export const register = (phone: string, password: string, nickname?: string) =>
  post<TokenResponse>('/auth/register', { phone, password, nickname })

export const login = (phone: string, password: string) =>
  post<TokenResponse>('/auth/login', { phone, password })

export const logout = (refreshToken: string) => post<null>('/auth/logout', { refreshToken })

export const fetchMe = () => get<UserInfo>('/me')

export const updateMe = (payload: Partial<Pick<UserInfo, 'nickname' | 'avatar' | 'gender'>>) =>
  put<UserInfo>('/me', payload)

export const listAddresses = () => get<Address[]>('/me/addresses')

export const createAddress = (payload: AddressInput) => post<Address>('/me/addresses', payload)

export const updateAddress = (id: string, payload: AddressInput) =>
  put<Address>(`/me/addresses/${id}`, payload)

export const deleteAddress = (id: string) => del<null>(`/me/addresses/${id}`)

/**
 * 店铺。和 `/me`、地址同属 account 域，所以放这里。
 *
 * `logo` 存的是**完整 url** 而不是相对路径 —— 商品详情页与列表卡片直接把它当
 * `<img src>` 用（web-mall 的 `ShopInfo.logo` 是同一个字段），所以上传后要拿
 * `url`。业务线是 `shops`（后端 files 模块的白名单里有）。
 */
export interface Shop {
  id: string
  name: string
  logo: string | null
  description: string | null
  status: number
}

export interface ShopInput {
  name?: string
  /** ★ 显式传 null 是**清空**，不传是**不改** —— 后端用 model_fields_set 区分 */
  logo?: string | null
  description?: string | null
}

export const fetchMyShop = () => get<Shop>('/merchant/shop')

export const updateMyShop = (payload: ShopInput) => put<Shop>('/merchant/shop', payload)

/**
 * 批量取店铺公开信息 —— 平台的商品审核队列要显示"这件商品是哪家店提交的"，
 * 一行一个请求就是 N+1。
 *
 * ★ 必须带 paramsSerializer：axios 默认把数组序列化成 `ids[]=1&ids[]=2`，
 *   而 FastAPI 认的是重复参数 `ids=1&ids=2`。`indexes: null` 就是"不要方括号"。
 */
export const fetchShops = (shopIds: string[]) =>
  get<Shop[]>('/shops', { params: { ids: shopIds }, paramsSerializer: { indexes: null } })
