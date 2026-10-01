/** 没有真实图片时用内联 SVG 占位，避免出现裂图 */

/**
 * 商品还没有图片时的默认图。
 *
 * ★ 用内联 data URI，而不是指向某个静态文件。因为**这个值是存进数据库的**
 *   （`product.spu.main_image`，VARCHAR(255)），指向文件就会有一串麻烦：
 *   那份文件得同时存在于商城（挂在 `/`）和后台（挂在 `/admin/`）两个路径下，
 *   而且 `/media/` 是挂载的数据卷，部署时会被覆盖。内联就没有这些问题，
 *   也省掉一次必然 404 的请求。
 *
 * 长度约 151 字符 —— 留足 VARCHAR(255) 的余量，别往里加文字标签。
 */
export const DEFAULT_IMAGE =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='300'%3E%3Crect width='300' height='300' fill='%23f1f1f3'/%3E%3C/svg%3E"

function escapeXml(value: string): string {
  return value.replace(
    /[<>&"']/g,
    (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' })[c] ?? c,
  )
}

export function placeholderImage(text: string, size = 300): string {
  const label = (text || '暂无图片').slice(0, 6)
  // 色值只能写死：这是塞进 data URI 的独立 SVG 文档，拿不到页面的 CSS 变量。
  // 取值对齐 tokens.css 里的 --n-100 / --n-400。
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}">
    <rect width="100%" height="100%" fill="#f1f1f3"/>
    <text x="50%" y="50%" font-family="sans-serif" font-size="${Math.round(size / 12)}"
          fill="#a1a1aa" text-anchor="middle" dominant-baseline="middle">${escapeXml(label)}</text>
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
