<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import { fetchCategoryTree, searchProducts, type Category, type SearchSort, type SpuCard } from '@/api/product'
import BannerCarousel from '@/components/BannerCarousel.vue'
import ProductGrid from '@/components/ProductGrid.vue'
import { useShelf } from '@/composables/useShelf'
import { yuanToFen } from '@/utils/money'

const router = useRouter()
const route = useRoute()

const categories = ref<Category[]>([])
const items = ref<SpuCard[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const nextCursor = ref<string | null>(null)
const hasMore = ref(false)
/** 符合条件的总数。只有首页返回，翻页时保持不变 */
const total = ref<number | null>(null)
const searched = ref(false)

const filters = reactive({
  keyword: '',
  // ★ 类目要在**setup 期**就从 URL 认下来，不能等 onMounted：
  //   第一屏是网格量完列数后触发的（见 useShelf），那时 onMounted 还没跑 ——
  //   晚一步认，带 `?categoryId=x` 进来会先拉一屏全量结果。
  categoryId: typeof route.query.categoryId === 'string' ? route.query.categoryId : '',
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
  // ★ 用服务端给的总数。它只在第一页返回（见后端 with_total），
  //   所以翻页途中这里一直是首页那个值，不会跳。
  //   拿不到才退回"已显示" —— 别把"这一页 12 件"说成总数，那是另一回事。
  return total.value !== null ? `共 ${total.value} 件商品` : `已显示 ${items.value.length} 件商品`
})

/**
 * 一页铺几行、页大小是多少，**都交给网格自己量**（见 `useShelf` 与 `ProductGrid`）。
 *
 * ★ 量完它 emit 一次，`onGridPageSize` 顺手把第一屏拉出来 —— 所以下面的
 *   `onMounted` **不再自己拉第一屏**，否则同一屏会请求两次。
 */
const { pageSize, onGridPageSize, ensureLoaded } = useShelf((reset) => void load(reset))

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
      limit: pageSize.value,
    })

    items.value = reset ? result.items : [...items.value, ...result.items]
    nextCursor.value = result.nextCursor
    hasMore.value = result.hasMore
    // 只有首页带 total；翻页响应里是 null，不能拿它覆盖已经拿到手的值
    if (reset) total.value = result.total
    searched.value = true
    // 店名由 ProductGrid 自己跟着 items 去解析（一次批量请求、按 id 去重），
    // 页面不用管 —— 那是卡片的事，不是搜索的事
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

/**
 * 当前选中类目的**祖先链**（含自身）。
 *
 * ★ 用它反查选中状态，而不是另存一份"当前点的是哪个胶囊"：`filters.categoryId`
 *   是唯一事实来源，所以用上面搜索栏的 cascader 选一个类目，下面的胶囊也会跟着亮。
 */
const activeChain = computed<Category[]>(() => {
  const id = filters.categoryId
  if (!id) return []

  const walk = (nodes: Category[], trail: Category[]): Category[] | null => {
    for (const node of nodes) {
      const next = [...trail, node]
      if (node.id === id) return next
      const found = walk(node.children ?? [], next)
      if (found) return found
    }
    return null
  }
  return walk(categories.value, []) ?? []
})

const activeIds = computed(() => new Set(activeChain.value.map((c) => c.id)))

/**
 * 逐层展开的类目行：第一行固定是一级类目，之后每一行是"选中节点"的下一层。
 * 选一级 → 出二级；再选二级 → 出三级。最多三行（类目最多三级）。
 */
const catRows = computed<Category[][]>(() => {
  const rows: Category[][] = [categories.value]
  let children = activeChain.value[0]?.children ?? []
  for (let depth = 0; depth < 2 && children.length > 0; depth += 1) {
    rows.push(children)
    children = activeChain.value[depth + 1]?.children ?? []
  }
  return rows
})

/** 点胶囊 = 设类目 + 立刻搜。后端会把选中类目的整棵子树一起算上 */
function pickCategory(id: string): void {
  filters.categoryId = id
  onSearch()
}

/**
 * 类目同步进 URL。
 *
 * ★ 这样「banner 指向 /?categoryId=xxx」这种站内链接才有意义 —— 否则跳过来
 *   只是换了个 query，列表照样是全量。顺带筛选结果可以刷新保留 / 直接分享。
 */
watch(
  () => filters.categoryId,
  (id) => {
    // 已经对上了就不用再 replace —— 反向 watcher 回写时两次会撞在一起
    if ((route.query.categoryId ?? '') === (id || '')) return
    void router.replace({ query: { ...route.query, categoryId: id || undefined } })
  },
)

