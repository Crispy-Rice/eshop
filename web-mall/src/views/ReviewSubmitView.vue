<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { uploadImage } from '@/api/files'
import { isBizError } from '@/api/errors'
import {
  checkReviewEligibility,
  submitFollowUp,
  submitReview,
  type ReviewEligibility,
} from '@/api/review'
import StarRating from '@/components/StarRating.vue'
import { newIdempotencyKey } from '@/utils/idempotency'
import { onImageError } from '@/utils/placeholder'

const route = useRoute()
const router = useRouter()

/** 追评模式：进来时带 ?followUp=<首评 id> */
const followUpId = computed(() => (route.query.followUp ? String(route.query.followUp) : ''))
const isFollowUp = computed(() => followUpId.value !== '')
const orderItemId = computed(() => String(route.params.orderItemId ?? ''))

const MAX_IMAGES = 9

const elig = ref<ReviewEligibility | null>(null)
const loading = ref(false)
const submitting = ref(false)

const form = reactive({
  score: 5,
  content: '',
  anonymous: false,
})

/** 已上传成功的图片（入库用 path，展示用 url） */
const uploaded = ref<{ path: string; url: string }[]>([])
/** 上传中/失败的项。**留着 File 引用**，失败时才能真的重试而不是让用户重选 */
const uploading = ref<
  { id: number; fileName: string; preview: string; file: File; failed: boolean }[]
>([])
let uploadSeq = 0

/** 本地预览是 blob URL，组件卸载前必须回收，否则内存泄漏 */
const blobUrls: string[] = []

const canSubmit = computed(() => {
  if (isFollowUp.value) return form.content.trim().length > 0
  return form.content.trim().length > 0 || uploaded.value.length > 0
})

async function load(): Promise<void> {
  if (isFollowUp.value) return // 追评不需要资格预检，后端会校验
  loading.value = true
  try {
    elig.value = await checkReviewEligibility(orderItemId.value)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载商品信息失败')
  } finally {
    loading.value = false
  }
}

async function onPickFiles(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = '' // 允许重复选同一个文件
  if (!files.length) return

  const room = MAX_IMAGES - uploaded.value.length - uploading.value.length
  if (room <= 0) {
    ElMessage.warning(`最多上传 ${MAX_IMAGES} 张图片`)
    return
  }
  for (const file of files.slice(0, room)) {
    await startUpload(file)
  }
  if (files.length > room) ElMessage.warning(`最多上传 ${MAX_IMAGES} 张图片`)
}

/**
 * 单张上传。
 *
 * 每张独立上传、独立重试 —— 批量上传时"第 3 张非法"会让整批失败，
 * 用户还得重选前两张。
 */
async function startUpload(file: File): Promise<void> {
  const preview = URL.createObjectURL(file)
  blobUrls.push(preview)

  uploadSeq += 1
  const entry = { id: uploadSeq, fileName: file.name, preview, file, failed: false }
  uploading.value.push(entry)
  await send(entry)
}

/** 真正发请求。失败只标记，不把这条从列表里拿掉 —— 用户要能点重试 */
async function send(entry: {
  id: number
  fileName: string
  preview: string
  file: File
  failed: boolean
}): Promise<void> {
  entry.failed = false
  try {
    const result = await uploadImage(entry.file, 'reviews')
    uploaded.value.push({ path: result.path, url: result.thumbUrl || result.url })
    uploading.value = uploading.value.filter((x) => x.id !== entry.id)
  } catch (e) {
    entry.failed = true
    ElMessage.error(
      isBizError(e) ? `${entry.fileName}：${e.message}` : `${entry.fileName} 上传失败`,
    )
  }
}

function removeUploaded(index: number): void {
  uploaded.value.splice(index, 1)
}

function removeUploading(id: number): void {
  uploading.value = uploading.value.filter((x) => x.id !== id)
}

/** 重试：同一份 File 再发一次，不用用户重新选文件 */
async function retry(id: number): Promise<void> {
  const entry = uploading.value.find((x) => x.id === id)
  if (entry) await send(entry)
}

/**
 * 提交。
 *
 * ★ 幂等键在发起前生成一次：网络卡住重试要沿用同一个键，
 *   否则等于没有幂等。
 */
async function onSubmit(): Promise<void> {
  if (!canSubmit.value) {
    ElMessage.warning('写点什么再提交吧')
    return
  }
  submitting.value = true
  const key = newIdempotencyKey()
  try {
    if (isFollowUp.value) {
      await submitFollowUp(followUpId.value, {
        content: form.content.trim(),
        images: uploaded.value.map((u) => u.path),
        anonymous: form.anonymous,
      })
      ElMessage.success('追评已提交')
    } else {
      const review = await submitReview(
        {
          orderItemId: orderItemId.value,
          score: form.score,
          content: form.content.trim() || undefined,
          images: uploaded.value.map((u) => u.path),
          anonymous: form.anonymous,
        },
        key,
      )
      // 命中敏感词的会进待审核，提示要如实
      ElMessage.success(
        review.status === 0 ? '评价已提交，正在审核中' : '评价已发布',
      )
    }
    void router.replace({ name: 'reviews' })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '提交失败')
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  await load()
  if (!isFollowUp.value && elig.value && !elig.value.eligible) {
    // 不能评就把原因说清楚，别让用户白填
    ElMessage.warning(elig.value.message ?? '暂时不能评价这件商品')
  }
})

onBeforeUnmount(() => {
  for (const url of blobUrls) URL.revokeObjectURL(url)
})
</script>

