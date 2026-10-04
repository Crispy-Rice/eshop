<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { fetchCategoryTree, type Category } from '@/api/category'
import { isBizError } from '@/api/errors'
import { createSpu, type SkuIn, type SpuCreateInput } from '@/api/product'
import ImageUploader from '@/components/ImageUploader.vue'
import { DEFAULT_IMAGE } from '@/utils/placeholder'
import { formatYuan, yuanToFen } from '@/utils/money'

const router = useRouter()

let seq = 0
const uid = (): string => `k${Date.now().toString(36)}${(seq++).toString(36)}`

interface ValueRow {
  key: string
  value: string
  image: string
}
interface GroupRow {
  key: string
  name: string
  values: ValueRow[]
}
interface SkuRow {
  comboKey: string
  label: string
  keys: string[]
  enabled: boolean
  skuCode: string
  /** 元，展示用；提交时转成分 */
  price: number | undefined
  coverImage: string
  weightG: number | undefined
}

const MAX_GROUPS = 3
const MAX_VALUES_PER_GROUP = 20
const MAX_SKUS = 200

const categories = ref<Category[]>([])
const submitting = ref(false)
const formRef = ref<FormInstance>()

const form = reactive({
  categoryId: '' as string,
  title: '',
  subTitle: '',
  mainImage: DEFAULT_IMAGE,
})

const groups = ref<GroupRow[]>([
  { key: uid(), name: '颜色', values: [{ key: uid(), value: '', image: '' }] },
])

const rules: FormRules = {
  categoryId: [{ required: true, message: '请选择末级类目', trigger: 'change' }],
  title: [{ required: true, message: '请输入商品标题', trigger: 'blur' }],
  mainImage: [{ required: true, message: '请上传主图', trigger: 'change' }],
}

const cascaderProps = {
  value: 'id',
  label: 'name',
  children: 'children',
  emitPath: false,
} as const

// ---------- 规格编辑 ----------
function addGroup(): void {
  if (groups.value.length >= MAX_GROUPS) {
    ElMessage.warning(`最多 ${MAX_GROUPS} 个规格组`)
    return
  }
  groups.value.push({ key: uid(), name: '', values: [{ key: uid(), value: '', image: '' }] })
}

function removeGroup(index: number): void {
  groups.value.splice(index, 1)
}

function addValue(group: GroupRow): void {
  if (group.values.length >= MAX_VALUES_PER_GROUP) {
    ElMessage.warning(`每组最多 ${MAX_VALUES_PER_GROUP} 个规格值`)
    return
  }
  group.values.push({ key: uid(), value: '', image: '' })
}

function removeValue(group: GroupRow, index: number): void {
  group.values.splice(index, 1)
}

/** 只保留填了名字的组、填了取值的值 */
const effectiveGroups = computed(() =>
  groups.value
    .map((g) => ({ ...g, values: g.values.filter((v) => v.value.trim() !== '') }))
    .filter((g) => g.name.trim() !== '' && g.values.length > 0),
)

/** 规格取值的笛卡尔积 —— 每个组合一行 SKU */
const combinations = computed<{ keys: string[]; label: string }[]>(() => {
  const result: { keys: string[]; label: string }[] = []
  for (const group of effectiveGroups.value) {
    if (result.length === 0) {
      for (const value of group.values) {
        result.push({ keys: [value.key], label: value.value.trim() })
      }
    } else {
      const expanded: { keys: string[]; label: string }[] = []
      for (const existing of result) {
        for (const value of group.values) {
          expanded.push({
            keys: [...existing.keys, value.key],
            label: `${existing.label} / ${value.value.trim()}`,
          })
        }
      }
      result.length = 0
      result.push(...expanded)
    }
  }
  return result
})

const skuRows = ref<Record<string, SkuRow>>({})

// 规格变化时重建 SKU 行：已填过的组合尽量保留原有输入
watch(
  combinations,
  (combos) => {
    const next: Record<string, SkuRow> = {}
    for (const combo of combos) {
      const key = combo.keys.join('|')
      const existing = skuRows.value[key]
      if (existing) {
        existing.label = combo.label
        existing.keys = combo.keys
        next[key] = existing
      } else {
        next[key] = {
          comboKey: key,
          label: combo.label,
          keys: combo.keys,
          enabled: true,
          skuCode: '',
          price: undefined,
          coverImage: DEFAULT_IMAGE,
          weightG: undefined,
        }
      }
    }
    skuRows.value = next
  },
  { immediate: true },
)

const skuTable = computed(() =>
  combinations.value.map((c) => skuRows.value[c.keys.join('|')]).filter((r): r is SkuRow => !!r),
)

