/**
 * 类目相关的接口。
 *
 * 公开的 `GET /categories` 商城和商家都在用；带 `/admin` 前缀的四条是运营
 * 维护类目树用的（改名 / 排序 / 启停 / 移动 / 删除）。
 */

import { del, get, post, put } from './http'

/** 类目树节点。公开接口只返回启用的类目。 */
export interface Category {
  id: string
  parentId: string | null
  name: string
  level: number
  sort: number
  children: Category[]
}

/**
 * 管理端的类目节点：比公开树多出停用状态和商品数。
 *
 * 商品数（spuCount）只算未删除的商品，和"有商品就不让删"用的是同一个口径 ——
 * 运营看到它是 0 才敢点删除。
 */
export interface AdminCategory extends Category {
  status: number
  spuCount: number
  children: AdminCategory[]
}

/** 类目最深三级，与后端 CHECK 约束和 `MAX_CATEGORY_LEVEL` 一致。 */
export const MAX_CATEGORY_LEVEL = 3

// ---------- 公开 ----------

/** 类目树（只含启用的）。 */
export const fetchCategoryTree = () => get<Category[]>('/categories')

// ---------- 运营 ----------

/** 完整类目树，**含停用节点** —— 运营要能看见并重新启用它们。 */
export const fetchAdminCategoryTree = () => get<AdminCategory[]>('/admin/categories')

export function createCategory(name: string, parentId?: string | null, sort = 0): Promise<Category> {
  return post<Category>('/admin/categories', { name, parentId: parentId ?? null, sort })
}

export interface CategoryUpdateInput {
  name?: string
  sort?: number
  /** 1 启用 / 2 停用 */
  status?: number
}

export function updateCategory(id: string, payload: CategoryUpdateInput): Promise<Category> {
  return put<Category>(`/admin/categories/${id}`, payload)
}

/** 移动类目（连同整棵子树）。`parentId` 传 null 表示升为一级类目。 */
export function moveCategory(id: string, parentId: string | null): Promise<Category> {
  return post<Category>(`/admin/categories/${id}/move`, { parentId })
}

export function deleteCategory(id: string): Promise<null> {
  return del<null>(`/admin/categories/${id}`)
}
