<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { fetchCategoryTree, type Category } from '@/api/category'
import { isBizError } from '@/api/errors'
import { fetchPublicSpu, searchProducts } from '@/api/product'
import {
  SCOPE_ALL,
  SCOPE_CATEGORY,
  SCOPE_SHOP,
  SCOPE_SKU,
  SCOPE_TYPE_TEXT,
  searchShops,
  type ShopOption,
} from '@/api/promotion'

/**
 * 「适用范围」选择器：全场 / 指定商品 / 指定类目 / 指定店铺。
 *
 * 券与活动共用。两边后端是**同一套契约**（`scopeType` + `scopeValue`），
 * 而且踩过同一个坑：值是**雪花 ID，必须全程 string** —— `Number()` 一下就会
 * 静默截断成另一个数，存进去之后算价永远匹配不上，而界面上看着一切正常。
 *
 * 用法：`v-model:type` 管"哪一档"，`@change` 抛"最终生效的 id 列表"（全场抛 `null`）。
 * **只管输入**：券和活动都只有"新建"、没有编辑，所以不做"从 id 反推选择状态"那一套。
 */
const type = defineModel<number>('type', { required: true })
const emit = defineEmits<{ (e: 'change', v: string[] | null): void }>()

interface PickerOption {
  id: string
  label: string
}

const categories = ref<Category[]>([])

// ---------- 商品与规格 ----------
const spuIds = ref<string[]>([])
const skuIds = ref<string[]>([])
const spuResults = ref<PickerOption[]>([])
const spuSearching = ref(false)
/** 搜索结果一换，旧的选项就没了；把见过的标签记下来，
 *  否则"已选中但不在这一页结果里"的目标会退化成裸 id */
const spuLabels = ref<Record<string, string>>({})
/** 已选商品的规格，按商品分组 —— 匹配的是 SKU，必须落到这一层 */
const skuGroups = ref<{ spuId: string; title: string; skus: PickerOption[] }[]>([])

// ---------- 类目 ----------
const categoryIds = ref<string[]>([])
/** 多选 + 允许选中间层。算价会命中该类的**全部子类目**，所以选「图书」就够 */
const categoryCascaderProps = {
  multiple: true,
  checkStrictly: true,
  emitPath: false,
  label: 'name',
  value: 'id',
  children: 'children',
}

// ---------- 店铺 ----------
const shopIds = ref<string[]>([])
const shopResults = ref<PickerOption[]>([])
const shopSearching = ref(false)
const shopLabels = ref<Record<string, string>>({})

const SHOP_STATUS_HINT: Record<number, string> = { 2: '已关闭', 3: '审核中' }

function withSelected(
  results: PickerOption[],
  selected: string[],
  labels: Record<string, string>,
): PickerOption[] {
  const seen = new Set(results.map((o) => o.id))
  const missing = selected
    .filter((id) => !seen.has(id))
    .map((id) => ({ id, label: labels[id] ?? id }))
  return [...results, ...missing]
}

const spuOptions = computed(() => withSelected(spuResults.value, spuIds.value, spuLabels.value))
const shopOptions = computed(() => withSelected(shopResults.value, shopIds.value, shopLabels.value))

async function searchSpu(keyword: string): Promise<void> {
  spuSearching.value = true
  try {
    // 用公开搜索（只出在售商品）：它不挑角色，admin 与 finance 都能用，
    // 而优惠本来就该建给"买得到的东西"
    const page = await searchProducts({ keyword, limit: 20 })
    spuResults.value = page.items.map((s) => {
      spuLabels.value[s.id] = s.title
      return { id: s.id, label: s.title }
    })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '搜索商品失败')
  } finally {
    spuSearching.value = false
  }
}

async function searchShop(keyword: string): Promise<void> {
  shopSearching.value = true
  try {
    shopResults.value = (await searchShops(keyword)).map((s: ShopOption) => {
      const hint = SHOP_STATUS_HINT[s.status]
      const label = hint ? `${s.name}（${hint}）` : s.name
      shopLabels.value[s.id] = label
      return { id: s.id, label }
    })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '搜索店铺失败')
  } finally {
    shopSearching.value = false
  }
}

// 商品一变就重取规格。整组重建而不是增量维护：选中的商品是个位数，
// 增量更新反而容易留下指向已移除商品的孤儿选项。
watch(
  () => [...spuIds.value],
  async (ids) => {
    try {
      skuGroups.value = await Promise.all(
        ids.map(async (id) => {
          const detail = await fetchPublicSpu(id)
          return {
            spuId: id,
            title: detail.title,
            skus: detail.skus.map((k) => ({
              id: k.id,
              label: k.specText || k.skuCode || '默认规格',
            })),
          }
        }),
      )
    } catch (e) {
      ElMessage.error(isBizError(e) ? e.message : '读取商品规格失败')
      return
    }
    // 商品被移除后，它的规格不该还留在"已选"里
    const alive = new Set(skuGroups.value.flatMap((g) => g.skus.map((s) => s.id)))
    skuIds.value = skuIds.value.filter((id) => alive.has(id))
  },
)

