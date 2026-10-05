<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import { uploadImage } from '@/api/files'
import { onImageError } from '@/utils/placeholder'

/**
 * 头像上传。
 *
 * 存的是 `url`（`/media/avatars/<用户id>/xxx.webp`），不是 `path` —— 它会直接被
 * 当成 `<img src>` 用。这和评价/售后那两处存 path 的契约不同，别互相套：
 * 那两处的图有「路径前缀必须是自己目录」的校验，头像没有。
 *
 * 没做「清除头像」：后端 `PUT /api/me` 里 `avatar: null` 表示"不改"，
 * 想清空只能写空串，多一条尴尬路径；换一张新的就够了。
 */
defineProps<{ modelValue: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [string] }>()

const uploading = ref(false)

async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 清掉才能重复选同一个文件
  if (!file) return

  uploading.value = true
  try {
    const img = await uploadImage(file, 'avatars')
    emit('update:modelValue', img.url)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '上传失败')
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <label class="avatar-uploader" :class="{ 'is-uploading': uploading }">
    <input type="file" accept="image/*" hidden :disabled="uploading" @change="onPick" />

    <img v-if="modelValue" :src="modelValue" alt="头像" @error="onImageError" />
    <span v-else class="empty">+</span>

    <span class="mask">{{ uploading ? '上传中…' : '更换' }}</span>
  </label>
</template>

<style scoped>
/* 只用语义 token，见 docs/17-frontend-design-system.md */
.avatar-uploader {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 72px;
  height: 72px;
  flex: 0 0 auto;
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-pill);
  background: var(--media-bg);
  color: var(--color-text-tertiary);
  cursor: pointer;
  overflow: hidden;
  transition: border-color var(--dur-fast) var(--ease-out);
}

.avatar-uploader:hover {
  border-color: var(--color-accent);
}

.avatar-uploader img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.empty {
  font-size: var(--text-xl);
  line-height: 1;
}

.mask {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-xs);
  color: var(--color-text-inverse);
  background: var(--color-bg-mask);
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease-out);
}

.avatar-uploader:hover .mask,
.avatar-uploader.is-uploading .mask {
  opacity: 1;
}
</style>
