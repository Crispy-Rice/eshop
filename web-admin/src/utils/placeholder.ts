/** 没有真实图片时用内联 SVG 占位，避免出现裂图 */

export const DEFAULT_IMAGE = '/media/placeholder.svg'

function escapeXml(value: string): string {
  return value.replace(
    /[<>&"']/g,
    (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' })[c] ?? c,
  )
}

export function placeholderImage(text: string, size = 300): string {
  const label = (text || '暂无图片').slice(0, 6)
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}">
    <rect width="100%" height="100%" fill="#f0f2f5"/>
    <text x="50%" y="50%" font-family="sans-serif" font-size="${Math.round(size / 12)}"
          fill="#c0c4cc" text-anchor="middle" dominant-baseline="middle">${escapeXml(label)}</text>
  </svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

/**
 * 图片加载失败时替换成占位图。
 *
 * 用事件回退而不是把 data URI 直接存库：data URI 动辄几百字符，
 * 而 main_image / cover_image 是 VARCHAR(255)，存不下。
 */
export function onImageError(event: Event): void {
  const img = event.target as HTMLImageElement
  if (img.dataset.fallbackApplied) return
  img.dataset.fallbackApplied = '1'
  img.src = placeholderImage(img.alt)
}
