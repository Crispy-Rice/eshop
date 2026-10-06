import { defineStore } from 'pinia'
import { ref } from 'vue'

import { fetchAdminPendingCount, fetchMerchantPendingCount } from '@/api/support'
import { useAuthStore } from '@/stores/auth'

/**
 * 客服「待回复」角标。
 *
 * ★ 这个角标数的是**待回复的会话数**，不是站内信 —— 站内信是发给买家的，
 *   商家/运营这边一条都不会有，挂站内信角标会永远显示 0，等于没有。
 *
 * 身份决定端点：有店铺走商家端点（本店），平台运营走 admin 端点（全部店铺）。
 * 与 `App.vue` 里菜单的判据一致，也和后端两套守卫一致。
 */
export const useSupportStore = defineStore('support', () => {
  const pending = ref(0)

  async function refresh(): Promise<void> {
    const auth = useAuthStore()
    if (!auth.isLoggedIn) {
      pending.value = 0
      return
    }
    const user = auth.user
    try {
      if (user?.shopId) {
        pending.value = (await fetchMerchantPendingCount()).count
      } else if (user?.role === 'admin') {
        pending.value = (await fetchAdminPendingCount()).count
      } else {
        pending.value = 0
      }
    } catch {
      // 角标拉不到不影响使用，保持旧值即可
    }
  }

  function reset(): void {
    pending.value = 0
  }

  return { pending, refresh, reset }
})
