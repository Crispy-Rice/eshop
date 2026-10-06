import { ref, watch } from 'vue'

import { DEFAULT_THEME, THEMES, isThemeId, type ThemeId } from '@/theme/themes'

/**
 * ★ 这个 key 必须和 `index.html` 里那段内联脚本用的字符串一致。
 *   内联脚本负责首屏前设好 data-theme（避免刷新时闪一下默认主题），
 *   改这里就要同步改那边。
 *
 * ★ 它的角色是**缓存**，不是用户偏好：存的是"上次从后端读到的站点皮肤"。
 *   皮肤由**运营**在后台启用、全站生效（docs/17 §3），买家端没有切换器，
 *   所以这里没有任何用户写入的入口 —— 只有 `applyServerSkin`。
 */
const STORAGE_KEY = 'eshop.theme'

/** 切主题时挂到 <html> 上的过渡类，样式在 base.css */
const SWITCH_CLASS = 'theme-switching'
const SWITCH_MS = 280

function readCached(): ThemeId {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return isThemeId(raw) ? raw : DEFAULT_THEME
  } catch {
    // Safari 隐私模式下 localStorage 可能直接抛异常，降级到默认主题即可
    return DEFAULT_THEME
  }
}

/** 模块级单例：主题是全局状态，各处读到的必须是同一份 */
const current = ref<ThemeId>(readCached())

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
    /**
     * 应用**后端下发的站点皮肤**（应用外壳启动时调一次，见 App.vue 的 onMounted）。
     *
     * ★ 认不出的 id 一律忽略：后端上了新皮肤而前端还是旧版本时，回落到当前值
     *   比切到一个没有配色定义的主题安全（那会让整站颜色变成一堆未定义变量）。
     * ★ 与当前值相同时直接返回，不触发 watch —— 也就没有那 280ms 的过渡闪动。
     */
    applyServerSkin(skin: string): void {
      if (!isThemeId(skin) || skin === current.value) return
      current.value = skin
    },
  }
}
