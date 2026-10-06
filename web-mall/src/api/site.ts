import { get } from './http'

/** 皮肤的可选项。``label`` 只给后台的选择器用。 */
export interface SkinOption {
  value: string
  label: string
}

export interface SiteTheme {
  skin: string
  options: SkinOption[]
}

/**
 * 当前站点皮肤。**公开接口，不需要登录**。
 *
 * ★ 皮肤由**运营**在后台启用、全站生效，所以这里没有"用户偏好"这回事 ——
 *   应用外壳启动时读一次并应用（见 `App.vue` 的 `onMounted` 与 `useTheme`）。
 * ★ 认不出的 skin 由 `useTheme.applyServerSkin` 忽略（回落到当前值），
 *   前端不为它做额外兜底。
 */
export const fetchSiteTheme = () => get<SiteTheme>('/site-theme')

/** 进行中的平台活动。**公开接口**。 */
export interface ActiveActivity {
  id: string
  name: string
  /** 0单品 1店铺 2平台 —— 公告只会有平台级 */
  level: number
  endAt: string
}

export interface ActivePromotions {
  items: ActiveActivity[]
  /**
   * 进行中的平台级活动**总数**，可能大于 `items.length`。
   *
   * ★ 拿这个数去算"还有 N 个活动进行中"，**不要**用 `items.length` ——
   *   列表有上限，用截断后的长度会凭空少报几个（而且还看不到它们）。
   */
  total: number
}

/**
 * 顶部公告的数据源：**正在进行的平台级活动**（最快结束的排最前）。
 *
 * 一条都没有就返回空 items —— 调用方据此**整条不渲染**，
 * 而不是显示一句"暂无活动"的空话。
 */
export const fetchActiveActivities = () => get<ActivePromotions>('/promotions/active')

/** 平台客服联系方式。三项都可能为 `null` = **未配置**。 */
export interface SiteContact {
  serviceEmail: string | null
  servicePhone: string | null
  serviceHours: string | null
}

/**
 * 平台联系方式。**公开接口，不需要登录**。
 *
 * ★ 必须是公开的：需要联系平台的典型场景（账号被冻结、登不进来）恰恰是用户
 *   **还没登录**的时候 —— 藏在「我的」里的联系方式对被封的人等于不存在。
 * ★ 三项都为 null 是合法状态（运营还没填）：调用方据此**只展示真正有值的项**，
 *   一项都没有就只说事实，不承诺一个不存在的渠道。
 */
export const fetchSiteContact = () => get<SiteContact>('/site-contact')
