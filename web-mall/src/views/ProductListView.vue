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
  <div>
    <el-card class="filters" shadow="never">
      <div class="filter-row">
        <el-input
          v-model="filters.keyword"
          placeholder="搜索商品，多个关键词用空格分隔"
          clearable
          class="keyword"
          @keyup.enter="onSearch"
        />
        <el-cascader
          v-model="filters.categoryId"
          :options="categories"
          :props="cascaderProps"
          placeholder="全部分类"
          clearable
          class="category"
        />
        <el-input-number v-model="filters.priceFrom" :min="0" :controls="false" placeholder="最低价" class="price" />
        <span class="dash">—</span>
        <el-input-number v-model="filters.priceTo" :min="0" :controls="false" placeholder="最高价" class="price" />
        <el-select v-model="filters.sort" class="sort" @change="onSearch">
          <el-option v-for="o in sortOptions" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-button type="primary" @click="onSearch">搜索</el-button>
        <el-button @click="onReset">重置</el-button>
      </div>
      <div class="filter-hint">当前价格区间：{{ priceRange }}</div>
    </el-card>

    <div v-loading="loading" class="grid-wrap">
      <el-empty v-if="searched && items.length === 0" description="没有找到符合条件的商品" />

      <div v-else class="grid">
        <el-card
          v-for="item in items"
          :key="item.id"
          class="product"
          shadow="hover"
          :body-style="{ padding: '0' }"
          @click="openDetail(item)"
        >
          <img :src="item.mainImage" :alt="item.title" class="cover" @error="onImageError" />
          <div class="info">
            <div class="title" :title="item.title">{{ item.title }}</div>
            <div class="price">{{ formatPriceRange(item.priceMin, item.priceMax) }}</div>
            <div class="meta">
              <span>已售 {{ item.totalSold }}</span>
              <span v-if="item.reviewCount > 0">{{ item.avgScore.toFixed(1) }} 分</span>
            </div>
          </div>
        </el-card>
      </div>

      <div v-if="hasMore" class="more">
        <el-button :loading="loadingMore" @click="load(false)">加载更多</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.filters {
  margin-bottom: 20px;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.keyword {
  width: 260px;
}

.category {
  width: 200px;
}

.price {
  width: 110px;
}

.dash {
  color: #c0c4cc;
}

.sort {
  width: 150px;
}

.filter-hint {
  margin-top: 10px;
  font-size: 12px;
  color: #909399;
}

.grid-wrap {
  min-height: 200px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 16px;
}

.product {
  cursor: pointer;
  overflow: hidden;
}

.cover {
  width: 100%;
  height: 210px;
  object-fit: cover;
  display: block;
  background: #f5f7fa;
}

.info {
  padding: 12px;
}

.title {
  font-size: 14px;
  line-height: 1.4;
  height: 39px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.price {
  margin-top: 8px;
  color: #d93025;
  font-size: 16px;
  font-weight: 600;
}

.meta {
  margin-top: 6px;
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: #909399;
}

.more {
  margin-top: 24px;
  text-align: center;
}
</style>
