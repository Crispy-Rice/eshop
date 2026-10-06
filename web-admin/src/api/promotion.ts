import { get, post, put } from './http'

// ============================================================
// 后端契约（取值必须与 backend/app/modules/promotion/models.py 对齐）
// ============================================================

/** 券类型 */
export const COUPON_TYPE_THRESHOLD = 1
export const COUPON_TYPE_DISCOUNT = 2
export const COUPON_TYPE_NO_THRESHOLD = 3
export const COUPON_TYPE_EXCHANGE = 4
export const COUPON_TYPE_FREIGHT = 5

export const COUPON_TYPE_TEXT: Record<number, string> = {
  [COUPON_TYPE_THRESHOLD]: '满减券',
  [COUPON_TYPE_DISCOUNT]: '折扣券',
  [COUPON_TYPE_NO_THRESHOLD]: '无门槛券',
  [COUPON_TYPE_EXCHANGE]: '兑换券',
  [COUPON_TYPE_FREIGHT]: '运费券',
}

/** 有效期类型 */
export const VALID_FIXED = 1
export const VALID_DAYS_AFTER = 2

/** 适用范围 */
export const SCOPE_ALL = 1
export const SCOPE_SKU = 2
export const SCOPE_CATEGORY = 3
export const SCOPE_SHOP = 4

export const SCOPE_TYPE_TEXT: Record<number, string> = {
  [SCOPE_ALL]: '全场',
  [SCOPE_SKU]: '指定商品',
  [SCOPE_CATEGORY]: '指定类目',
  [SCOPE_SHOP]: '指定店铺',
}

/** 券模板状态 */
export const TPL_NOT_STARTED = 1
export const TPL_ONGOING = 2
export const TPL_ENDED = 3
export const TPL_VOID = 4

/** 活动层级。**顺序是语义的一部分**，0 是最内层 */
export const LEVEL_ITEM = 0
export const LEVEL_SHOP = 1
export const LEVEL_PLATFORM = 2

export const LEVEL_TEXT: Record<number, string> = {
  [LEVEL_ITEM]: '单品级',
  [LEVEL_SHOP]: '店铺级',
  [LEVEL_PLATFORM]: '平台级',
}

/** 活动优惠类型。这些字符串是叠加规则矩阵的键，不能改 */
export const PROMO_TYPE_BY_LEVEL: Record<number, string> = {
  [LEVEL_ITEM]: 'PROMO_ITEM',
  [LEVEL_SHOP]: 'PROMO_ORDER_SHOP',
  [LEVEL_PLATFORM]: 'PROMO_ORDER_PLATFORM',
}

export const PROMO_TYPE_TEXT: Record<string, string> = {
  PROMO_ITEM: '单品促销',
  PROMO_ORDER_SHOP: '店铺活动',
  PROMO_ORDER_PLATFORM: '平台活动',
}

/** 计算方式 */
export const CALC_DIRECT = 1
export const CALC_RATE = 2
export const CALC_FIXED = 3

export const CALC_TYPE_TEXT: Record<number, string> = {
  [CALC_DIRECT]: '直降',
  [CALC_RATE]: '折扣',
  [CALC_FIXED]: '特价',
}

// ============================================================
// 类型
// ============================================================
export interface AdminCouponTemplate {
  id: string
  shopId: string
  name: string
  type: number
  typeText: string
  getType: number
  discountValue: number
  maxDiscount: number
  threshold: number
  totalCount: number
  issuedCount: number
  usedCount: number
  perUserLimit: number
  validType: number
  validStart: string | null
  validEnd: string | null
  validDays: number | null
  scopeType: number
  /** ★ 字符串数组：雪花 ID 超 2^53，用 number 会丢精度 */
  scopeValue: string[] | null
  status: number
  statusText: string
  createdAt: string
}

export interface AdminPromoActivity {
  id: string
  name: string
  level: number
  levelText: string
  type: string
  typeText: string
  calcType: number
  calcTypeText: string
  discountValue: number
  maxDiscount: number
  threshold: number
  shopId: string
  scopeType: number
  scopeValue: string[] | null
  startAt: string
  endAt: string
  priority: number
  status: number
  statusText: string
  createdAt: string
}

export interface CursorPage<T> {
  items: T[]
  nextCursor: string | null
  hasMore: boolean
}

