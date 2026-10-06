import { get, post } from './http'

export interface Category {
  id: string
  parentId: string | null
  name: string
  level: number
  sort: number
  children: Category[]
}

export interface SpuCard {
  id: string
  shopId: string
  categoryId: string
  title: string
  mainImage: string
  /** 640 中间档。卡片用它渲染；缺失时回退到 mainImage */
  mainImageMid: string
  priceMin: number
  priceMax: number
  totalSold: number
  avgScore: number
  reviewCount: number
  status: number
}

export interface SpecValue {
  id: string
  value: string
  image: string | null
}

export interface SpecGroup {
  id: string
  name: string
  values: SpecValue[]
}

export interface SkuDetail {
  id: string
  skuCode: string
  specText: string
  price: number
  coverImage: string
  weightG: number
  status: number
  /** 该 SKU 在各规格组下选中的值，前端据此推导"哪些组合可选" */
  specValueIds: string[]
}

export interface SpuDetail {
  id: string
  shopId: string
  categoryId: string
  title: string
  subTitle: string | null
  mainImage: string
  priceMin: number
  priceMax: number
  totalSold: number
  status: number
  specGroups: SpecGroup[]
  skus: SkuDetail[]
}

export interface SpuList {
  items: SpuCard[]
  hasMore: boolean
  nextCursor: string | null
  /** 符合条件的总数。**只有第一页给**（翻页时是 null），拿不到就退回"已显示 N 件" */
  total: number | null
}

export type SearchSort = 'relevance' | 'sales' | 'newest' | 'price_asc' | 'price_desc'

export interface SearchParams {
  keyword?: string
  categoryId?: string
  /** 只看这家店的商品（店铺页用）。不传就是全平台 */
  shopId?: string
  priceFrom?: number
  priceTo?: number
  sort?: SearchSort
  cursor?: string | null
  limit?: number
}

export const fetchCategoryTree = () => get<Category[]>('/categories')

export const searchProducts = (params: SearchParams) => get<SpuList>('/search', { params })

export const fetchSpu = (spuId: string) => get<SpuDetail>(`/spus/${spuId}`)

export const fetchSkus = (skuIds: string[]) => post<unknown[]>('/skus/batch', { skuIds })
