import { onUnmounted, ref } from 'vue'

/**
 * 「提交问题 → 轮询到答案」的**短轮询**。
 *
 * ★ 刻意**不复用** `usePoll`：那是给导航角标这类**固定周期的后台循环**用的，
 *   `start` / `stop` 在闭包里，任务内部**无法自停**。这里要的恰恰相反 ——
 *   拿到答案（或者超时）就立刻停，多问一次就多一次无谓的请求。
 *
 * @param fetchOnce 拉一次最新状态；返回 `true` 表示"已经有着落了，别再问了"
 */
export function useAnswerPoll(
  fetchOnce: () => Promise<boolean>,
  { intervalMs = 1500, timeoutMs = 90_000 } = {},
) {
  const polling = ref(false)
  const timedOut = ref(false)
  let timer: ReturnType<typeof setTimeout> | null = null
  let deadline = 0

  function stop(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
    polling.value = false
  }

  function schedule(): void {
    timer = setTimeout(() => {
      void tick()
    }, intervalMs)
  }

  async function tick(): Promise<void> {
    try {
      if (await fetchOnce()) {
        stop()
        return
      }
    } catch {
      // 单次失败不终止轮询：网络抖一下不该让用户重新提问
    }
    if (Date.now() > deadline) {
      timedOut.value = true
      stop()
      return
    }
    schedule()
  }

  async function start(): Promise<void> {
    stop()
    timedOut.value = false
    polling.value = true
    deadline = Date.now() + timeoutMs
    // 先立刻拉一次：worker 可能已经答完了（尤其是很快的"答不了"）
    await tick()
  }

  onUnmounted(stop)

  return { polling, timedOut, start, stop }
}