export interface CouponTemplateInput {
  name: string
  shopId?: string
  type: number
  discountValue: number
  maxDiscount?: number
  threshold?: number
  totalCount: number
  perUserLimit?: number
  validType: number
  validStart?: string | null
  validEnd?: string | null
  validDays?: number | null
  scopeType?: number
  /**
   * ★ **字符串**数组，不是 number[]：里面是雪花 ID，超过 2^53，
   * `Number()` 会静默截断成另一个数 —— 存进去之后算价时
   * `item.skuId in scopeValue` 永远为假，券看着正常却一辈子不生效。
   */
  scopeValue?: string[] | null
}

export interface PromoActivityInput {
  name: string
  level: number
  type: string
  calcType: number
  discountValue: number
  maxDiscount?: number
  threshold?: number
  shopId?: string
  scopeType?: number
  /** 同券那份：**字符串**数组（雪花 ID 超过 2^53，`Number()` 会静默截断） */
  scopeValue?: string[] | null
  startAt: string
  endAt: string
  priority?: number
}

/** 补发的券。后端**只支持单个收件人**，没有批量。 */
export interface IssuedCoupon {
  id: string
  code: string
  name: string
  validStart: string
  validEnd: string
}

/**
 * 按手机号定位到的用户 —— 定向发券前用来确认"发给谁"。
 *
 * ★ 运营拿不到用户的雪花 ID，所以发券入口必须能先把手机号换成 userId；
 *   ``nickname`` + ``phoneMasked`` 是给运营核对"没找错人"的。
 */
export interface UserLookup {
  userId: string
  nickname: string
  phoneMasked: string
}

/** 一条补发记录。**一行 = 一张券** —— 审计到这个粒度，能顺着券码查下去。 */
export interface CouponIssueRecord {
  id: string
  createdAt: string
  templateName: string
  code: string
  /** 操作人昵称；解析不出会是 `admin:123` 这种原始标识，不会是空白 */
  operatorName: string
  userId: string
  nickname: string
  phoneMasked: string
  remark: string | null
}

export interface CouponIssueSummary {
  total: number
  byOperator: { operatorName: string; count: number }[]
}

export interface CouponIssuePage {
  items: CouponIssueRecord[]
  hasMore: boolean
  nextCursor: string | null
  summary: CouponIssueSummary
}

// ============================================================
// 接口
// ============================================================
export function listCouponTemplates(params: {
  status?: number
  cursor?: string
  limit?: number
}): Promise<CursorPage<AdminCouponTemplate>> {
  return get<CursorPage<AdminCouponTemplate>>('/admin/coupons/templates', { params })
}

export function createCouponTemplate(
  payload: CouponTemplateInput,
): Promise<AdminCouponTemplate> {
  return post<AdminCouponTemplate>('/admin/coupons/templates', payload)
}

/** 客服补发。不占活动额度（issuedCount 不变），所以列表里看不到"已发"增加。 */
export function issueCoupon(payload: {
  templateId: string
  userId: string
  count?: number
}): Promise<IssuedCoupon[]> {
  return post<IssuedCoupon[]>('/admin/coupons/issue', payload)
}

/**
 * 把运营手上的手机号换成一个 ``userId``（定向发券用）。
 *
 * 查不到会 404 并给出明确文案，而不是返回空 —— 免得运营拿着一个空值继续往下发。
 */
export function lookupUserByPhone(phone: string): Promise<UserLookup> {
  return get<UserLookup>('/admin/users/lookup', { params: { phone } })
}

/**
 * 客服补发的历史记录（含最近 24 小时的汇总）。
 *
 * ★ 这是约束运营发券的**主要手段**：补发不占活动额度，"活动库存"根本不构成
 *   约束 —— 能被看见才是最有效的那道。后端另有两道配额闸（单用户单模板、
 *   单运营 24 小时）。
 */
export function listCouponIssues(params: {
  cursor?: string | null
  limit?: number
}): Promise<CouponIssuePage> {
  return get<CouponIssuePage>('/admin/coupons/issues', { params })
}

export function listPromoActivities(params: {
  status?: number
  cursor?: string
  limit?: number
}): Promise<CursorPage<AdminPromoActivity>> {
  return get<CursorPage<AdminPromoActivity>>('/admin/promotions', { params })
}

