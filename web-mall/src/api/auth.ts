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
 * 店铺的公开信息 —— 商品详情页要显示"这件商品是哪家店的"。
 *
 * 放在这里是因为它和 `/me`、地址同属 account 域。接口**匿名可读**，
 * 所以没登录的买家也能看到店铺。
 */
export interface ShopInfo {
  id: string
  name: string
  logo: string | null
  description: string | null
  status: number
}

export const fetchShop = (shopId: string) => get<ShopInfo>(`/shops/${shopId}`)
