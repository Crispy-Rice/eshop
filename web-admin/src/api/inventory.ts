import { get, post } from './http'

/** 雪花 ID 在 JSON 里是字符串，见 docs/15 §1.3 */
export interface Warehouse {
  id: string
  name: string
  regionCode: string
  isDefault: boolean
  status: number
  createdAt: string
}

export interface StockItem {
  skuId: string
  warehouseId: string
  warehouseName: string
  skuCode: string
  specText: string
  spuTitle: string
  total: number
  available: number
  locked: number
  frozen: number
  updatedAt: string
}

export interface StockList {
  items: StockItem[]
  nextCursor: string | null
  hasMore: boolean
}

export interface StockFlow {
  id: number
  skuId: string
  warehouseId: string
  orderNo: string | null
  changeType: number
  changeTypeText: string
  num: number
  beforeQty: number
  afterQty: number
  operator: string | null
  remark: string | null
  createdAt: string
}

export interface StockFlowList {
  items: StockFlow[]
  nextCursor: string | null
  hasMore: boolean
}

export interface StockAdjustResult {
  skuId: string
  warehouseId: string
  beforeQty: number
  afterQty: number
  available: number
}

export interface StockListQuery {
  warehouseId?: string
  skuId?: string
  cursor?: string | null
  limit?: number
}

export function listWarehouses(): Promise<Warehouse[]> {
  return get<Warehouse[]>('/merchant/warehouses')
}

export function createWarehouse(name: string, regionCode = ''): Promise<Warehouse> {
  return post<Warehouse>('/merchant/warehouses', { name, regionCode })
}

export function listStock(query: StockListQuery = {}): Promise<StockList> {
  return get<StockList>('/merchant/inventory', { params: query })
}

export function listFlows(
  query: Omit<StockListQuery, 'limit'> = {},
): Promise<StockFlowList> {
  return get<StockFlowList>('/merchant/inventory/flows', { params: query })
}

/**
 * 手工调整库存。
 *
 * ★ 必须带 `Idempotency-Key`：后端缺这个头会直接返回 400，
 *   而且它是"同一笔调整被重试"时唯一的识别依据（docs/10）。
 */
export function adjustStock(
  payload: { skuId: string; warehouseId: string; delta: number; remark?: string },
  idempotencyKey: string,
): Promise<StockAdjustResult> {
  return post<StockAdjustResult>('/merchant/inventory/adjust', payload, {
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

/** 库存流水类型 → 中文，后端也会回 changeTypeText，这里用于筛选下拉 */
export const CHANGE_TYPES: { label: string; value: number }[] = [
  { label: '预占', value: 1 },
  { label: '实扣', value: 2 },
  { label: '回补', value: 3 },
  { label: '发货扣减', value: 4 },
  { label: '退货入库', value: 5 },
  { label: '手工调整', value: 6 },
  { label: '初始化', value: 7 },
]
