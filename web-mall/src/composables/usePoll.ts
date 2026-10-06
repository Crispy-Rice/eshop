import { onMounted, onUnmounted } from 'vue'

/**
 * 轻量轮询。仓库里原本**没有**轮询件（唯一一个 `setInterval` 是支付页的倒计时），
 * 客服会话与站内信角标都需要"过一会儿自己更新"，所以抽这一个。
 *
 * 三条行为是刻意的：
 *
 * - **挂载时立刻跑一次**（`immediate: false` 可关掉，给"初始值要等登录态恢复"
 *   的场景用 —— 否则会拿着未登录的 token 白跑一次）。
 * - **页面切到后台就停**：用户没在看，轮询只是白烧请求。
 * - **切回前台立刻补一次**再重新计时 —— 否则用户切回来要干等一整个周期。
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
