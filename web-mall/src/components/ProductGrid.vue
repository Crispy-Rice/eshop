<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { fetchShops } from '@/api/auth'
import type { SpuCard } from '@/api/product'
import { formatPriceRange } from '@/utils/money'
import { onImageError, thumbFallback, thumbSrc } from '@/utils/placeholder'

/**
 * 商品货架：网格 + 卡片 + 加载更多 + 空状态。搜索页与店铺页共用。
 *
 * ★ 抽出来的理由是**卡片会腐烂**：这几十行模板与 CSS 里全是"货架感"的讲究
 *   （图片锁比例、标题固定两行、价格 `margin-top:auto` 压底），复制一份到店铺页，
 *   下次给卡片加个促销角标就只改了其中一处。
 *
 * ★ 它**也负责量列数**（见 `useShelf`）：列数只有网格自己算得出来，而页大小
 *   要靠它。所以这里 emit `page-size`，页面把它落成请求的 `limit`。
 *
 * ★ 它**不发商品请求**：拿什么数据是页面的事（搜索页带关键词、店铺页带 shopId），
 *   组件只管画。
 */
const props = withDefaults(
  defineProps<{
    items: SpuCard[]
    /** 首屏加载中（盖住整片网格） */
    loading?: boolean
    loadingMore?: boolean
    hasMore?: boolean
    /** 空结果时说的那句话。两个页面的说法不一样 */
    emptyText?: string
    /**
     * 卡片上是否显示店名。
     * ★ 店铺页要关掉：那里每张卡都是同一家店，逐张重复店名只是噪声。
     */
    showShopName?: boolean
    /** 一屏铺几行。页大小 = 列数 × 它 */
    shelfRows?: number
  }>(),
  {
    loading: false,
    loadingMore: false,
    hasMore: false,
    emptyText: '没有找到符合条件的商品',
    showShopName: true,
    shelfRows: 3,
  },
)

const emit = defineEmits<{
  'load-more': []
  /** 当前铺得下多少张。页面拿它当请求的 `limit` */
  'page-size': [size: number]
}>()

const router = useRouter()

/** shopId → 店铺名 */
const shopNames = ref<Record<string, string>>({})

/**
 * 解析这批商品所属的店铺名。
 *
 * ★ **一次批量请求**，不要每张卡各发一个 —— 一页十来个商品往往只来自一两个店，
 *   去重之后通常只有一个 id 要查。已经查过的直接跳过，"加载更多"不会重复请求。
 *
 * 店名只是附加信息：拉不到只是卡片上少一行，不该弹错、更不该拖住列表。
 * 所以这里**跟着 items 变**（而不是让页面在请求回来后手动调一次）——
 * 页面因此完全不用关心这件事。
 */
async function loadShopNames(list: SpuCard[]): Promise<void> {
  if (!props.showShopName) return
  const missing = [...new Set(list.map((item) => item.shopId))].filter(
    (id) => shopNames.value[id] === undefined,
  )
  if (missing.length === 0) return
  try {
    for (const shop of await fetchShops(missing)) {
      shopNames.value[shop.id] = shop.name
    }
  } catch {
    // 静默降级：卡片上少一行店名而已
  }
}

watch(() => props.items, (list) => void loadShopNames(list), { immediate: true })

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

const shelfEl = ref<HTMLElement | null>(null)

/**
 * 网格当前几列。视口一变就变，所以每次现测，不能缓存。
 *
 * ★ 只认**算出 px 的轨道表**。元素不可见（display:none）时 `getComputedStyle` 给的是
 *   **声明值** `repeat(auto-fill, minmax(200px, 1fr))` —— 按空格切正好是 3 段，
 *   会被当成"3 列"，算出一个**看着很合理**的页大小（3 × 3 行 = 9），一路静默错下去。
 *   不是 px 就这一轮不量：宁可用兜底页大小，也不把错的发出去。
 */
function measure(): void {
  const el = shelfEl.value
  if (!el) return
  const tracks = getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean)
  if (!tracks.length || !tracks.every((t) => t.endsWith('px'))) return
  emit('page-size', tracks.length * props.shelfRows)
}

onMounted(() => {
  // ★ 先量再让页面发第一次请求：它拿这个值当 limit
  measure()
  window.addEventListener('resize', measure)
})

onBeforeUnmount(() => window.removeEventListener('resize', measure))
</script>

<template>
  <div v-loading="loading" class="shelf-wrap">
    <el-empty v-if="!loading && items.length === 0" :description="emptyText" />

    <!-- ★ 网格**必须一直在 DOM 里、且一直是 `display:grid`** —— 列数只有布局算得出来。
         这里早先挂着 `v-show="!(items.length === 0 && !loading)"`，而"空结果且不在加载中"
         恰恰就是**首屏挂载那一刻**的状态：网格是 display:none，量到的是隐藏元素的声明值，
         于是"3 列 × 3 行 = 9"，首屏只拉 9 个商品、还要点「加载更多」才补齐。
         空网格高度是 0，与 el-empty 并存没有任何视觉影响，就让它一直显示。 -->
    <div ref="shelfEl" class="shelf">
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
          <div v-if="showShopName && shopNames[item.shopId]" class="card-shop" :title="shopNames[item.shopId]">
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
      <el-button :loading="loadingMore" @click="emit('load-more')">加载更多</el-button>
    </div>
  </div>
</template>

<style scoped>
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
</style>
