import { http } from './http'

export interface HealthResponse {
  status: string
  checks?: Record<string, string>
}

/**
 * 健康检查接口在根路径下（不在 /api 前缀里），而且返回的是裸 JSON
 * 不是统一的 ApiResponse 包装，所以这里直接用 http 而不走 request()。
 */
export async function fetchHealth(): Promise<HealthResponse> {
  const res = await http.request<HealthResponse>({ url: '/healthz', method: 'GET', baseURL: '' })
  return res.data
}

export async function fetchReady(): Promise<HealthResponse> {
  const res = await http.request<HealthResponse>({ url: '/readyz', method: 'GET', baseURL: '' })
  return res.data
}
