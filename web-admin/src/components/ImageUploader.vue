<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import { uploadImage, type BizLine } from '@/api/files'
import { onImageError } from '@/utils/placeholder'

/**
 * 单图上传。后台第一次把上传抽成公共组件 —— 发布页、编辑页的主图与 SKU 封面
 * 一共四处用法，尺寸不同（表单里大、SKU 行里小），逻辑完全一样。
 *
 * **存的是 `url` 而不是 `path`**：商品图这条链路上，商城主图、购物车缩略图、
 * 订单项快照都是直接把字段当 `<img src>` 用的，存相对路径的话这三处渲染全要改。
 * （评价/售后那两处存 path，是因为它们有「路径前缀必须是自己目录」的校验要求，
 *  而商品没有。这是两套契约，别互相套。）
 *
 * 多图场景（售后的质检留证）不适用本组件，那边是 ≤9 张、可删可排序。
 */
const props = withDefaults(
  defineProps<{
    modelValue: string
    /** 业务线，进服务端路径白名单；商品图用 products，店铺 LOGO 用 shops */
    biz: BizLine
    size?: 'large' | 'small'
    disabled?: boolean
    /** 选填字段才给清除按钮；必填字段（商品主图）不给，清了也提交不了 */
    clearable?: boolean
  }>(),
  { size: 'large', disabled: false, clearable: false },
)

const emit = defineEmits<{ 'update:modelValue': [string] }>()

const uploading = ref(false)

async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 清掉才能重复选同一个文件
  if (!file) return

  uploading.value = true
  try {
    const img = await uploadImage(file, props.biz)
    emit('update:modelValue', img.url)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '上传失败')
  } finally {
    uploading.value = false
  }
}

function onClear(event: MouseEvent): void {
  // 别触发 label 的选文件
  event.preventDefault()
  event.stopPropagation()
  emit('update:modelValue', '')
}
</script>

<template>
  <label
    class="uploader"
    :class="[`is-${size}`, { 'is-disabled': disabled, 'is-uploading': uploading }]"
  >
    <input type="file" accept="image/*" hidden :disabled="disabled" @change="onPick" />

    <img v-if="modelValue" :src="modelValue" alt="图片" @error="onImageError" />

    <span v-if="clearable && modelValue && !uploading" class="clear" @click="onClear">×</span>

    <div class="hollow">
      <span v-if="uploading">上传中…</span>
      <span v-else-if="modelValue">更换</span>
      <span v-else>+ 上传</span>
    </div>
  </label>
</template>

<style scoped>
/* 只用语义 token，见 docs/17-frontend-design-system.md */
.uploader {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--media-bg);
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
  cursor: pointer;
  overflow: hidden;
  transition: border-color var(--dur-fast) var(--ease-out);
}

.uploader.is-large {
  width: 120px;
  height: 120px;
}

.uploader.is-small {
  width: 40px;
  height: 40px;
  border-radius: var(--radius-sm);
}

.uploader:hover {
  border-color: var(--color-accent);
}

.uploader.is-disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.uploader.is-disabled:hover {
  border-color: var(--color-border-strong);
}

.uploader img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 有图时默认藏起来，hover 才浮出来当"更换" */
.uploader .hollow {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
}

.uploader:has(img) .hollow {
  opacity: 0;
  background: var(--color-bg-mask);
  color: var(--color-text-inverse);
  transition: opacity var(--dur-fast) var(--ease-out);
}

.uploader:has(img):hover .hollow {
  opacity: 1;
}

/* 上传中要一直可见，不能被"hover 才显示"的规则藏掉 */
.uploader.is-uploading .hollow {
  opacity: 1;
  background: var(--color-bg-mask);
  color: var(--color-text-inverse);
}

/* 选填字段的清除按钮，压在右上角 */
.clear {
  position: absolute;
  top: 2px;
  right: 2px;
  z-index: 2;
  width: 16px;
  height: 16px;
  line-height: 14px;
  text-align: center;
  border-radius: var(--radius-pill);
  background: var(--color-danger);
  color: var(--color-text-inverse);
  font-size: var(--text-xs);
}

.clear:hover {
  opacity: 0.85;
}
</style>
