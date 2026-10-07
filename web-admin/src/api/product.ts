import { del, get, post, put } from './http'

export interface SpuCard {
  id: string
  shopId: string
  categoryId: string
  title: string
  mainImage: string
  /** 640 中间档。列表缩略用它渲染；缺失时回退到 mainImage */
  mainImageMid: string
  priceMin: number
  priceMax: number
  totalSold: number
  avgScore: number
  reviewCount: number
  status: number
  /** 状态文案。**由后端给**（product service 那一份），前端没有再维护映射表 */
  statusText: string
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
  /** 状态文案。**由后端给**（与商家在「商品管理」页看到的是同一处来源） */
  statusText: string
  /** 最近一次审核意见。**只有店主看得到**，买家视角恒为 null */
  auditRemark: string | null
  specGroups: SpecGroup[]
  skus: SkuDetail[]
  /**
   * 能不能整体替换规格与 SKU。
   *
   * 只有**商家详情**接口会算它（要看该商品有没有订单），且要求状态不是
   * 在售 / 待审核。买家与平台视角恒为 false。
   */
  specEditable: boolean
}

export interface SpuList {
  items: SpuCard[]
  hasMore: boolean
  nextCursor: string | null
  /** 总数。只有公开 `/search` 的第一页给；商家/审核列表恒为 null */
  total: number | null
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
  /** 商家编码，**选填**；不填就传 null。填了要在同一个商品内唯一 */
  skuCode?: string | null
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

/**
 * SPU 状态 → 标签颜色。
 *
 * ★ **这里只留颜色**：文案由后端给（`SpuCard.statusText` / `SpuDetail.statusText`）。
 *   原来前端也维护了一份 `SPU_STATUS_TEXT`，于是同一件事有两个来源 ——
 *   改了一边忘了另一边，页面上写的和接口里说的就不是一回事，而这种「差一个词」
 *   没人会发现。颜色是纯展示，后端不管，所以它留下。
 */
export const SPU_STATUS_TYPE: Record<number, 'info' | 'success' | 'warning' | 'danger'> = {
  1: 'info',
  2: 'success',
  3: 'warning',
  4: 'danger',
  5: 'warning',
  6: 'danger',
}

/**
 * 商家可以提交审核的状态：新建的草稿（1）和平台打回待改的（6）。
 * 对应后端的 `models.SPU_SUBMITTABLE`，改一处要跟着改另一处。
 */
export const SUBMITTABLE_STATUSES: readonly number[] = [1, 6]

// ---------- 接口 ----------

export const listMySpus = (params: { status?: number; keyword?: string; cursor?: string | null; limit?: number }) =>
  get<SpuList>('/merchant/spus', { params })

export const fetchMySpu = (spuId: string) => get<SpuDetail>(`/merchant/spus/${spuId}`)

export const createSpu = (body: SpuCreateInput) => post<SpuDetail>('/merchant/spus', body)

export const submitForAudit = (spuId: string) => post<null>(`/merchant/spus/${spuId}/submit`)

export const onShelf = (spuId: string) => post<null>(`/merchant/spus/${spuId}/on-shelf`)

export const offShelf = (spuId: string) => post<null>(`/merchant/spus/${spuId}/off-shelf`)

/**
 * 删除商品。**软删**：商品从商城、自己的列表、平台审核列表里一起消失，
 * 但历史订单不受影响（订单读的是商品快照），库存页会保留它的库存行。
 *
 * 在售的（status=2）和审核中的（status=5）后端会拒绝，分别提示先下架 / 等审核结果。
 */
export const deleteSpu = (spuId: string) => del<null>(`/merchant/spus/${spuId}`)

export const updateSku = (
  skuId: string,
  body: { price?: number; coverImage?: string; weightG?: number; status?: number },
) => put<null>(`/merchant/skus/${skuId}`, body)

export const updateSpu = (
  spuId: string,
  body: { title?: string; subTitle?: string | null; mainImage?: string; sortWeight?: number },
) => put<SpuDetail>(`/merchant/spus/${spuId}`, body)

/** 替换规格的请求体：就是新建那两段，标题 / 类目 / 主图不在这里改 */
export type SpuSpecsReplaceInput = Pick<SpuCreateInput, 'specGroups' | 'skus'>

/**
 * 整体替换规格与 SKU。
 *
 * **只允许没有订单的商品**（后端会查 trade 的订单项），否则 400。
 * `spu_id` 不变 —— 购物车行和外部链接都不受影响。
 */
export const replaceSpuSpecs = (spuId: string, body: SpuSpecsReplaceInput) =>
  put<SpuDetail>(`/merchant/spus/${spuId}/specs`, body)

export const auditSpu = (spuId: string, approved: boolean, remark?: string) =>
  post<null>(`/admin/spus/${spuId}/audit`, { approved, remark: remark ?? null })

/**
 * 平台侧的商品列表（**跨店铺**），审核队列用它。
 *
 * 与 `listMySpus` 的区别只有"不限定店铺"和"能看到待审核"——
 * 后端那条路径本来就支持状态过滤，所以这里是同一个返回结构。
 */
export const listAdminSpus = (params: {
  status?: number
  keyword?: string
  cursor?: string | null
  limit?: number
}) => get<SpuList>('/admin/spus', { params })

/**
 * 公开搜索（买家侧那条）。**轮播图选商品用它**。
 *
 * ★ 不能用 `/merchant/spus`：那条按店铺归属判权，而平台运营没有店铺，必 403；
 *   `/admin/spus` 也不合适 —— 它只放给 admin，finance 进不去。
 *   这个接口不需要登录，且**只返回在售商品**，正合适：banner 指向已下架的商品，
 *   买家点过去就是 404。
 */
export const searchProducts = (params: { keyword?: string; limit?: number }) =>
  get<SpuList>('/search', { params: { ...params, sort: 'newest' } })

/** 公开的商品详情。用来把已配好的 `/products/<id>` 反查成商品名显示出来 */
export const fetchPublicSpu = (spuId: string) => get<SpuDetail>(`/spus/${spuId}`)

/**
 * 平台视角的商品详情。**任何店铺、任何状态**都看得到，`auditRemark` 也给。
 *
 * ★ 审核页必须用它：`fetchMySpu`（`/merchant/spus/{id}`）按店铺归属判权，
 *   平台运营没有店铺，必 403；`fetchPublicSpu` 只出已上架的，
 *   而审核队列里全是**待审核**的。
 */
export const fetchAdminSpu = (spuId: string) => get<SpuDetail>(`/admin/spus/${spuId}`)

/** `/skus/batch` 的行。详情里只用到"认出是哪件商品"这几个字段 */
export interface SkuBrief {
  id: string
  title: string
  specText: string
}

/**
 * 批量按 id 查 SKU。
 *
 * 运营看券 / 活动的「适用范围」时用：那里存的是 **SKU id**（算价就是按 SKU 匹配的），
 * 要显示成人看得懂的名字就得反查回来。
 *
 * ★ 只返回**在售**的：已经下架或删除的目标不会出现在结果里 —— 调用方要据此
 *   提示"这个目标已经不在了"，而不是静默少一行。
 */
export const fetchSkusByIds = (skuIds: string[]) =>
  post<SkuBrief[]>('/skus/batch', { skuIds })
