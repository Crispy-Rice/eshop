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

/**
 * 改密码。返回**新的一对令牌**。
 *
 * ★ 后端会把该账号其它设备上的 refresh token 全部吊销（改密的动机常常就是
 *   "怀疑别人在用我的号"），同时给当前会话补发一对 —— 所以调用方要把返回的
 *   令牌写回本地，用户不会被打回登录页。
 */
export const changePassword = (oldPassword: string, newPassword: string) =>
  put<TokenResponse>('/me/password', { oldPassword, newPassword })

/**
 * 注销账号（**不可恢复**：手机号、昵称、地址都会被匿名化）。要带当前密码 ——
 * 没有短信验证码，这是唯一能证明"是本人"的二次确认。
 *
 * ★ 可能被三条守卫拦下，后端会回具体原因与数量，直接把 message 透给用户：
 *   ACCOUNT_HAS_UNFINISHED（还有未完成订单/售后）、
 *   SHOP_OWNER_CANNOT_DEACTIVATE（名下有店铺）。
 */
export const deactivateAccount = (password: string) =>
  post<null>('/me/deactivate', { password })

/** 注销前置检查的结果。**只报事实**，文案与"去哪处理"的按钮由页面组织。 */
export interface DeactivationCheck {
  canDeactivate: boolean
  hasShop: boolean
  orderCount: number
  refundCount: number
}

/**
 * 注销前先问一次「能不能注销、被什么挡着」。
 *
 * ★ 存在的意义是**别让用户白输一次密码**：原先只有提交之后才知道被挡，
 *   而那时密码已经输过了，提示还是个转瞬即逝的 toast。
 */
export const fetchDeactivationCheck = () => get<DeactivationCheck>('/me/deactivation')

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

/**
 * 批量取店铺信息 —— 商品列表页一次显示 N 个商品，逐个请求就是 N+1。
 *
 * ★ 必须带 paramsSerializer：axios 默认把数组序列化成 `ids[]=1&ids[]=2`，
 *   而 FastAPI 认的是重复参数 `ids=1&ids=2`。`indexes: null` 就是"不要方括号"。
 */
export const fetchShops = (shopIds: string[]) =>
  get<ShopInfo[]>('/shops', { params: { ids: shopIds }, paramsSerializer: { indexes: null } })
