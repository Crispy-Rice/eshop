import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as authApi from '@/api/auth'
import type { UserInfo } from '@/api/auth'
import type { AccountBlock } from '@/api/errors'
import { clearTokens, getRefreshToken, setTokens } from '@/api/http'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  const loading = ref(false)

  /**
   * 账号被冻结 / 已注销。
   *
   * ★ 它**不是**会话状态，而是"为什么没有会话"——所以单独放一个 ref，不塞进 user。
   *   两个来源都会写它：登录被拒，以及**已登录的人被封后令牌续不了期**
   *   （见 `api/http.ts` 的刷新分支）。两者都要让登录页给出同一份说明。
   * ★ 有意**不**在 `clearLocal()` 里清它 —— 那个函数是"令牌没了"的通用清理，
   *   而封锁恰恰要在令牌被清掉之后仍然显示。
   */
  const blocked = ref<AccountBlock | null>(null)

  function setBlocked(next: AccountBlock | null): void {
    blocked.value = next
  }

  /** 换一个手机号、或重新登录成功时调用：上一条结论不再适用。 */
  function clearBlocked(): void {
    blocked.value = null
  }

  const isLoggedIn = computed(() => user.value !== null)

  function applyTokens(tokens: authApi.TokenResponse): void {
    setTokens(tokens.accessToken, tokens.refreshToken)
  }

  async function login(phone: string, password: string): Promise<void> {
    loading.value = true
    try {
      applyTokens(await authApi.login(phone, password))
      user.value = await authApi.fetchMe()
      // 进得来就说明没被封锁 —— 上一条结论（可能是别的账号的）到此为止
      clearBlocked()
    } finally {
      loading.value = false
    }
  }

  async function register(phone: string, password: string, nickname?: string): Promise<void> {
    loading.value = true
    try {
      applyTokens(await authApi.register(phone, password, nickname))
      user.value = await authApi.fetchMe()
      clearBlocked()
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
      clearBlocked()
    }
  }

  /** 令牌失效时由 http 层回调，只清本地状态 */
  function clearLocal(): void {
    clearTokens()
    user.value = null
  }

  /**
   * 改密码。成功后把本地令牌换成后端补发的那一对。
   *
   * ★ 该账号在**其它设备**上的 refresh token 已被吊销，当前这台不用重新登录 ——
   *   这是"改完密码自己也被踢出去"这种别扭体验的由来，别改成重新登录。
   */
  async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
    applyTokens(await authApi.changePassword(oldPassword, newPassword))
  }

  /** 注销账号。不可恢复，所以成功后直接清掉本地登录态。 */
  async function deactivate(password: string): Promise<void> {
    await authApi.deactivateAccount(password)
    clearLocal()
  }

  return {
    user,
    loading,
    blocked,
    isLoggedIn,
    login,
    register,
    restore,
    logout,
    clearLocal,
    setBlocked,
    clearBlocked,
    changePassword,
    deactivate,
  }
})
