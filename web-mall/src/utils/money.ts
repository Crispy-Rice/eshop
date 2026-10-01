/** 金额一律以「分」传输，只在展示时转成元。 */

/** 12345 → "123.45" */
export function formatYuan(fen: number): string {
  const sign = fen < 0 ? '-' : ''
  const abs = Math.abs(fen)
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`
}

/** 带符号，用于展示价格区间 */
export function formatPriceRange(priceMin: number, priceMax: number): string {
  if (priceMin === priceMax) return `¥${formatYuan(priceMin)}`
  return `¥${formatYuan(priceMin)} ~ ¥${formatYuan(priceMax)}`
}

/** 元 → 分。用户输入的价格要转成分再提交 */
export function yuanToFen(yuan: string | number): number {
  const value = typeof yuan === 'number' ? yuan : Number.parseFloat(yuan)
  if (!Number.isFinite(value)) return 0
  // 用 round 避免 19.99 * 100 = 1998.9999... 这类浮点误差
  return Math.round(value * 100)
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
