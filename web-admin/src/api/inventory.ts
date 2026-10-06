import { get, post, put } from './http'

/** 雪花 ID 在 JSON 里是字符串，见 docs/15 §1.3 */
export interface RegionRule {
  id: string
  regionCode: string
  /** 1省 2市 3区。由码长推导，只作展示（匹配看的是码长本身） */
  regionLevel: number
}

export interface Warehouse {
  id: string
  name: string
  /** 最细一级的行政区划码（6 位）。下单按它的**前缀**匹配发货仓 */
  regionCode: string
  province: string
  city: string
  district: string
  detail: string
  contactName: string
  contactPhone: string
  /** 默认仓是路由的兜底；每店最多一个，且不允许停用 */
  isDefault: boolean
  /** 1启用 2停用 */
  status: number
  createdAt: string
  /** 这个仓覆盖的发货区划。列表接口一并给全 */
  rules: RegionRule[]
}

/** 建仓结果：多一个"顺手预建了多少条库存行"，提示商家去库存页填数量 */
export interface WarehouseCreated extends Warehouse {
  stockedSkus: number
}

export interface WarehousePayload {
  name: string
  regionCode?: string
  province?: string
  city?: string
  district?: string
  detail?: string
  contactName?: string
  contactPhone?: string
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

/**
 * 建仓。★ 后端会**顺手给该店全部 SKU 在这个仓补 0 库存行**，
 * 返回的 `stockedSkus` 就是补了多少条 —— 下一步该去库存页填数量。
 * 第一个建出来的仓自动成为默认仓。
 */
export function createWarehouse(payload: WarehousePayload): Promise<WarehouseCreated> {
  return post<WarehouseCreated>('/merchant/warehouses', payload)
}

/** 改仓（名称 / 地址 / 联系人）。部分更新：只提交要改的字段 */
export function updateWarehouse(
  id: string,
  payload: Partial<WarehousePayload>,
): Promise<Warehouse> {
  return put<Warehouse>(`/merchant/warehouses/${id}`, payload)
}

/** 设为默认仓。默认仓是路由的**兜底**：收货地址没命中任何区域规则时发它 */
export function setDefaultWarehouse(id: string): Promise<Warehouse> {
  return post<Warehouse>(`/merchant/warehouses/${id}/default`)
}

/** 启用 / 停用。默认仓不允许停用（停了就没有兜底仓了） */
export function setWarehouseStatus(id: string, status: number): Promise<Warehouse> {
  return post<Warehouse>(`/merchant/warehouses/${id}/status`, { status })
}

/**
 * 设置这个仓覆盖的发货区划（**整体替换**）。
 *
 * ★ 一个区划只能由一个仓发货：提交了已被别的仓占用的地方会拿到 400，
 *   文案里点名"是哪个地区、现在归哪个仓"。
 */
export function replaceWarehouseRegions(id: string, rules: string[]): Promise<RegionRule[]> {
  return put<RegionRule[]>(`/merchant/warehouses/${id}/regions`, { rules })
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
