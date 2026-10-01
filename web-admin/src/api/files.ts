import { post } from './http'

/**
 * 上传单张图片（商品图、售后的质检留证）。
 *
 * ★ 必须把 `Content-Type` 置空：axios 实例上默认挂着 `application/json`，
 *   而 multipart 请求的 `Content-Type` 要带 boundary，只能由浏览器/axios
 *   根据 FormData 自己生成。显式设成 undefined 才会让 axios 重新推导。
 */
export function uploadImage(
  file: File,
  biz: 'reviews' | 'aftersale' | 'products',
): Promise<UploadedImage> {
  const form = new FormData()
  form.append('file', file)
  return post<UploadedImage>(`/files/images?biz=${biz}`, form, {
    headers: { 'Content-Type': undefined },
    // 上传比普通请求慢，给足时间
    timeout: 30_000,
  })
}

export interface UploadedImage {
  /** 相对路径，**入库用这个**（不带 /media/ 前缀） */
  path: string
  url: string
  thumbPath: string
  thumbUrl: string
  width: number
  height: number
}