/** 某个商品一个规格都没勾 = 要它的全部规格 */
function expandedSkuIds(): string[] {
  const out = new Set<string>()
  for (const group of skuGroups.value) {
    const chosen = group.skus.filter((s) => skuIds.value.includes(s.id))
    for (const s of chosen.length > 0 ? chosen : group.skus) out.add(s.id)
  }
  return [...out]
}

/**
 * 切档就把已选目标清掉。
 *
 * ★ 留着"上一次选的东西"最坏：切到「指定商品」时下拉里躺着上一轮的选项，
 *   运营以为那是空的、直接提交，结果活动被限到了他没打算限的商品上。
 */
watch(type, (t) => {
  spuIds.value = []
  skuIds.value = []
  categoryIds.value = []
  shopIds.value = []
  // 刚切到某一档时先铺一批候选，省得运营面对空下拉框不知道该打字
  if (t === SCOPE_SKU && spuResults.value.length === 0) void searchSpu('')
  if (t === SCOPE_SHOP && shopResults.value.length === 0) void searchShop('')
})

/** 最终生效的目标。全场给 `null` —— 后端两个接口都是这个约定 */
const payload = computed<string[] | null>(() => {
  if (type.value === SCOPE_ALL) return null
  if (type.value === SCOPE_CATEGORY) return categoryIds.value
  if (type.value === SCOPE_SHOP) return shopIds.value
  return expandedSkuIds()
})

watch(payload, (v) => emit('change', v))

onMounted(async () => {
  try {
    categories.value = await fetchCategoryTree()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载类目失败')
  }
})
</script>

<template>
  <el-form-item label="适用范围">
    <el-select v-model="type" style="width: 100%">
      <el-option
        v-for="(text, value) in SCOPE_TYPE_TEXT"
        :key="value"
        :label="text"
        :value="Number(value)"
      />
    </el-select>
  </el-form-item>

  <!-- 指定类目：用级联，能选到二级 / 三级。
       原来是个平铺的 select，只列了顶层 —— 子类目根本选不到 -->
  <el-form-item v-if="type === SCOPE_CATEGORY" label="指定类目">
    <el-cascader
      v-model="categoryIds"
      :options="categories"
      :props="categoryCascaderProps"
      filterable
      clearable
      collapse-tags
      collapse-tags-tooltip
      style="width: 100%"
    />
    <div class="form-hint">含该类的全部子类目（选「图书」就覆盖它下面的所有子类）</div>
  </el-form-item>

  <!-- 指定商品：先挑商品，再挑规格 —— 算价是按 SKU 匹配的
       （pricing 里是 `item.sku_id in scope_value`），只挑到商品层不够 -->
  <template v-else-if="type === SCOPE_SKU">
    <el-form-item label="指定商品">
      <el-select
        v-model="spuIds"
        multiple
        filterable
        remote
        reserve-keyword
        :remote-method="searchSpu"
        :loading="spuSearching"
        placeholder="搜商品名"
        style="width: 100%"
        collapse-tags
        collapse-tags-tooltip
      >
        <el-option v-for="o in spuOptions" :key="o.id" :label="o.label" :value="o.id" />
      </el-select>
      <div class="form-hint">只列在售商品</div>
    </el-form-item>
    <el-form-item label="指定规格">
      <el-select
        v-model="skuIds"
        multiple
        filterable
        :disabled="spuIds.length === 0"
        placeholder="不选就是该商品的全部规格"
        style="width: 100%"
        collapse-tags
        collapse-tags-tooltip
      >
        <el-option-group v-for="g in skuGroups" :key="g.spuId" :label="g.title">
          <el-option v-for="s in g.skus" :key="s.id" :label="s.label" :value="s.id" />
        </el-option-group>
      </el-select>
      <div class="form-hint">
        优惠是按规格匹配的，所以要落到规格这一层；某个商品不选规格就是它的全部规格
      </div>
    </el-form-item>
  </template>

  <el-form-item v-else-if="type === SCOPE_SHOP" label="指定店铺">
    <el-select
      v-model="shopIds"
      multiple
      filterable
      remote
      reserve-keyword
      :remote-method="searchShop"
      :loading="shopSearching"
      placeholder="搜店铺名"
      style="width: 100%"
      collapse-tags
      collapse-tags-tooltip
    >
      <el-option v-for="o in shopOptions" :key="o.id" :label="o.label" :value="o.id" />
    </el-select>
  </el-form-item>
</template>

<style scoped>
/* 每个页面自带一份 —— 与项目里其它视图的做法一致（scoped 样式不进子组件，
   所以要跟着组件走，不能指望父组件那份） */
.form-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
</style>
