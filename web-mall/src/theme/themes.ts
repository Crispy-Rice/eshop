/**
 * 皮肤注册表。
 *
 * 这里是皮肤的**元数据**，配色本身在 `assets/tokens.css` 的 `[data-theme=...]` 块里。
 * 两边靠 `id` 对齐：id 既是 CSS 选择器里的值，也是 localStorage 里缓存的值。
 *
 * ★ 皮肤由**运营在后台启用、全站生效**（docs/17 §3），买家端没有切换器。
 *   所以这里不再有中文名、色点，也**没有营销文案**：
 *
 *   - 中文名只在后台的选择器上用，来自后端 `GET /api/site-theme` 下发的 options
 *     （皮肤清单只有后端 `promotion/models.py` 的 `SITE_SKINS` 一份，避免两处漂移）；
 *   - 营销文案原先硬编码在这里（"跨店每满 300 减 50"这类**优惠承诺**），
 *     现在顶部公告读的是**真实进行中的活动** —— 皮肤只管外观，不再替活动说话。
 *
 * 加一套新皮肤 = tokens.css 加一个主题块 + 这里加一条 + 后端 `SITE_SKINS` 登记一行。
 */

export type ThemeId = 'neutral' | 'promo-618' | 'promo-double11' | 'promo-spring'

export interface ThemeDef {
  id: ThemeId
  /** 浏览器地址栏 / 移动端状态栏颜色 */
  themeColor: string
}

export const THEMES: Record<ThemeId, ThemeDef> = {
  neutral: { id: 'neutral', themeColor: '#f7f7f8' },
  'promo-618': { id: 'promo-618', themeColor: '#e1251b' },
  'promo-double11': { id: 'promo-double11', themeColor: '#6a3df0' },
  'promo-spring': { id: 'promo-spring', themeColor: '#d97706' },
}

export const DEFAULT_THEME: ThemeId = 'neutral'

export function isThemeId(value: unknown): value is ThemeId {
  return typeof value === 'string' && Object.hasOwn(THEMES, value)
}
