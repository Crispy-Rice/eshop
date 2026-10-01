/** 图片加载失败时用内联 SVG 占位，避免出现裂图。 */

function escapeXml(value: string): string {
  return value.replace(
    /[<>&"']/g,
    (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' })[c] ?? c,
  )
}

export function placeholderImage(text: string, size = 300): string {
  const label = (text || '暂无图片').slice(0, 6)
  // 色值只能写死：这是塞进 data URI 的独立 SVG 文档，拿不到页面的 CSS 变量。
  // 取值对齐 tokens.css 里的 --n-100 / --n-400，换肤时这里不变（占位图本就该是中性灰）。
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}">
    <rect width="100%" height="100%" fill="#f1f1f3"/>
    <text x="50%" y="50%" font-family="sans-serif" font-size="${Math.round(size / 12)}"
          fill="#a1a1aa" text-anchor="middle" dominant-baseline="middle">${escapeXml(label)}</text>
  </svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

export function onImageError(event: Event): void {
  const img = event.target as HTMLImageElement
  if (img.dataset.fallbackApplied) return
  img.dataset.fallbackApplied = '1'
  img.src = placeholderImage(img.alt)
}
