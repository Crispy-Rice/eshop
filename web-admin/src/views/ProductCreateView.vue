<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { fetchCategoryTree, type Category } from '@/api/category'
import { isBizError } from '@/api/errors'
import {
  createSpu,
  fetchMySpu,
  replaceSpuSpecs,
  type SkuIn,
  type SpuCreateInput,
} from '@/api/product'
import ImageUploader from '@/components/ImageUploader.vue'
import { formatYuan, yuanToFen } from '@/utils/money'

const route = useRoute()
const router = useRouter()

/**
 * 有 ``spuId`` 路由参数就是**替换规格**模式 —— 这一个组件两种用途。
 *
 * ★ 替换模式只换规格与 SKU；标题 / 类目 / 主图拉出来是为了给商家上下文，
 *   提交时**不发**（那些走编辑页的「保存基本信息」）。
 */
const replaceSpuId = computed(() => (route.params.spuId ? String(route.params.spuId) : null))
const isReplace = computed(() => replaceSpuId.value !== null)

let seq = 0
const uid = (): string => `k${Date.now().toString(36)}${(seq++).toString(36)}`

interface ValueRow {
  key: string
  value: string
  image: string
  /**
   * 这个规格值在服务端的 id。**只有"替换规格"预填时才有** ——
   * 用它把已有 SKU 反查回它属于哪个规格组合（前端的 ``key`` 是现场生成的，
   * 跟服务端 id 没有关系，光看 key 认不出来）。
   */
  specValueId?: string
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
const loading = ref(false)
const formRef = ref<FormInstance>()

const form = reactive({
  categoryId: '' as string,
  title: '',
  subTitle: '',
  // ★ 空串，不是占位图。以前这里预填一段灰色 SVG 的 data URI 来满足"非空"校验，
  //   结果它被当成真实图片存进了库，一路传染到购物车和订单快照 —— 前端兜底只认
  //   空值，对"一张能成功加载的灰图"无能为力。图可以之后再补，占位图不能进库。
  mainImage: '',
})

const groups = ref<GroupRow[]>([
  { key: uid(), name: '颜色', values: [{ key: uid(), value: '', image: '' }] },
])

const rules: FormRules = {
  categoryId: [{ required: true, message: '请选择末级类目', trigger: 'change' }],
  title: [{ required: true, message: '请输入商品标题', trigger: 'blur' }],
  // 主图不设为必填：允许"先发布、后补图"。空图在商城由展示层兜底成占位图，
  // 而**占位图本身不能进库** —— 那会变成一张骗过 onImageError 的灰块。
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
          coverImage: '',
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

// ---------- 校验 ----------
//
// ★ 逐格的问题（编码 / 价格 / 重量）**就地标红**，不再攒成一条顶部 toast。
//   原来那版一次只报第一处，而且只说"哪个规格组合"、不说哪一格 —— 商家还得自己低头
//   在十几个输入框里找。现在每一格自己的问题就写在那格下面。
//
//   整页层面的问题（没有规格组、没有 SKU）仍然走顶部提示：它们在表格里没有落脚的一格。

/** 首次点保存之后才开始标红 —— 否则一进页面整张表就是红的 */
const submitted = ref(false)

/** 出现超过一次的商家编码。跨行的问题，但落在每一行自己的编码格上 */
const dupCodes = computed(() => {
  const seen = new Map<string, number>()
  for (const row of enabledSkus.value) {
    const code = row.skuCode.trim()
    if (code) seen.set(code, (seen.get(code) ?? 0) + 1)
  }
  return new Set([...seen].filter(([, n]) => n > 1).map(([code]) => code))
})

/**
 * 每个 SKU 行的问题，按 comboKey 索引。
 *
 * 用 computed 而不是"手动 set / clear"：改好一格，那格的红色立刻自己消失，不用额外写
 * 清理逻辑，也不会留下过期的错误。
 */
const skuErrors = computed<Record<string, { skuCode?: string; price?: string; weightG?: string }>>(
  () => {
    const map: Record<string, { skuCode?: string; price?: string; weightG?: string }> = {}
    for (const row of enabledSkus.value) {
      const errs: { skuCode?: string; price?: string; weightG?: string } = {}
      // ★ 编码是**选填**的：空着不算错，只有"填了却和别的行撞了"才标红
      const code = row.skuCode.trim()
      if (code && dupCodes.value.has(code)) errs.skuCode = '编码重复'
      if (row.price === undefined || row.price <= 0) errs.price = '要大于 0'
      // 重量是运费唯一的输入（运费引擎按首重 / 续重计费），这一格不能空
      if (!row.weightG || row.weightG <= 0) errs.weightG = '要大于 0'
      map[row.comboKey] = errs
    }
    return map
  },
)

/** 表格里还有没有没填好的格子 */
const hasSkuErrors = computed(() =>
  Object.values(skuErrors.value).some((e) => e.skuCode || e.price || e.weightG),
)

function cellError(row: SkuRow, field: 'skuCode' | 'price' | 'weightG'): string | undefined {
  return submitted.value ? skuErrors.value[row.comboKey]?.[field] : undefined
}

/** 整页层面的问题：表格里没有对应的一格，只能顶部提示 */
function validatePage(): string | null {
  if (effectiveGroups.value.length === 0) return '至少需要一个填好的规格组'
  if (skuTable.value.length === 0) return '没有生成任何 SKU'
  if (skuTable.value.length > MAX_SKUS) return `SKU 数量不能超过 ${MAX_SKUS}`
  if (enabledSkus.value.length === 0) return '至少要上架一个 SKU'
  return null
}

async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  const pageError = validatePage()
  if (pageError) {
    ElMessage.warning(pageError)
    return
  }

  // 点了保存才让格子里的问题显形，然后让它们自己说明问题，不再攒成一条 toast
  submitted.value = true
  if (hasSkuErrors.value) {
    ElMessage.warning('SKU 明细里有标红的格子，补齐后再保存')
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
      // 空编码发 null 而不是空串：后端把空串归一成 NULL，这里显式传 null 更直白
      skuCode: row.skuCode.trim() || null,
      specValueKeys: row.keys,
      price: yuanToFen(row.price ?? 0),
      coverImage: row.coverImage.trim(),
      weightG: row.weightG ?? 0,
    })),
  }

  submitting.value = true
  try {
    if (replaceSpuId.value) {
      // 只发规格那两段 —— 标题 / 类目 / 主图不归这里改
      await replaceSpuSpecs(replaceSpuId.value, {
        specGroups: payload.specGroups,
        skus: payload.skus,
      })
      ElMessage.success('规格已替换')
      await router.push({ name: 'product-edit', params: { spuId: replaceSpuId.value } })
    } else {
      const created = await createSpu(payload)
      ElMessage.success('商品已保存为草稿，去列表页提交审核')
      await router.push({ name: 'product-edit', params: { spuId: created.id } })
    }
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : isReplace.value ? '替换失败' : '发布失败')
  } finally {
    submitting.value = false
  }
}

