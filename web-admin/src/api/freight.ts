import { get, post, put } from './http'

/** 运费模板。金额单位是分，重量单位是克。 */
export interface FreightTemplate {
  id: string
  name: string
  /** 1按重量 2按件数 3按体积 */
  chargeType: number
  firstUnit: number
  firstPrice: number
  addUnit: number
  addPrice: number
  freeShipping: boolean
  freeThreshold: number
  freeNum: number
  /** 1同仓取首重最高 2各算各的 */
  mergeType: number
  status: number
  /**
   * 店铺默认模板（一店只有一条）。
   *
   * 没绑定模板的规格算运费时**回落到它** —— 绑定是逐条 SKU 的，新发布的商品
   * 天然在模板之外，没有这条回落就只能在买家结算时才发现算不出运费。
   */
  isDefault: boolean
  /** 影响面：这个模板绑了多少 SKU */
  boundSkuCount: number
}

export interface FreightTemplateInput {
  name: string
  chargeType: number
  firstUnit: number
  firstPrice: number
  addUnit: number
  addPrice: number
  freeShipping: boolean
  freeThreshold: number
  freeNum: number
  mergeType: number
}

export interface FreightRegionRule {
  id: string
  regionCode: string
  regionLevel: number
  firstUnit: number
  firstPrice: number
  addUnit: number
  addPrice: number
  freeShipping: boolean
  enabled: boolean
  priority: number
}

export interface RegionRuleInput {
  regionCode: string
  regionLevel: number
  firstUnit: number
  firstPrice: number
  addUnit: number
  addPrice: number
  freeShipping: boolean
  enabled: boolean
  priority: number
}

export interface ExcludeRegion {
  id: string
  regionCode: string
  reason: string | null
}

export interface TemplateUpdateResult {
  template: FreightTemplate
  affectedSkuCount: number
  notice: string
}

export function listTemplates(): Promise<FreightTemplate[]> {
  return get<FreightTemplate[]>('/merchant/freight/templates')
}

export function createTemplate(payload: FreightTemplateInput): Promise<FreightTemplate> {
  return post<FreightTemplate>('/merchant/freight/templates', payload)
}

/**
 * 修改模板。响应里带着**受影响的 SKU 数**和提示 ——
 * 改动立即对新建订单生效，已下单订单用的是下单时的快照。
 */
export function updateTemplate(
  templateId: string,
  payload: FreightTemplateInput & { status: number },
): Promise<TemplateUpdateResult> {
  return put<TemplateUpdateResult>(`/merchant/freight/templates/${templateId}`, payload)
}

export function listRegionRules(templateId: string): Promise<FreightRegionRule[]> {
  return get<FreightRegionRule[]>(`/merchant/freight/templates/${templateId}/regions`)
}

/**
 * 设为店铺默认模板。
 *
 * 一个店只有一条，设新的会顶掉旧的。默认模板是**没绑定模板的规格的兜底** ——
 * 一条默认都没有时，新发布的商品算不出运费，也就上不了架。
 */
export function setDefaultTemplate(templateId: string): Promise<FreightTemplate> {
  return put<FreightTemplate>(`/merchant/freight/templates/${templateId}/default`, {})
}

/** 整体替换区域规则。后端会校验"必须保留一条全国默认"。 */
export function replaceRegionRules(
  templateId: string,
  rules: RegionRuleInput[],
): Promise<FreightRegionRule[]> {
  return put<FreightRegionRule[]>(`/merchant/freight/templates/${templateId}/regions`, { rules })
}

export function listExcludeRegions(templateId: string): Promise<ExcludeRegion[]> {
  return get<ExcludeRegion[]>(`/merchant/freight/templates/${templateId}/exclude`)
}

export function replaceExcludeRegions(
  templateId: string,
  excludes: { regionCode: string; reason?: string }[],
): Promise<ExcludeRegion[]> {
  return put<ExcludeRegion[]>(`/merchant/freight/templates/${templateId}/exclude`, { excludes })
}

export function bindSku(payload: {
  skuId: string
  templateId: string
  warehouseId: string
  priority?: number
}): Promise<null> {
  return post<null>('/merchant/freight/bind', payload)
}

/** 一条 SKU↔模板 的绑定。显示名由后端拼好，前端不用再join 商品数据。 */
export interface SkuBind {
  skuId: string
  skuCode: string
  spuTitle: string
  specText: string
  warehouseId: string
  warehouseName: string
  /** 同 SKU 多模板时越大越优先 */
  priority: number
  enabled: boolean
}

export function fetchTemplateBinds(templateId: string): Promise<SkuBind[]> {
  return get<SkuBind[]>(`/merchant/freight/templates/${templateId}/binds`)
}

export const CHARGE_TYPE_TEXT: Record<number, string> = {
  1: '按重量',
  2: '按件数',
  3: '按体积',
}
