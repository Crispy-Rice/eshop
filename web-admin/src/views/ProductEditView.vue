<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  fetchMySpu,
  offShelf,
  onShelf,
  submitForAudit,
  updateSku,
  updateSpu,
  SPU_STATUS_TEXT,
  SPU_STATUS_TYPE,
  type SkuDetail,
  type SpuDetail,
} from '@/api/product'
import ImageUploader from '@/components/ImageUploader.vue'
import { placeholderImage, onImageError } from '@/utils/placeholder'
import { formatYuan, yuanToFen } from '@/utils/money'

const route = useRoute()
const router = useRouter()

const spuId = computed(() => String(route.params.spuId))
const spu = ref<SpuDetail | null>(null)
const loading = ref(false)
const saving = ref(false)

const form = reactive({ title: '', subTitle: '', mainImage: '' })
/** skuId → 正在编辑的价格/重量（元 / 克） */
const skuDraft = reactive<Record<string, { price: number | undefined; weightG: number | undefined; coverImage: string }>>({})

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await fetchMySpu(spuId.value)
    spu.value = data
    form.title = data.title
    form.subTitle = data.subTitle ?? ''
    form.mainImage = data.mainImage
    for (const sku of data.skus) {
      skuDraft[sku.id] = {
        price: sku.price / 100,
        weightG: sku.weightG,
        coverImage: sku.coverImage,
      }
    }
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载失败')
    void router.push('/')
  } finally {
    loading.value = false
  }
}

async function saveBasic(): Promise<void> {
  saving.value = true
  try {
    spu.value = await updateSpu(spuId.value, {
      title: form.title,
      subTitle: form.subTitle || null,
      mainImage: form.mainImage,
    })
    ElMessage.success('商品信息已保存')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function saveSku(sku: SkuDetail): Promise<void> {
  const draft = skuDraft[sku.id]
  if (!draft) return
  if (!draft.price || draft.price <= 0) {
    ElMessage.warning('价格必须大于 0')
    return
  }
  if (!draft.weightG || draft.weightG <= 0) {
    ElMessage.warning('重量必须大于 0')
    return
  }

  try {
    await updateSku(sku.id, {
      price: yuanToFen(draft.price),
      weightG: draft.weightG,
      coverImage: draft.coverImage,
    })
    ElMessage.success('SKU 已更新')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  }
}

async function act(action: 'submit' | 'on' | 'off'): Promise<void> {
  const labels = { submit: '提交审核', on: '上架', off: '下架' }
  try {
    await ElMessageBox.confirm(`确定要${labels[action]}吗？`, '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    if (action === 'submit') await submitForAudit(spuId.value)
    else if (action === 'on') await onShelf(spuId.value)
    else await offShelf(spuId.value)
    ElMessage.success(`${labels[action]}成功`)
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : `${labels[action]}失败`)
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <el-card v-if="spu" shadow="never">
      <template #header>
        <div class="card-header">
          <div class="left">
            <span>编辑商品</span>
            <el-tag :type="SPU_STATUS_TYPE[spu.status] ?? 'info'" size="small">
              {{ SPU_STATUS_TEXT[spu.status] ?? spu.status }}
            </el-tag>
          </div>
          <div class="actions">
            <el-button @click="router.push('/')">返回列表</el-button>
            <el-button v-if="spu.status === 1" type="primary" @click="act('submit')">提交审核</el-button>
            <el-button v-if="spu.status === 3" type="success" @click="act('on')">上架</el-button>
            <el-button v-if="spu.status === 2" type="warning" @click="act('off')">下架</el-button>
          </div>
        </div>
      </template>

      <el-alert type="warning" :closable="false" class="mb16">
        <template #title>
          规格组合与 SKU 集合创建后不可修改 —— 订单里存的是 SKU 快照，
          增减 SKU 会让历史订单指向不存在的商品。需要不同规格请新建商品。
        </template>
      </el-alert>

      <el-form :model="form" label-width="100px">
        <el-form-item label="商品标题">
          <el-input v-model="form.title" maxlength="120" class="w360" />
        </el-form-item>
        <el-form-item label="副标题">
          <el-input v-model="form.subTitle" maxlength="255" class="w360" />
        </el-form-item>
        <el-form-item label="主图">
          <div class="image-row">
            <ImageUploader v-model="form.mainImage" biz="products" />
            <span class="inline-hint">长边超过 1280px 会自动压缩</span>
          </div>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="saveBasic">保存基本信息</el-button>
        </el-form-item>
      </el-form>

      <el-divider />

      <h3 class="section">规格</h3>
      <el-descriptions :column="1" border class="mb16">
        <el-descriptions-item v-for="group in spu.specGroups" :key="group.id" :label="group.name">
          <el-tag v-for="v in group.values" :key="v.id" class="mr8" type="info">
            {{ v.value }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <h3 class="section">SKU（价格改动不影响已下单订单）</h3>
      <el-table :data="spu.skus">
        <el-table-column label="封面" width="70">
          <template #default="{ row }">
            <img
              :src="skuDraft[row.id]?.coverImage || placeholderImage(row.specText)"
              alt="封面"
              class="thumb"
              @error="onImageError"
            />
          </template>
        </el-table-column>
        <el-table-column prop="specText" label="规格" min-width="150" />
        <el-table-column prop="skuCode" label="商家编码" width="170" />
        <el-table-column label="原价" width="110">
          <template #default="{ row }">¥{{ formatYuan(row.price) }}</template>
        </el-table-column>
        <el-table-column label="价格（元）" width="140">
          <template #default="{ row }">
            <el-input-number
              v-model="skuDraft[row.id]!.price"
              size="small"
              :min="0.01"
              :precision="2"
              :controls="false"
              class="w110"
            />
          </template>
        </el-table-column>
        <el-table-column label="重量（克）" width="130">
          <template #default="{ row }">
            <el-input-number
              v-model="skuDraft[row.id]!.weightG"
              size="small"
              :min="1"
              :precision="0"
              :controls="false"
              class="w110"
            />
          </template>
        </el-table-column>
        <el-table-column label="封面图" width="110" align="center">
          <template #default="{ row }">
            <ImageUploader v-model="skuDraft[row.id]!.coverImage" biz="products" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="primary" @click="saveSku(row)">保存</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.left {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.actions {
  display: flex;
  gap: var(--space-2);
}

/* 区块标题：左侧一道短竖线做锚点，比单纯加粗更容易在长表单里定位 */
.section {
  position: relative;
  margin: 0 0 var(--space-3);
  padding-left: var(--space-3);
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.section::before {
  content: "";
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 14px;
  border-radius: var(--radius-pill);
  background: var(--color-accent);
}

.mb16 {
  margin-bottom: var(--space-4);
}

.mr8 {
  margin-right: var(--space-2);
  margin-bottom: var(--space-1);
}

.w110 {
  width: 110px;
}

.w360 {
  width: 360px;
}

.image-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.preview,
.thumb {
  flex: 0 0 auto;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-bg-inset);
}

.preview {
  width: 64px;
  height: 64px;
}

.thumb {
  width: 40px;
  height: 40px;
}
</style>
