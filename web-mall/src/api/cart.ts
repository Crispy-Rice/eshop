import { del, get, post, put } from './http'

/** 购物车项状态。与后端 `cart/schemas.py` 的常量一一对应。 */
export const CART_VALID = 1
export const CART_INVALID = 2
export const CART_OFF_SHELF = 3
export const CART_NO_STOCK = 4

export interface CartItem {
  skuId: string
  spuId: string
  shopId: string

  title: string
  specText: string
  coverImage: string
  skuCode: string

  num: number
  selected: boolean

  /** 当前单价（分）。**结算以此为准**，不是 priceSnapshot */
  price: number
  /** 加购时单价（分），只用于降价提醒 */
  priceSnapshot: number
  /** 较加购时降了多少（分），> 0 才展示 */
  priceDown: number
  itemAmount: number

  status: number
  statusText: string
  available: number
}

export interface CartShopGroup {
  shopId: string
  shopName: string
  items: CartItem[]
  /** 该店已勾选的件数（数量之和） */
  selectedCount: number
  /** 该店已勾选的金额小计（分） */
  selectedAmount: number
}

export interface Cart {
  groups: CartShopGroup[]
  /** 失效/下架的商品单独放，**不参与合计** */
  invalidItems: CartItem[]
  totalCount: number
  totalAmount: number
  skuCount: number
}

export function fetchCart(): Promise<Cart> {
  return get<Cart>('/cart')
}

export function fetchCartCount(): Promise<{ count: number }> {
  return get<{ count: number }>('/cart/count')
}

/** 加购。**累加语义**——同一 SKU 再加会变成 num 相加，不是覆盖。 */
export function addToCart(skuId: string, num = 1, source = 1): Promise<null> {
  return post<null>('/cart/items', { skuId, num, source })
}

/** 改数量。**SET 语义**——把数量设为指定值，不是累加。 */
export function updateCartNum(skuId: string, num: number): Promise<null> {
  return put<null>(`/cart/items/${skuId}`, { num })
}

export function removeCartItems(skuIds: string[]): Promise<null> {
  return del<null>('/cart/items', { data: { skuIds } })
}

export function selectCartItems(skuIds: string[], selected: boolean): Promise<null> {
  return put<null>('/cart/select', { skuIds, selected })
}

/** 清除失效商品。只清"失效/已下架"，无货的留着。 */
export function clearInvalidItems(): Promise<null> {
  return del<null>('/cart/invalid')
}