/**
 * 反向同步：URL 上的类目变了，跟着筛选。
 *
 * ★ 少了这一条，**首页轮播图点「类目」型 banner 是没反应的** —— banner 就挂在
 *   首页，点击是 push 到 `/?categoryId=xxx`，路由没换页、组件不重挂，
 *   `onMounted` 只读一次 query，于是列表纹丝不动。
 *   顺带把浏览器前进/后退也接上了。
 */
watch(
  () => route.query.categoryId,
  (value) => {
    const id = typeof value === 'string' ? value : ''
    if (id === filters.categoryId) return
    filters.categoryId = id
    void load(true)
  },
)

function onReset(): void {
  filters.keyword = ''
  filters.categoryId = ''
  filters.priceFrom = undefined
  filters.priceTo = undefined
  filters.sort = 'relevance'
  void load(true)
}

onMounted(async () => {
  try {
    categories.value = await fetchCategoryTree()
  } catch {
    // 类目拉不到不影响商品列表，静默即可
  }
  // ★ 第一屏**不在这里拉**：网格比页面先挂载，它量完列数就 emit 了一次，
  //   那次已经把第一屏带出来了（见 useShelf）。这里只是兜住"网格一个都没量出来"
  ensureLoaded()
})
</script>

<template>
  <div class="page">
    <!-- 轮播图：App 壳里的营销带在它上方，所以这里天然是"营销带下方" -->
    <BannerCarousel />

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

    <!-- 类目导航：按层展开，点任意一层即筛商品（后端会连整棵子树一起算）。
         放在搜索栏下面、商品网格上面 —— 紧挨着它筛选的那片结果，"点了会变"最直观 -->
    <nav v-if="categories.length > 0" class="cat-nav">
      <div
        v-for="(row, depth) in catRows"
        :key="depth"
        class="cat-row"
        :class="{ sub: depth > 0 }"
        :style="{ '--depth': depth }"
      >
        <button
          v-if="depth === 0"
          type="button"
          class="cat-chip"
          :class="{ picked: !filters.categoryId }"
          @click="pickCategory('')"
        >
          全部
        </button>
        <button
          v-for="c in row"
          :key="c.id"
          type="button"
          class="cat-chip"
          :class="{ picked: activeIds.has(c.id) }"
          @click="pickCategory(c.id)"
        >
          {{ c.name }}
        </button>
      </div>
    </nav>

    <div class="result-bar">
      <span class="result-count">{{ resultText }}</span>
      <span class="result-hint">价格区间：{{ priceRange }}</span>
    </div>

    <ProductGrid
      :items="items"
      :loading="loading"
      :loading-more="loadingMore"
      :has-more="hasMore"
      empty-text="没有找到符合条件的商品"
      @load-more="load(false)"
      @page-size="onGridPageSize"
    />
  </div>
</template>

<style scoped>
/* --------------------------------------------------------------------------
 * 筛选工具栏
 * ------------------------------------------------------------------------*/

/* --------------------------------------------------------------------------
 * 类目导航
 *
 * 一行一层：第一行是一级类目，往下跟着当前选中的祖先链逐层展开。
 * 用胶囊而不是树：层数最多三级，胶囊在首屏占的高度可控，也不会把商品网格挤到折叠线以下。
 * ------------------------------------------------------------------------*/

.cat-nav {
  display: flex;
  flex-direction: column;
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  /* 内边距交给每一行自己，行与行之间的分隔线才能通到两侧 */
  overflow: hidden;
}

.cat-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-1) var(--space-2);
  padding: var(--space-3) var(--space-4);
}

/*
 * 二级及更深的一行：换成子面板。
 *
 * ★ 原来只给了 16px 左缩进，读起来就是"第一行没排满、又接了一行"——
 *   和上面那条一级行之间看不出从属关系，所以显脏。
 *   现在给它浅底 + 一条分隔线 + 随层级递增的缩进：
 *   一眼能看到"这行是我刚点的那个类目的下一层"，而不是另一组一级类目。
 */
.cat-row.sub {
  background: var(--color-bg-subtle);
  border-top: 1px solid var(--color-border);
  padding-left: calc(var(--space-4) + var(--depth) * var(--space-4));
}

.cat-chip {
  padding: var(--space-1) var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    color var(--dur-fast) var(--ease-out),
    background-color var(--dur-fast) var(--ease-out);
}

.cat-chip:hover {
  color: var(--color-accent);
  background: var(--color-accent-soft);
}

.cat-chip.picked {
  background: var(--color-accent);
  color: var(--color-accent-contrast);
}

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

/* 货架（网格 + 卡片 + 加载更多）的样式全在 `ProductGrid.vue` 里 —— 搜索页与店铺页
   共用同一份。"货架感"那几条讲究（图片锁比例、标题两行、价格压底）只维护一处，
   否则下次给卡片加个促销角标就会只改其中一边 */

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
