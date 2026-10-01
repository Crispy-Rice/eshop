import axios, { AxiosError, type AxiosRequestConfig } from 'axios'

import { BizError, ErrorCode, type ApiResponse } from './errors'

/**
 * 令牌存取。
 *
 * 第一期 access token 放 localStorage（简单、够用）。
 * ★ 开启 HTTPS 后应把 refresh token 改存 HttpOnly Cookie
 *   （docs/15-api-and-errors.md §1.1）。
 */
const ACCESS_TOKEN_KEY = 'eshop.accessToken'

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY)
}

export function setAccessToken(token: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, token)
}

export function clearAccessToken(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
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

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ code?: string; message?: string; data?: unknown; requestId?: string }>) => {
    // 后端返回了结构化错误体 —— 原样转成 BizError
    const body = error.response?.data
    if (body?.code) {
      return Promise.reject(new BizError(body as { code: string; message: string }))
    }
    if (error.code === 'ECONNABORTED') {
      return Promise.reject(
        new BizError({ code: ErrorCode.TIMEOUT, message: '请求超时，请稍后重试' }),
      )
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
