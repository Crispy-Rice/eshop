import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as authApi from '@/api/auth'
import type { UserInfo } from '@/api/auth'
import { clearTokens, getRefreshToken, setTokens } from '@/api/http'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  const loading = ref(false)

  const isLoggedIn = computed(() => user.value !== null)

  function applyTokens(tokens: authApi.TokenResponse): void {
    setTokens(tokens.accessToken, tokens.refreshToken)
  }

  async function login(phone: string, password: string): Promise<void> {
    loading.value = true
    try {
      applyTokens(await authApi.login(phone, password))
      user.value = await authApi.fetchMe()
    } finally {
      loading.value = false
    }
  }

  async function register(phone: string, password: string, nickname?: string): Promise<void> {
    loading.value = true
    try {
      applyTokens(await authApi.register(phone, password, nickname))
      user.value = await authApi.fetchMe()
    } finally {
      loading.value = false
    }
  }

  /** 应用启动 / 刷新页面时调用：有令牌就拉一次用户信息，失败视为未登录 */
  async function restore(): Promise<void> {
    if (!getRefreshToken()) {
      user.value = null
      return
    }
    try {
      user.value = await authApi.fetchMe()
    } catch {
      user.value = null
    }
  }

  async function logout(): Promise<void> {
    const refreshToken = getRefreshToken()
    try {
      if (refreshToken) await authApi.logout(refreshToken)
    } catch {
      // 登出失败也要清本地状态，否则用户会卡在"已登录但用不了"的状态
    } finally {
      clearTokens()
      user.value = null
    }
  }

  /** 令牌失效时由 http 层回调，只清本地状态 */
  function clearLocal(): void {
    clearTokens()
    user.value = null
  }

  return { user, loading, isLoggedIn, login, register, restore, logout, clearLocal }
})