export function createPromoActivity(
  payload: PromoActivityInput,
): Promise<{ id: string; name: string }> {
  return post<{ id: string; name: string }>('/admin/promotions', payload)
}

/**
 * 作废一个促销活动。
 *
 * ★ **下线用"作废"而不是删除**：活动一旦产生过订单，订单里的优惠快照就指着它 ——
 *   删行会让那些快照变成查不到来源的孤儿。作废是改状态，算价当场不再带它，
 *   历史订单照旧。后端会拒掉重复作废（两个运营都点了，第二个得知道自己白点了）。
 */
export function voidPromoActivity(activityId: string): Promise<null> {
  return post<null>(`/admin/promotions/${activityId}/void`)
}

/** 作废一个券模板。**已经发出去的券不受影响** —— 券有自己的一生，只是不再能被领取。 */
export function voidCouponTemplate(templateId: string): Promise<null> {
  return post<null>(`/admin/coupons/templates/${templateId}/void`)
}

/** 建券挑「指定店铺」时的候选项。 */
export interface ShopOption {
  id: string
  name: string
  /** 1 正常 / 2 关闭 / 3 审核中 —— 界面要能标出「已关闭」，给关掉的店建券等于白发 */
  status: number
}

/**
 * 按店名搜店铺（建券的「指定店铺」用）。
 *
 * ★ 券里存的是 shopId，而运营手上只有**店名** —— 跟"按手机号找用户"是同一类问题。
 *   不分页：这是选择器不是列表页，多打两个字收窄比翻页快。
 */
export function searchShops(keyword: string): Promise<ShopOption[]> {
  return get<ShopOption[]>('/admin/shops', {
    params: { keyword: keyword || undefined, limit: 30 },
  })
}

/**
 * 按 id 批量取店铺 —— `searchShops` 的反方向。
 *
 * 已经存下来的「指定店铺」范围里只有 id，要看"这条到底限了哪几家店"
 * 就得按 id 换回名字。
 */
export function fetchShopsByIds(ids: string[]): Promise<ShopOption[]> {
  return get<ShopOption[]>('/admin/shops', { params: { ids: ids.join(',') } })
}

// ============================================================
// 站点设置（全站皮肤 + 客服联系方式，各一张单行配置）
// ============================================================
export interface SkinOption {
  value: string
  label: string
}

export interface SiteTheme {
  skin: string
  options: SkinOption[]
}

/**
 * 当前站点皮肤 + **可选清单**。
 *
 * ★ 这个接口是**公开**的（买家端启动时也读它），后台只是复用它拿 `options` ——
 *   皮肤清单因此只有**一份**（后端 `promotion/models.py` 的 `SITE_SKINS`），
 *   后台不必再维护一个数组，也就不会出现"后台能选、买家端不认"的漂移。
 */
export function fetchSiteTheme(): Promise<SiteTheme> {
  return get<SiteTheme>('/site-theme')
}

/** 切换全站皮肤。保存后立刻对所有人生效（买家端下次加载就读到）。 */
export function updateSiteTheme(skin: string): Promise<SiteTheme> {
  return put<SiteTheme>('/admin/site-theme', { skin })
}

// ============================================================
// 客服联系方式（全站单行配置）
// ============================================================
/** 三项都可能为 `null` = **未配置**。 */
export interface SiteContact {
  serviceEmail: string | null
  servicePhone: string | null
  serviceHours: string | null
}

/**
 * 平台客服联系方式。**公开接口**，后台只是复用它读现有值。
 *
 * ★ 它在买家端出现在**登录页**与**账号被冻结的提示**上 —— 这两处正是"人还没登进来"
 *   的场景。所以这一项**不是可选装饰**：不填，被封的用户就真的没有联系入口。
 */
export function fetchSiteContact(): Promise<SiteContact> {
  return get<SiteContact>('/site-contact')
}

/**
 * 存客服联系方式。三项**一起提交**（整行覆盖）—— 传空串 = 取消配置那一项。
 *
 * ★ 返回的是**归一化之后**的值（空串 / 纯空格都变 `null`）。调用方拿它回填表单，
 *   界面上显示的和服务端存的就是同一个东西，不会出现"表单里还留着空格、库里已经清了"。
 */
export function updateSiteContact(contact: SiteContact): Promise<SiteContact> {
  return put<SiteContact>('/admin/site-contact', contact)
}
