/**
 * 幂等键。
 *
 * ★ 必须用 `crypto.randomUUID`，它在**非安全上下文**（HTTP + IP 直连）里
 *   不存在，那时回落到 `getRandomValues` 拼一个（docs/10 §4.2）。
 *
 *   同一个键只在"这一笔提交"里有效 —— **重试要沿用同一个键，新提交要用新键**。
 *   所以调用方要在发起请求前生成一次并持有它，而不是每次重试都重新生成
 *   （那样等于没有幂等）。
 */
export function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID().replace(/-/g, '').slice(0, 32)
  }
  const bytes = new Uint8Array(16)
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    crypto.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256)
  }
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}
