<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import { fetchCategoryTree, searchProducts, type Category, type SearchSort, type SpuCard } from '@/api/product'
import { formatPriceRange, yuanToFen } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const router = useRouter()

const categories = ref<Category[]>([])
const items = ref<SpuCard[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const nextCursor = ref<string | null>(null)
const hasMore = ref(false)
const searched = ref(false)

const filters = reactive({
  keyword: '',
  categoryId: '' as string,
  priceFrom: undefined as number | undefined,
  priceTo: undefined as number | undefined,
  sort: 'relevance' as SearchSort,
})

const sortOptions: { label: string; value: SearchSort }[] = [
  { label: '综合', value: 'relevance' },
  { label: '销量', value: 'sales' },
  { label: '最新', value: 'newest' },
  { label: '价格从低到高', value: 'price_asc' },
  { label: '价格从高到低', value: 'price_desc' },
]

// el-cascader 用 id/name/children 这套字段
const cascaderProps = {
  value: 'id',
  label: 'name',
  children: 'children',
  checkStrictly: true,
  emitPath: false,
} as const

const priceRange = computed(() => {
  const { priceFrom, priceTo } = filters
  if (priceFrom === undefined && priceTo === undefined) return '不限'
  if (priceFrom !== undefined && priceTo !== undefined) return `¥${priceFrom} ~ ¥${priceTo}`
  if (priceFrom !== undefined) return `¥${priceFrom} 以上`
  return `¥${priceTo} 以下`
})

const resultText = computed(() => {
  if (!searched.value) return '正在加载…'
  if (items.value.length === 0) return '没有找到符合条件的商品'
  // hasMore 为真时总数还不确定，所以说"已显示"而不是"共"
  return hasMore.value ? `已显示 ${items.value.length} 件商品` : `共 ${items.value.length} 件商品`
})

async function load(reset: boolean): Promise<void> {
  if (reset) {
    loading.value = true
    nextCursor.value = null
  } else {
    loadingMore.value = true
  }

  try {
    const result = await searchProducts({
      keyword: filters.keyword || undefined,
      categoryId: filters.categoryId || undefined,
      // 用户在输入框里填的是元，传给后端要换成「分」
      priceFrom: filters.priceFrom === undefined ? undefined : yuanToFen(filters.priceFrom),
      priceTo: filters.priceTo === undefined ? undefined : yuanToFen(filters.priceTo),
      sort: filters.sort,
      cursor: reset ? null : nextCursor.value,
      limit: 12,
    })

    items.value = reset ? result.items : [...items.value, ...result.items]
    nextCursor.value = result.nextCursor
    hasMore.value = result.hasMore
    searched.value = true
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function onSearch(): void {
  void load(true)
}

function onReset(): void {
  filters.keyword = ''
  filters.categoryId = ''
  filters.priceFrom = undefined
  filters.priceTo = undefined
  filters.sort = 'relevance'
  void load(true)
}

function openDetail(spu: SpuCard): void {
  void router.push({ name: 'product-detail', params: { spuId: spu.id } })
}

onMounted(async () => {
  try {
    categories.value = await fetchCategoryTree()
  } catch {
    // 类目拉不到不影响商品列表，静默即可
  }
  await load(true)
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <el-input
        v-model="filters.keyword"
        placeholder="搜索商品，多个关键词用空格分隔"
        clearable
        class="f-keyword"
        @keyup.enter="onSearch"
      />
      <el-cascader
        v-model="filters.categoryId"
        :options="categories"
        :props="cascaderProps"
        placeholder="全部分类"
        clearable
        class="f-category"
      />
      <div class="f-price">
        <el-input-number
          v-model="filters.priceFrom"
          :min="0"
          :controls="false"
          placeholder="最低价"
        />
        <span class="dash">—</span>
        <el-input-number v-model="filters.priceTo" :min="0" :controls="false" placeholder="最高价" />
      </div>
      <el-select v-model="filters.sort" class="f-sort" @change="onSearch">
        <el-option v-for="o in sortOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <div class="f-actions">
        <el-button type="primary" @click="onSearch">搜索</el-button>
        <el-button @click="onReset">重置</el-button>
      </div>
    </div>

    <div class="result-bar">
      <span class="result-count">{{ resultText }}</span>
      <span class="result-hint">价格区间：{{ priceRange }}</span>
    </div>

    <div v-loading="loading" class="shelf-wrap">
      <el-empty v-if="searched && items.length === 0" description="没有找到符合条件的商品" />

      <div v-else class="shelf">
        <article
          v-for="item in items"
          :key="item.id"
          class="card"
          tabindex="0"
          @click="openDetail(item)"
          @keyup.enter="openDetail(item)"
        >
          <div class="card-media">
            <img :src="item.mainImage" :alt="item.title" loading="lazy" @error="onImageError" />
          </div>
          <div class="card-body">
            <h3 class="card-title" :title="item.title">{{ item.title }}</h3>
            <div class="card-price tnum">{{ formatPriceRange(item.priceMin, item.priceMax) }}</div>
            <div class="card-meta">
              <span class="tnum">已售 {{ item.totalSold }}</span>
              <span v-if="item.reviewCount > 0" class="tnum">{{ item.avgScore.toFixed(1) }} 分</span>
            </div>
          </div>
        </article>
      </div>

      <div v-if="hasMore" class="more">
        <el-button :loading="loadingMore" @click="load(false)">加载更多</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* --------------------------------------------------------------------------
 * 筛选工具栏
 * ------------------------------------------------------------------------*/

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.f-keyword {
  width: 280px;
}

.f-category {
  width: 190px;
}

.f-price {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 240px;
}

.f-price :deep(.el-input-number) {
  width: 100%;
}

.dash {
  color: var(--color-text-placeholder);
  flex: 0 0 auto;
}

.f-sort {
  width: 150px;
}

/* 主操作推到最右，工具栏左右分组更清晰 */
.f-actions {
  display: flex;
  gap: var(--space-2);
  margin-left: auto;
}

/* --------------------------------------------------------------------------
 * 结果计数
 * ------------------------------------------------------------------------*/

.result-bar {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-4);
  margin: var(--space-4) var(--space-1) var(--space-3);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.result-count {
  color: var(--color-text-secondary);
}

/* --------------------------------------------------------------------------
 * 货架
 *
 * "货架感"来自**严格的行列对齐**，不是装饰：
 *   - 图片区用 aspect-ratio 锁死比例，列宽变化时所有图仍然等高
 *   - 标题固定两行高度，整排卡片的标题槽位一致
 *   - card-price 用 margin-top:auto 压到底部，价格行跨卡片对齐
 * 少了任何一条，卡片就会参差不齐，"货架"立刻散架。
 * ------------------------------------------------------------------------*/

.shelf-wrap {
  min-height: 200px;
}

.shelf {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--space-4);
}

.card {
  display: flex;
  flex-direction: column;
  background: var(--card-bg);
  border: var(--card-border);
  border-radius: var(--card-radius);
  overflow: hidden;
  cursor: pointer;
  transition:
    border-color var(--dur) var(--ease-out),
    box-shadow var(--dur) var(--ease-out),
    transform var(--dur) var(--ease-out);
}

.card:hover,
.card:focus-visible {
  border-color: var(--color-border-strong);
  box-shadow: var(--card-shadow-hover);
  transform: translateY(-2px);
}

.card-media {
  aspect-ratio: 1 / 1;
  background: var(--media-bg);
  overflow: hidden;
}

.card-media img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform var(--dur-slow) var(--ease-out);
}

.card:hover .card-media img {
  transform: scale(1.03);
}

.card-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4) var(--space-4);
}

.card-title {
  /* 固定两行的高度：这是整排卡片能对齐的关键 */
  height: calc(var(--text-base) * var(--leading-snug) * 2);
  font-size: var(--text-base);
  font-weight: var(--weight-normal);
  line-height: var(--leading-snug);
  color: var(--color-text);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.card-price {
  /* 把价格行顶到卡片底部，跨卡片对齐 */
  margin-top: auto;
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  line-height: var(--leading-tight);
  color: var(--color-price);
}

.card-meta {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.more {
  margin-top: var(--space-6);
  text-align: center;
}

/* --------------------------------------------------------------------------
 * 响应式
 * ------------------------------------------------------------------------*/

@media (max-width: 768px) {
  .f-keyword,
  .f-category,
  .f-price,
  .f-sort {
    width: 100%;
  }

  .f-actions {
    width: 100%;
    margin-left: 0;
  }

  .shelf {
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: var(--space-3);
  }
}
</style>
