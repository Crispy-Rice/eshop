/**
 * 皮肤注册表。
 *
 * 这里是皮肤的**元数据**，配色本身在 `assets/tokens.css` 的 `[data-theme=...]` 块里。
 * 两边靠 `id` 对齐：id 既是 CSS 选择器里的值，也是 localStorage 里存的值。
 *
 * 加一套新皮肤 = 在 tokens.css 加一个主题块 + 在下面这个对象里加一条，
 * 组件一行都不用改。
 */

export type ThemeId = 'neutral' | 'promo-618' | 'promo-double11' | 'promo-spring'

export interface ThemeDef {
  id: ThemeId
  /** 切换器上的短标签 */
  label: string
  /** 切换器上的色点，和该主题的强调色一致 */
  swatch: string
  /** 浏览器地址栏 / 移动端状态栏颜色 */
  themeColor: string
  /** 营销带主标题 */
  headline: string
  /** 营销带副文案 */
  slogan: string
  /** 营销带右上角徽标；中性主题没有大促，所以是可选的 */
  badge?: string
}

export const THEMES: Record<ThemeId, ThemeDef> = {
  neutral: {
    id: 'neutral',
    label: '默认',
    swatch: '#18181b',
    themeColor: '#f7f7f8',
    headline: '甄选好物',
    slogan: '中性货架 · 专注商品本身',
  },
  'promo-618': {
    id: 'promo-618',
    label: '618',
    swatch: '#e1251b',
    themeColor: '#e1251b',
    headline: '618 年中大促',
    slogan: '全场直降 · 跨店每满 300 减 50',
    badge: '618 大促',
  },
  'promo-double11': {
    id: 'promo-double11',
    label: '双 11',
    swatch: '#6a3df0',
    themeColor: '#6a3df0',
    headline: '双 11 购物狂欢',
    slogan: '定金膨胀 · 尾款立减',
    badge: '双 11',
  },
  'promo-spring': {
    id: 'promo-spring',
    label: '年货节',
    swatch: '#d97706',
    themeColor: '#d97706',
    headline: '年货节 · 囤货正当时',
    slogan: '满 199 减 30 · 全场顺丰包邮',
    badge: '年货节',
  },
}

export const DEFAULT_THEME: ThemeId = 'neutral'

export const THEME_LIST: ThemeDef[] = Object.values(THEMES)

export function isThemeId(value: unknown): value is ThemeId {
  return typeof value === 'string' && Object.hasOwn(THEMES, value)
}
