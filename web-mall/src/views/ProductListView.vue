<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { fetchShops } from '@/api/auth'
import { isBizError } from '@/api/errors'
import { fetchCategoryTree, searchProducts, type Category, type SearchSort, type SpuCard } from '@/api/product'
import BannerCarousel from '@/components/BannerCarousel.vue'
import { formatPriceRange, yuanToFen } from '@/utils/money'
import { onImageError, thumbFallback, thumbSrc } from '@/utils/placeholder'

const router = useRouter()
const route = useRoute()

const categories = ref<Category[]>([])
const items = ref<SpuCard[]>([])
/** shopId → 店铺名。卡片上任它显示卖家是谁 */
const shopNames = ref<Record<string, string>>({})
const loading = ref(false)
const loadingMore = ref(false)
const nextCursor = ref<string | null>(null)
const hasMore = ref(false)
/** 符合条件的总数。只有首页返回，翻页时保持不变 */
const total = ref<number | null>(null)
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
  // ★ 用服务端给的总数。它只在第一页返回（见后端 with_total），
  //   所以翻页途中这里一直是首页那个值，不会跳。
  //   拿不到才退回"已显示" —— 别把"这一页 12 件"说成总数，那是另一回事。
  return total.value !== null ? `共 ${total.value} 件商品` : `已显示 ${items.value.length} 件商品`
})

/**
 * 解析这批商品所属的店铺名，显示在卡片上。
 *
 * ★ **一次批量请求**，不要每张卡各发一个 —— 一页十来个商品往往只来自一两个店，
 *   去重之后通常只有一个 id 要查。已经查过的直接跳过，"加载更多"不会重复请求。
 *
 * 店名只是附加信息：拉不到只是卡片上少一行，不该弹错、更不该拖住列表。
 */
async function loadShopNames(list: SpuCard[]): Promise<void> {
  const missing = [...new Set(list.map((item) => item.shopId))].filter(
    (id) => shopNames.value[id] === undefined,
  )
  if (missing.length === 0) return
  try {
    for (const shop of await fetchShops(missing)) {
      shopNames.value[shop.id] = shop.name
    }
  } catch {
    // 静默降级
  }
}

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
    // 只有首页带 total；翻页响应里是 null，不能拿它覆盖已经拿到手的值
    if (reset) total.value = result.total
    searched.value = true
    // 不 await：商品先渲染出来，店名稍后补上，别为了附加信息拖住列表
    void loadShopNames(result.items)
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

/**
 * 商品卡片一律在**新标签页**打开（照淘宝的做法）。
 *
 * 详情页会顶掉列表页，用户看完一款想再看别款就得靠浏览器后退 ——
 * 新标签页让列表始终留在原处，"返回上一级"这个问题就不存在了。
 * 卡片是真 `<a>`，于是中键 / Ctrl+点击这些浏览器原生行为也都跟着有。
 */
function detailHref(spu: SpuCard): string {
  return router.resolve({ name: 'product-detail', params: { spuId: spu.id } }).href
}

onMounted(async () => {
  // 先认 URL 上的类目（banner / 分享链接会带过来），再拉第一屏
  const fromQuery = route.query.categoryId
  if (typeof fromQuery === 'string') filters.categoryId = fromQuery

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

    <div v-loading="loading" class="shelf-wrap">
      <el-empty v-if="searched && items.length === 0" description="没有找到符合条件的商品" />

      <div v-else class="shelf">
        <a
          v-for="item in items"
          :key="item.id"
          class="card"
          :href="detailHref(item)"
          target="_blank"
          rel="noopener"
        >
          <div class="card-media">
            <img
              :src="thumbSrc(item.mainImage, item.mainImageMid, item.title)"
              :data-fallback-src="thumbFallback(item.mainImage, item.mainImageMid)"
              :alt="item.title"
              loading="lazy"
              @error="onImageError"
            />
          </div>
          <div class="card-body">
            <h3 class="card-title" :title="item.title">{{ item.title }}</h3>
            <div
              v-if="shopNames[item.shopId]"
              class="card-shop"
              :title="shopNames[item.shopId]"
            >
              {{ shopNames[item.shopId] }}
            </div>
            <div class="card-price tnum">{{ formatPriceRange(item.priceMin, item.priceMax) }}</div>
            <div class="card-meta">
              <span class="tnum">已售 {{ item.totalSold }}</span>
              <span v-if="item.reviewCount > 0" class="tnum">{{ item.avgScore.toFixed(1) }} 分</span>
            </div>
          </div>
        </a>
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
  /* 卡片是 <a>，把链接的默认样式抹掉 */
  text-decoration: none;
  color: inherit;
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

/* 卖家。锁单行省略 —— 店名长短不一会把同一排卡片撑得参差 */
.card-shop {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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
