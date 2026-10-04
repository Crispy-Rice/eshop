import { get, post, put } from './http'

export interface SpuCard {
  id: string
  shopId: string
  categoryId: string
  title: string
  mainImage: string
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
}

export interface Shop {
  id: string
  name: string
  logo: string | null
  description: string | null
  status: number
}

// ---------- 发布商品 ----------

export interface SpecValueIn {
  key: string
  value: string
  image?: string | null
}

export interface SpecGroupIn {
  name: string
  values: SpecValueIn[]
}

export interface SkuIn {
  skuCode: string
  /** 每个规格组各选一个值，顺序不限 */
  specValueKeys: string[]
  price: number
  coverImage: string
  weightG: number
}

export interface SpuCreateInput {
  categoryId: string
  title: string
  subTitle?: string | null
  mainImage: string
  specGroups: SpecGroupIn[]
  skus: SkuIn[]
}

/** SPU 状态：1草稿 2上架 3下架 4违规下架 5待审核 */
export const SPU_STATUS_TEXT: Record<number, string> = {
  1: '草稿',
  2: '已上架',
  3: '已下架',
  4: '违规下架',
  5: '待审核',
}

export const SPU_STATUS_TYPE: Record<number, 'info' | 'success' | 'warning' | 'danger'> = {
  1: 'info',
  2: 'success',
  3: 'warning',
  4: 'danger',
  5: 'warning',
}

// ---------- 接口 ----------

export const listMySpus = (params: { status?: number; keyword?: string; cursor?: string | null; limit?: number }) =>
  get<SpuList>('/merchant/spus', { params })

export const fetchMySpu = (spuId: string) => get<SpuDetail>(`/merchant/spus/${spuId}`)

export const createSpu = (body: SpuCreateInput) => post<SpuDetail>('/merchant/spus', body)

export const submitForAudit = (spuId: string) => post<null>(`/merchant/spus/${spuId}/submit`)

export const onShelf = (spuId: string) => post<null>(`/merchant/spus/${spuId}/on-shelf`)

export const offShelf = (spuId: string) => post<null>(`/merchant/spus/${spuId}/off-shelf`)

export const updateSku = (
  skuId: string,
  body: { price?: number; coverImage?: string; weightG?: number; status?: number },
) => put<null>(`/merchant/skus/${skuId}`, body)

export const updateSpu = (
  spuId: string,
  body: { title?: string; subTitle?: string | null; mainImage?: string; sortWeight?: number },
) => put<SpuDetail>(`/merchant/spus/${spuId}`, body)

export const fetchMyShop = () => get<Shop>('/merchant/shop')

export const auditSpu = (spuId: string, approved: boolean, remark?: string) =>
  post<null>(`/admin/spus/${spuId}/audit`, { approved, remark: remark ?? null })