/**
 * 替换模式：把商品现有的标题 / 类目 / 主图、**规格组与取值**、
 * **以及每个 SKU 的价格 / 重量 / 编码 / 封面**预填进来。
 *
 * ★ 每个规格值都要**重新生成前端 key**：``comboKey`` 依赖前端生成的 key，
 *   而服务端返回的是雪花 id，两者没有关系。为了把已有 SKU 认回它属于哪个组合，
 *   这里把服务端 id 记在 ``ValueRow.specValueId`` 上再做反查 ——
 *   **不能靠顺序猜**，服务端返回 id 的次序没有承诺。
 */
async function loadForReplace(spuId: string): Promise<void> {
  loading.value = true
  try {
    const spu = await fetchMySpu(spuId)
    form.categoryId = spu.categoryId
    form.title = spu.title
    form.subTitle = spu.subTitle ?? ''
    form.mainImage = spu.mainImage
    groups.value = spu.specGroups.map((group) => ({
      key: uid(),
      name: group.name,
      values: group.values.map((value) => ({
        key: uid(),
        value: value.value,
        image: value.image ?? '',
        specValueId: value.id,
      })),
    }))

    // 把 SKU 认回各自的组合：每个规格组里找出"它的 id 在该 SKU 的 specValueIds 里"
    // 的那个取值。某个规格组找不到就整行丢弃 —— 宁可让商家重填，也不编造一个组合。
    const seeded: Record<string, SkuRow> = {}
    for (const sku of spu.skus) {
      const ids = new Set(sku.specValueIds)
      const keys = groups.value.map(
        (group) => group.values.find((v) => v.specValueId !== undefined && ids.has(v.specValueId))?.key,
      )
      if (keys.some((k) => k === undefined)) continue
      const comboKey = (keys as string[]).join('|')
      seeded[comboKey] = {
        comboKey,
        // label 会被下面那个 watch 用新算出来的文案覆盖，这里只是占位
        label: sku.specText,
        keys: keys as string[],
        enabled: true,
        skuCode: sku.skuCode,
        price: sku.price / 100, // 分 → 元
        coverImage: sku.coverImage,
        weightG: sku.weightG,
      }
    }
    // ★ 顺序：先给 groups、再给 skuRows。上面那个 watch 是异步刷新的，
    //   等它跑的时候两边都已就位，于是按 comboKey 把预填值保留下来。
    skuRows.value = seeded
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载商品失败')
    void router.replace({ name: 'products' })
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    categories.value = await fetchCategoryTree()
  } catch {
    ElMessage.error('加载类目失败，请确认后端已启动且平台已建好类目')
  }
  if (replaceSpuId.value) await loadForReplace(replaceSpuId.value)
})
</script>

