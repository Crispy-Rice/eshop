<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { fetchMyShop, updateMyShop } from '@/api/auth'
import { ErrorCode, isBizError } from '@/api/errors'
import ImageUploader from '@/components/ImageUploader.vue'

const loading = ref(false)
const saving = ref(false)
/** 没开店时后端回 404 —— 正常走导航进不来，直接敲 URL 才可能撞上 */
const noShop = ref(false)
const formRef = ref<FormInstance>()

/** logo 用 '' 而不是 null：ImageUploader 清空时 emit 的是空串 */
const form = reactive({ name: '', logo: '', description: '' })

/** 与后端 ShopUpdateRequest / ShopCreateRequest 对齐：名称 2~64，简介 ≤255 */
const rules: FormRules = {
  name: [
    { required: true, message: '请填写店铺名称', trigger: 'blur' },
    { min: 2, max: 64, message: '店铺名称 2~64 个字符', trigger: 'blur' },
  ],
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const shop = await fetchMyShop()
    form.name = shop.name
    form.logo = shop.logo ?? ''
    form.description = shop.description ?? ''
  } catch (e) {
    if (isBizError(e, ErrorCode.NOT_FOUND)) noShop.value = true
    else ElMessage.error(isBizError(e) ? e.message : '加载店铺信息失败')
  } finally {
    loading.value = false
  }
}

/**
 * 保存。
 *
 * ★ **三个字段都显式提交**：后端的空值语义是"传 null = 清空，不传 = 不改"
 *   （model_fields_set 区分）。这里把空串转成 null，于是"把简介删干净再保存"
 *   真的会清空，而不是存进去一个空字符串 —— 后者商城里会渲染成一行空白。
 */
async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  saving.value = true
  try {
    await updateMyShop({
      name: form.name.trim(),
      logo: form.logo.trim() || null,
      description: form.description.trim() || null,
    })
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">店铺设置</h2>
      <span class="hint">这些信息会显示在商城的商品详情页与商品卡片上</span>
    </div>

    <div v-loading="loading" class="panel">
      <el-empty v-if="noShop" description="你还没有店铺">
        <p class="empty-hint">先去「我的商品」页开通店铺，再回来设置</p>
      </el-empty>

      <el-form
        v-else
        ref="formRef"
        class="shop-form"
        :model="form"
        :rules="rules"
        label-position="top"
      >
        <el-form-item label="店铺名称" prop="name">
          <el-input v-model="form.name" maxlength="64" show-word-limit placeholder="2~64 个字符" />
        </el-form-item>

        <el-form-item label="店铺 LOGO">
          <div class="logo-field">
            <ImageUploader v-model="form.logo" biz="shops" clearable />
            <span class="form-hint">建议正方形；不传则商城里用店名首字代替</span>
          </div>
        </el-form-item>

        <el-form-item label="店铺简介">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            maxlength="255"
            show-word-limit
            placeholder="一句话介绍你的店（选填）"
          />
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" @click="onSubmit">保存</el-button>
          <el-button @click="load">重置</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.toolbar {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.hint {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-6) var(--space-6) var(--space-2);
}

.panel :deep(.el-empty) {
  padding: var(--space-8) 0;
}

.empty-hint {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text-placeholder);
}

/* 表单别拉满一屏 —— 输入框和它的右侧留白比例失衡，读起来很散 */
.shop-form {
  max-width: 480px;
}

.logo-field {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.form-hint {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}
</style>