<template>
  <div v-loading="loading" class="page">
    <!-- 不能评价：说清原因，给条出路 -->
    <template v-if="!isFollowUp && elig && !elig.eligible">
      <section class="blocked">
        <p class="blocked-title">{{ elig.message }}</p>
        <p v-if="elig.canFollowUp" class="blocked-sub">你之前评价过这件商品，可以去追评。</p>
      </section>
      <div class="actions">
        <el-button @click="router.back()">返回</el-button>
        <el-button
          v-if="elig.canFollowUp && elig.existingReviewId"
          type="primary"
          @click="router.replace({ name: 'review-submit', query: { followUp: elig.existingReviewId } })"
        >
          去追评
        </el-button>
      </div>
    </template>

    <template v-else>
      <!-- 商品信息（追评时后端已经知道是哪件，这里不重复展示） -->
      <section v-if="!isFollowUp && elig" class="panel">
        <div class="goods">
          <div class="cover">
            <img :src="elig.coverImage ?? ''" :alt="elig.title ?? ''" @error="onImageError" />
          </div>
          <div class="info">
            <h3 class="title">{{ elig.title }}</h3>
            <p class="spec">{{ elig.specText || '—' }}</p>
            <p class="meta">共 {{ elig.num }} 件</p>
          </div>
        </div>
      </section>

      <section class="panel">
        <header class="panel-head">{{ isFollowUp ? '追加评价' : '发表评价' }}</header>

        <!-- 追评不重新打分 -->
        <div v-if="!isFollowUp" class="field">
          <div class="label">评分</div>
          <StarRating v-model:score="form.score" editable size="lg" />
        </div>
        <div v-else class="field">
          <p class="follow-hint">用了一段时间之后，再说说真实感受吧（不重新打分）。</p>
        </div>

        <div class="field">
          <div class="label">内容</div>
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="4"
            maxlength="2000"
            show-word-limit
            placeholder="说说商品怎么样，给其他买家一个参考"
          />
        </div>

        <div class="field">
          <div class="label">
            图片
            <span class="hint">最多 {{ MAX_IMAGES }} 张</span>
          </div>
          <div class="thumbs">
            <div v-for="(img, i) in uploaded" :key="img.path" class="thumb">
              <img :src="img.url" alt="已上传" @error="onImageError" />
              <button type="button" class="remove" @click="removeUploaded(i)">×</button>
            </div>

            <div v-for="up in uploading" :key="up.id" class="thumb uploading">
              <img :src="up.preview" alt="上传中" />
              <button type="button" class="remove" @click="removeUploading(up.id)">×</button>
              <div v-if="up.failed" class="fail">
                <span>失败</span>
                <button type="button" class="retry" @click="retry(up.id)">重试</button>
              </div>
              <div v-else class="spinner">上传中</div>
            </div>

            <label v-if="uploaded.length + uploading.length < MAX_IMAGES" class="picker">
              <input type="file" accept="image/*" multiple hidden @change="onPickFiles" />
              <span class="plus">+</span>
            </label>
          </div>
          <p class="tip">支持 JPG / PNG / WEBP，单张不超过 5MB；图片会自动压缩并去除拍摄信息。</p>
        </div>

        <div class="field row-between">
          <div>
            <div class="label">匿名评价</div>
            <p class="tip">开启后其他买家看不到你的昵称与头像</p>
          </div>
          <el-switch v-model="form.anonymous" />
        </div>
      </section>

      <footer class="action-bar">
        <el-button @click="router.back()">返回</el-button>
        <el-button
          type="primary"
          size="large"
          :loading="submitting"
          :disabled="!canSubmit"
          @click="onSubmit"
        >
          {{ isFollowUp ? '提交追评' : '发布评价' }}
        </el-button>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding-bottom: 76px;
  max-width: 720px;
}

.blocked {
  padding: var(--space-6);
  border-radius: var(--radius-lg);
  background: var(--color-warning-soft);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.blocked-title {
  font-size: var(--text-lg);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.blocked-sub {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-3);
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.panel-head {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.goods {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4);
}

.cover {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--media-bg);
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.title {
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
}

.spec,
.meta {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.field {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.row-between {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
}

.label {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.label .hint {
  margin-left: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.follow-hint {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.tip {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
  line-height: var(--leading-snug);
}

/* ---------- 图片 ---------- */

.thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.thumb {
  position: relative;
  width: 88px;
  height: 88px;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--media-bg);
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.remove {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 20px;
  height: 20px;
  border: none;
  border-radius: var(--radius-pill);
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}

.uploading img {
  opacity: 0.5;
}

.spinner,
.fail {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: var(--text-xs);
  color: var(--color-text);
  background: rgba(255, 255, 255, 0.65);
}

.fail {
  gap: var(--space-1);
  grid-auto-flow: row;
}

.retry {
  border: none;
  background: none;
  color: var(--color-accent);
  font-size: var(--text-xs);
  cursor: pointer;
  text-decoration: underline;
}

.picker {
  width: 88px;
  height: 88px;
  display: grid;
  place-items: center;
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-out);
}

.picker:hover {
  border-color: var(--color-accent);
}

.plus {
  font-size: var(--text-2xl);
  color: var(--color-text-placeholder);
}

.action-bar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: var(--z-sticky);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
  padding: var(--space-3) var(--layout-gutter);
  background: var(--color-bg-surface);
  border-top: 1px solid var(--color-border);
}
</style>
