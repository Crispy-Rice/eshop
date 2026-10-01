import { computed, ref, watch } from 'vue'

import { DEFAULT_THEME, THEMES, isThemeId, THEME_LIST, type ThemeId } from '@/theme/themes'

/**
 * ★ 这个 key 必须和 `index.html` 里那段内联脚本用的字符串一致。
 *   内联脚本负责首屏前设好 data-theme（避免刷新时闪一下默认主题），
 *   改这里就要同步改那边。
 */
const STORAGE_KEY = 'eshop.theme'

/** 切主题时挂到 <html> 上的过渡类，样式在 base.css */
const SWITCH_CLASS = 'theme-switching'
const SWITCH_MS = 280

function readStored(): ThemeId {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return isThemeId(raw) ? raw : DEFAULT_THEME
  } catch {
    // Safari 隐私模式下 localStorage 可能直接抛异常，降级到默认主题即可
    return DEFAULT_THEME
  }
}

/** 模块级单例：主题是全局状态，各处读到的必须是同一份 */
const current = ref<ThemeId>(readStored())

function apply(id: ThemeId): void {
  document.documentElement.dataset.theme = id
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', THEMES[id].themeColor)
}

// 首屏那次由 index.html 的内联脚本完成，这里补一次是为了防止
// 内联脚本被 CSP 之类挡掉时主题没生效。
apply(current.value)

watch(current, (id) => {
  const el = document.documentElement
  // 先挂过渡类再改主题，色值变化就会走一段短过渡；
  // 过渡类只在切换的 ~280ms 内存在，不影响组件自身的动效
  el.classList.add(SWITCH_CLASS)
  apply(id)
  try {
    localStorage.setItem(STORAGE_KEY, id)
  } catch {
    // 存不下就算了，至少当前会话是对的
  }
  window.setTimeout(() => el.classList.remove(SWITCH_CLASS), SWITCH_MS)
})

export function useTheme() {
  return {
    /** 当前皮肤 id */
    theme: current,
    /** 当前皮肤的完整元数据，营销带和切换器都读它 */
    themeDef: computed(() => THEMES[current.value]),
    /** 全部皮肤，供切换器渲染 */
    themes: THEME_LIST,
    setTheme(id: ThemeId): void {
      current.value = id
    },
  }
}
