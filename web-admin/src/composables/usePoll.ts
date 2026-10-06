import { onMounted, onUnmounted } from 'vue'

/**
 * 轻量轮询（与商城端同一个件，两个应用各自持有 —— 它们是两个 bundle，
 * 不做跨包共享）。
 *
 * - **挂载时立刻跑一次**（`immediate: false` 可关掉，给"初始值要等登录态恢复"用）
 * - **页面切到后台就停**：用户没在看，轮询只是白烧请求
 * - **切回前台立刻补一次**再重新计时，否则要干等一整个周期
 */
export function usePoll(
  task: () => void | Promise<void>,
  intervalMs: number,
  options: { immediate?: boolean } = {},
): void {
  const immediate = options.immediate ?? true
  let timer: number | null = null

  function start(): void {
    if (timer !== null) return
    timer = window.setInterval(() => void task(), intervalMs)
  }

  function stop(): void {
    if (timer === null) return
    window.clearInterval(timer)
    timer = null
  }

  function onVisibilityChange(): void {
    if (document.visibilityState === 'visible') {
      void task()
      start()
    } else {
      stop()
    }
  }

  onMounted(() => {
    if (immediate) void task()
    start()
    document.addEventListener('visibilitychange', onVisibilityChange)
  })

  onUnmounted(() => {
    stop()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })
}
