import axios, { AxiosError, type AxiosRequestConfig } from 'axios'

import { BizError, ErrorCode, type ApiResponse } from './errors'

/**
 * 令牌存取。
 *
 * 第一期 access token 与 refresh token 都放 localStorage（简单、够用）。
 * ★ 开启 HTTPS 后应把 refresh token 改存 HttpOnly Cookie
 *   （docs/15-api-and-errors.md §1.1）。
 */
const ACCESS_TOKEN_KEY = 'eshop.accessToken'
const REFRESH_TOKEN_KEY = 'eshop.refreshToken'

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
let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler
}

type RetriableConfig = AxiosRequestConfig & { _retried?: boolean }

/**
 * 刷新令牌。多个请求同时 401 时共用同一次刷新，避免并发刷新把
 * refresh token 轮换掉（我们的刷新是**轮换式**的，旧的一次只能用一次）。
 */
let refreshing: Promise<boolean> | null = null

async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) return false

  refreshing ??= (async () => {
    try {
      const resp = await axios.post<ApiResponse<{ accessToken: string; refreshToken: string }>>(
        '/api/auth/refresh',
        { refreshToken },
        { headers: { 'Content-Type': 'application/json' } },
      )
      const data = resp.data.data
      if (!data) return false
      setTokens(data.accessToken, data.refreshToken)
      return true
    } catch {
      return false
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
      if (await refreshAccessToken()) {
        return http.request(original)
      }
      clearTokens()
      onUnauthorized?.()
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
