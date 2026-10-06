import { defineStore } from 'pinia'
import { ref } from 'vue'

import { fetchUnreadCount } from '@/api/notify'
import { useAuthStore } from '@/stores/auth'

/**
 * 站内信角标（导航栏「消息」上的那个数）。
 *
 * 与 `useCartStore` 同构：**只维护一个数字**，不缓存消息列表 —— 列表在消息页
 * 实时拉，缓存它就要处理一致性问题，得不偿失。
 *
 * ★ 客服回复**必然**产生一条站内信（后端同事务直写），所以这一个角标就够了，
 *   「客服」入口上不再挂第二个 —— 两个角标说同一件事只会让用户困惑。
 */
export const useNotifyStore = defineStore('notify', () => {
  const unread = ref(0)

  async function refresh(): Promise<void> {
    const auth = useAuthStore()
    if (!auth.isLoggedIn) {
      unread.value = 0
      return
    }
    try {
      unread.value = (await fetchUnreadCount()).count
    } catch {
      // 角标拉不到不影响浏览，保持旧值即可
    }
  }

  function reset(): void {
    unread.value = 0
  }

  return { unread, refresh, reset }
})
