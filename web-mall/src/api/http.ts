import axios, { AxiosError, type AxiosRequestConfig } from 'axios'

import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY } from '@/utils/storageKeys'

import { BizError, ErrorCode, type ApiResponse } from './errors'

/**
 * 令牌存取。
 *
 * 第一期 access token 与 refresh token 都放 localStorage（简单、够用）。
 * ★ 开启 HTTPS 后应把 refresh token 改存 HttpOnly Cookie
 *   （docs/15-api-and-errors.md §1.1）。
 * ★ 键名带本应用的命名空间 —— 商城与后台同源，共用键就等于共用一个登录态
 *   （见 `utils/storageKeys.ts`）。
 */

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY)
}

export function setTokens(accessToken: string, refreshToken: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, accessToken)
  localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken)
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
}

export const http = axios.create({
  baseURL: '/api',
  timeout: 10_000,
  headers: { 'Content-Type': 'application/json' },
})

http.interceptors.request.use((config) => {
  const token = getAccessToken()
  if (token) config.headers.set('Authorization', `Bearer ${token}`)
  return config
})

/** 令牌过期时跳登录页——由应用启动时注册，避免这里硬依赖 router */
let onUnauthorized: ((blocked?: BizError) => void) | null = null
export function setUnauthorizedHandler(handler: (blocked?: BizError) => void): void {
  onUnauthorized = handler
}

type RetriableConfig = AxiosRequestConfig & { _retried?: boolean }

/** 刷新结果。失败时若原因**是账号本身**（被冻结 / 已注销），把那个异常带出来。 */
type RefreshResult = { ok: true } | { ok: false; blocked?: BizError }

/**
 * 刷新令牌。多个请求同时 401 时共用同一次刷新，避免并发刷新把
 * refresh token 轮换掉（我们的刷新是**轮换式**的，旧的一次只能用一次）。
 */
let refreshing: Promise<RefreshResult> | null = null

async function refreshAccessToken(): Promise<RefreshResult> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) return { ok: false }

  refreshing ??= (async (): Promise<RefreshResult> => {
    try {
      const resp = await axios.post<ApiResponse<{ accessToken: string; refreshToken: string }>>(
        '/api/auth/refresh',
        { refreshToken },
        { headers: { 'Content-Type': 'application/json' } },
      )
      const data = resp.data.data
      if (!data) return { ok: false }
      setTokens(data.accessToken, data.refreshToken)
      return { ok: true }
    } catch (e) {
      /**
       * ★ 刷新被拒有**两种**原因，必须分开：
       *   - 令牌本身过期 / 已轮换 → 重新登录就好，这是常态；
       *   - **账号被冻结或已注销** → 重新登录不会成功。
       *   这里原来是一个裸 `catch { return false }`，把两者都当成第一种，
       *   于是被封的人看到的是「登录已过期，请重新登录」——一个死循环的指引。
       *   现在把后者带出去，交给调用方去登录页说清楚。
       */
      if (e instanceof AxiosError && e.response?.data?.code === ErrorCode.ACCOUNT_BANNED) {
        return { ok: false, blocked: new BizError(e.response.data) }
      }
      return { ok: false }
    } finally {
      // 交回主流程前清掉，下一次 401 可以重新发起刷新
      setTimeout(() => (refreshing = null), 0)
    }
  })()

  return refreshing
}

http.interceptors.response.use(
  (response) => response,
  async (error: AxiosError<{ code?: string; message?: string; data?: unknown; requestId?: string }>) => {
    const status = error.response?.status
    const original = error.config as RetriableConfig | undefined

    // 401 且没重试过 → 尝试刷新一次令牌后重放
    if (status === 401 && original && !original._retried && getRefreshToken()) {
      original._retried = true
      const result = await refreshAccessToken()
      if (result.ok) {
        return http.request(original)
      }
      clearTokens()
      onUnauthorized?.(result.blocked)
    }

    const body = error.response?.data
    if (body?.code) {
      return Promise.reject(new BizError(body as { code: string; message: string }))
    }
    if (error.code === 'ECONNABORTED') {
      return Promise.reject(new BizError({ code: ErrorCode.TIMEOUT, message: '请求超时，请稍后重试' }))
    }
    return Promise.reject(
      new BizError({ code: ErrorCode.NETWORK_ERROR, message: '网络异常，请检查网络后重试' }),
    )
  },
)

/**
 * 发起请求并解包 `{ code, message, data }`，直接返回 `data`。
 *
 * 非 OK 的 code 一律抛 BizError，调用方用 try/catch + isBizError 处理。
 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await http.request<ApiResponse<T>>(config)
  const body = response.data
  if (body.code !== ErrorCode.OK) {
    throw new BizError(body as { code: string; message: string })
  }
  return body.data as T
}

export const get = <T>(url: string, config?: AxiosRequestConfig) =>
  request<T>({ ...config, method: 'GET', url })

export const post = <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
  request<T>({ ...config, method: 'POST', url, data })

export const put = <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
  request<T>({ ...config, method: 'PUT', url, data })

export const del = <T>(url: string, config?: AxiosRequestConfig) =>
  request<T>({ ...config, method: 'DELETE', url })
