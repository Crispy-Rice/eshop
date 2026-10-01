import { defineStore } from 'pinia'
import { ref } from 'vue'

import { fetchCartCount } from '@/api/cart'
import { useAuthStore } from '@/stores/auth'

/**
 * 购物车角标。
 *
 * 只维护"有几种 SKU"这一个数字，不缓存购物车内容——内容在购物车页实时拉，
 * 缓存它就要处理一致性问题，得不偿失。
 */
export const useCartStore = defineStore('cart', () => {
  const count = ref(0)

  /** 刷新角标。未登录时归零（未登录没有购物车）。 */
  async function refresh(): Promise<void> {
    const auth = useAuthStore()
    if (!auth.isLoggedIn) {
      count.value = 0
      return
    }
    try {
      count.value = (await fetchCartCount()).count
    } catch {
      // 角标拉不到不影响浏览，保持旧值即可
    }
  }

  function reset(): void {
    count.value = 0
  }

  return { count, refresh, reset }
})