const enabledSkus = computed(() => skuTable.value.filter((r) => r.enabled))

/** 没勾选的组合就是"不上架的无效组合"，比如"白色 512G" */
const disabledCount = computed(() => skuTable.value.length - enabledSkus.value.length)

function generateCodes(): void {
  if (!form.title) {
    ElMessage.warning('先填商品标题，再按标题生成编码')
    return
  }
  enabledSkus.value.forEach((row, index) => {
    if (!row.skuCode) row.skuCode = `${form.title.slice(0, 6).toUpperCase().replace(/\s/g, '')}-${index + 1}`
  })
}

const pricePreview = computed(() => {
  const prices = enabledSkus.value.map((r) => r.price).filter((p): p is number => typeof p === 'number')
  if (prices.length === 0) return '—'
  const min = Math.min(...prices)
  const max = Math.max(...prices)
  const text = min === max ? `¥${formatYuan(yuanToFen(min))}` : `¥${formatYuan(yuanToFen(min))} ~ ¥${formatYuan(yuanToFen(max))}`
  return text
})

// ---------- 提交 ----------
function validateSpecs(): string | null {
  if (effectiveGroups.value.length === 0) return '至少需要一个填好的规格组'
  if (skuTable.value.length === 0) return '没有生成任何 SKU'
  if (skuTable.value.length > MAX_SKUS) return `SKU 数量不能超过 ${MAX_SKUS}`
  if (enabledSkus.value.length === 0) return '至少要上架一个 SKU'

  for (const row of enabledSkus.value) {
    if (!row.skuCode.trim()) return `「${row.label}」缺少商家编码`
    if (row.price === undefined || row.price <= 0) return `「${row.label}」的价格必须大于 0`
    if (!row.coverImage.trim()) return `「${row.label}」缺少封面图`
    if (!row.weightG || row.weightG <= 0) return `「${row.label}」的重量必须大于 0`
  }

  const codes = enabledSkus.value.map((r) => r.skuCode.trim())
  if (new Set(codes).size !== codes.length) return '商家编码有重复'

  return null
}