<template>
  <div v-loading="loading" class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>{{ isReplace ? '修改规格' : '发布商品' }}</span>
          <el-button @click="router.push('/')">返回列表</el-button>
        </div>
      </template>

      <!-- 替换模式：把"会发生什么"说在前面。下面那几项只做上下文展示，不提交 -->
      <el-alert v-if="isReplace" type="warning" :closable="false" class="mb16">
        <template #title>
          保存后会整体替换这个商品的规格与 SKU，原来的规格组合作废；
          标题、类目、主图不受影响。规格与价格已按现状填好，改完直接保存即可
          （新加的组合要自己填价格与重量）。
        </template>
      </el-alert>

      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="末级类目" prop="categoryId">
          <el-cascader
            v-model="form.categoryId"
            :options="categories"
            :props="cascaderProps"
            placeholder="只能选到末级类目"
            :disabled="isReplace"
            class="w360"
          />
        </el-form-item>
        <el-form-item label="商品标题" prop="title">
          <el-input v-model="form.title" maxlength="120" show-word-limit :disabled="isReplace" />
        </el-form-item>
        <el-form-item label="副标题">
          <el-input v-model="form.subTitle" maxlength="255" :disabled="isReplace" />
        </el-form-item>
        <el-form-item label="主图" prop="mainImage">
          <div class="image-row">
            <ImageUploader v-model="form.mainImage" biz="products" :disabled="isReplace" />
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
            <el-input
              v-model="row.skuCode"
              size="small"
              placeholder="选填"
              :disabled="!row.enabled"
              maxlength="64"
              :class="{ 'is-invalid': cellError(row, 'skuCode') }"
            />
            <div v-if="cellError(row, 'skuCode')" class="cell-error">
              {{ cellError(row, 'skuCode') }}
            </div>
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
              :class="{ 'is-invalid': cellError(row, 'price') }"
            />
            <div v-if="cellError(row, 'price')" class="cell-error">
              {{ cellError(row, 'price') }}
            </div>
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
              :class="{ 'is-invalid': cellError(row, 'weightG') }"
            />
            <div v-if="cellError(row, 'weightG')" class="cell-error">
              {{ cellError(row, 'weightG') }}
            </div>
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
        {{ isReplace ? '保存规格' : '保存为草稿' }}
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

/* 行内校验：出问题的那一格描红，原因就写在那格下面，不用回头找是哪一行 */
.is-invalid :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--color-danger) inset;
}

.cell-error {
  margin-top: 2px;
  font-size: var(--text-xs);
  line-height: 1.4;
  color: var(--color-danger);
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
