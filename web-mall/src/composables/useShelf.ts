import { ref } from 'vue'

/**
 * 货架的页大小：**网格量出列数，这里落成 `limit`，并负责拉第一屏**。
 *
 * 为什么需要它：页大小 = 列数 × 行数，而列数是 CSS 网格自己算出来的
 * （`repeat(auto-fill, minmax(200px, 1fr))`：宽屏 5 列、中等 4 列、窄屏 3 列），
 * 只有网格量得到。量出来的值要变成请求里的 `limit`，于是"谁先谁后"很讲究：
 *
 * ★ **网格比页面先挂载**，它在自己的 `onMounted` 里 emit，所以这个值在页面第一次
 *   发请求**之前**就位。页面因此**不要在 `onMounted` 里自己拉第一屏** —— 那会发两次。
 *   只在拿不准时调 `ensureLoaded()`。
 * ★ 值**相等也要保证拉过一次**：宽屏正好 4 列时算出来就是初始值 12，
 *   只比较值会让整个页面**一屏都不拉**（白屏，且不报错）。
 * ★ 连续 resize 只触发一次：列数没变时直接返回，不重拉。
 */
export function useShelf(load: (reset: boolean) => void, fallback = 12) {
  const pageSize = ref(fallback)
  let applied = false

  function onGridPageSize(size: number): void {
    const changed = size !== pageSize.value
    pageSize.value = size
    if (changed || !applied) {
      applied = true
      load(true)
    }
  }

  /** 兜底：网格没量出列数（元素缺失 / 渲染异常）时，至少把第一屏拉出来。重复调用无害。 */
  function ensureLoaded(): void {
    if (applied) return
    applied = true
    load(true)
  }

  return { pageSize, onGridPageSize, ensureLoaded }
}