async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  const error = validateSpecs()
  if (error) {
    ElMessage.warning(error)
    return
  }

  const payload: SpuCreateInput = {
    categoryId: form.categoryId,
    title: form.title,
    subTitle: form.subTitle || null,
    mainImage: form.mainImage,
    specGroups: effectiveGroups.value.map((g) => ({
      name: g.name.trim(),
      values: g.values.map((v) => ({ key: v.key, value: v.value.trim(), image: v.image || null })),
    })),
    skus: enabledSkus.value.map<SkuIn>((row) => ({
      skuCode: row.skuCode.trim(),
      specValueKeys: row.keys,
      price: yuanToFen(row.price ?? 0),
      coverImage: row.coverImage.trim(),
      weightG: row.weightG ?? 0,
    })),
  }

  submitting.value = true
  try {
    const created = await createSpu(payload)
    ElMessage.success('商品已保存为草稿，去列表页提交审核')
    await router.push({ name: 'product-edit', params: { spuId: created.id } })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '发布失败')
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  try {
    categories.value = await fetchCategoryTree()
  } catch {
    ElMessage.error('加载类目失败，请确认后端已启动且平台已建好类目')
  }
})
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>发布商品</span>
          <el-button @click="router.push('/')">返回列表</el-button>
        </div>
      </template>

      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="末级类目" prop="categoryId">
          <el-cascader
            v-model="form.categoryId"
            :options="categories"
            :props="cascaderProps"
            placeholder="只能选到末级类目"
            class="w360"
          />
        </el-form-item>
        <el-form-item label="商品标题" prop="title">
          <el-input v-model="form.title" maxlength="120" show-word-limit />
        </el-form-item>
        <el-form-item label="副标题">
          <el-input v-model="form.subTitle" maxlength="255" />
        </el-form-item>
        <el-form-item label="主图" prop="mainImage">
          <div class="image-row">
            <ImageUploader v-model="form.mainImage" biz="products" />
            <span class="inline-hint">
              建议正方形。长边超过 1280px 会自动压缩，带 GPS 的元数据会被剔除
            </span>
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>规格</span>
          <el-button size="small" :disabled="groups.length >= MAX_GROUPS" @click="addGroup">
            添加规格组（最多 3 组）
          </el-button>
        </div>
      </template>

      <div v-for="(group, gi) in groups" :key="group.key" class="group">
        <div class="group-head">
          <el-input v-model="group.name" placeholder="规格名，如 颜色" class="w200" maxlength="32" />
          <el-button link type="danger" :disabled="groups.length <= 1" @click="removeGroup(gi)">
            删除该组
          </el-button>
        </div>

        <div class="values">
          <div v-for="(value, vi) in group.values" :key="value.key" class="value-row">
            <el-input v-model="value.value" placeholder="取值，如 暗夜黑" class="w180" maxlength="32" />
            <ImageUploader v-model="value.image" biz="products" size="small" clearable />
            <span class="inline-hint">色块图（选填）</span>
            <el-button link type="danger" :disabled="group.values.length <= 1" @click="removeValue(group, vi)">
              删除
            </el-button>
          </div>
          <el-button size="small" @click="addValue(group)">添加取值</el-button>
        </div>
      </div>

      <el-alert type="info" :closable="false">
        <template #title>
          已生成 {{ skuTable.length }} 个规格组合，默认全部上架。
          勾掉不需要的组合即可（例如"白色 + 512G"没有备货，取消勾选即可，前端会把它置灰）。
        </template>
      </el-alert>
    </el-card>

    <el-card v-if="skuTable.length > 0" class="mt16" shadow="never">
      <template #header>
        <div class="card-header">
          <span>SKU 明细</span>
          <div class="header-actions">
            <span class="hint">
              上架 {{ enabledSkus.length }} 个<span v-if="disabledCount > 0">，不上架 {{ disabledCount }} 个</span>
              ｜ 价格区间 <span class="tnum">{{ pricePreview }}</span>
            </span>
            <el-button size="small" @click="generateCodes">按标题生成编码</el-button>
          </div>
        </div>
      </template>

      <el-table :data="skuTable" max-height="460">
        <el-table-column label="上架" width="70">
          <template #default="{ row }">
            <el-checkbox v-model="row.enabled" />
          </template>
        </el-table-column>
        <el-table-column prop="label" label="规格组合" min-width="180" />
        <el-table-column label="商家编码" width="200">
          <template #default="{ row }">
            <el-input v-model="row.skuCode" size="small" :disabled="!row.enabled" maxlength="64" />
          </template>
        </el-table-column>
        <el-table-column label="价格（元）" width="140">
          <template #default="{ row }">
            <el-input-number
              v-model="row.price"
              size="small"
              :min="0.01"
              :precision="2"
              :controls="false"
              :disabled="!row.enabled"
              class="w110"
            />
          </template>
        </el-table-column>
        <el-table-column label="重量（克）" width="130">
          <template #default="{ row }">
            <el-input-number
              v-model="row.weightG"
              size="small"
              :min="1"
              :precision="0"
              :controls="false"
              :disabled="!row.enabled"
              class="w110"
            />
          </template>
        </el-table-column>
        <el-table-column label="封面图" width="110" align="center">
          <template #default="{ row }">
            <ImageUploader
              v-model="row.coverImage"
              biz="products"
              size="small"
              :disabled="!row.enabled"
            />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <div class="submit-bar">
      <el-button size="large" @click="router.push('/')">取消</el-button>
      <el-button type="primary" size="large" :loading="submitting" @click="onSubmit">
        保存为草稿
      </el-button>
    </div>
  </div>
</template>

<style scoped>
/* 三张卡片用统一的纵向间隔串起来，替代原来散落的 .mt16 */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.hint {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.hint .tnum {
  color: var(--color-price);
  font-weight: var(--weight-medium);
}

/* --------------------------------------------------------------------------
 * 规格组编辑器
 *
 * 每个规格组做成一块有底色的小面板 —— "模块化"在这里是字面意思：
 * 一组规格就是一个可独立增删的模块，边界清楚比分隔线更好读。
 * ------------------------------------------------------------------------*/

.group {
  padding: var(--space-3) var(--space-4) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.group + .group {
  margin-top: var(--space-3);
}

.group-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.values {
  margin-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  align-items: flex-start;
}

.value-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
}

.image-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.w110 {
  width: 110px;
}

.w180 {
  width: 180px;
}

.w200 {
  width: 200px;
}

.w280 {
  width: 280px;
}

.w360 {
  width: 360px;
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
  width: 32px;
  height: 32px;
}

/* 主操作固定在右下，整页只有一个终点 */
.submit-bar {
  margin-top: var(--space-5);
  display: flex;
  justify-content: flex-end;
  gap: var(--space-3);
}

@media (max-width: 900px) {
  .w180,
  .w200,
  .w280,
  .w360 {
    width: 100%;
  }

  .value-row,
  .image-row {
    flex-wrap: wrap;
  }
}
</style>
