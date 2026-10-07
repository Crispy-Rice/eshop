<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { fetchShop, type ShopInfo } from '@/api/auth'
import { isBizError } from '@/api/errors'
import { searchProducts, type SearchSort, type SpuCard } from '@/api/product'
import { TICKET_SOURCE } from '@/api/support'
import ProductGrid from '@/components/ProductGrid.vue'
import { useShelf } from '@/composables/useShelf'
import { onImageError } from '@/utils/placeholder'

/**
 * 店铺页：店招 + 这家店的商品。
 *
 * ★ 商品列表**不另写查询**，走的是搜索那条路（`GET /api/search?shopId=…`）。
 *   后端本来就有 `shop_id` 这个过滤条件，列表与计数共用一份 —— 另写一条必然漏掉
 *   已下架 / 已软删的过滤，表现就是"店铺页陈列着已下架商品"。
 * ★ 一期**不做类目筛选**：店铺往往只挂几十件、横跨几个类目，把全站类目树铺过来
 *   多半点出空结果。排序 + 翻页够了；接口本来就支持 `categoryId`，将来要加不难。
 */
const route = useRoute()
const router = useRouter()
const shopId = computed(() => String(route.params.shopId))

const shop = ref<ShopInfo | null>(null)
const shopLoading = ref(false)
const notFound = ref(false)

const items = ref<SpuCard[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const hasMore = ref(false)
const nextCursor = ref<string | null>(null)
const total = ref<number | null>(null)

/**
 * 默认「最新上架」，**不是**「综合」。
 *
 * ★ 一期的「综合」在后端就是**销量降序**（没有相关度模型，见 `SORT_SPECS`）。
 *   店铺页没有关键词，对一个没有查询词的页面说"综合排序"没有意义 ——
 *   进店的人想看的是"这家店最近上了什么"。所以这里显式选 `newest`，
 *   而不是继承接口的默认值。
 */
const sort = ref<SearchSort>('newest')
const SORTS: { label: string; value: SearchSort }[] = [
  { label: '最新上架', value: 'newest' },
  { label: '销量', value: 'sales' },
  { label: '价格从低到高', value: 'price_asc' },
  { label: '价格从高到低', value: 'price_desc' },
]

const resultText = computed(() => (total.value === null ? '' : `共 ${total.value} 件商品`))

async function load(reset: boolean): Promise<void> {
  if (reset) {
    loading.value = true
    nextCursor.value = null
  } else {
    loadingMore.value = true
  }

  try {
    const result = await searchProducts({
      shopId: shopId.value,
      sort: sort.value,
      cursor: reset ? null : nextCursor.value,
      limit: pageSize.value,
    })
    items.value = reset ? result.items : [...items.value, ...result.items]
    nextCursor.value = result.nextCursor
    hasMore.value = result.hasMore
    // 只有首页带 total；翻页响应里是 null，不能覆盖已经拿到手的值
    if (reset) total.value = result.total
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

/** 页大小由网格量出来，量完顺手带出第一屏 —— 所以 onMounted 不自己拉（见 useShelf） */
const { pageSize, onGridPageSize, ensureLoaded } = useShelf((reset) => void load(reset))

function onSortChange(): void {
  void load(true)
}

/**
 * 联系客服：深链到客服页，带上 shopId 与一个带店名的标题。
 *
 * ★ 与商品详情页那个入口同一条路（`docs/19 §5.1`：开会话的逻辑只有一处，
 *   各入口只负责"把上下文放进 URL"）。这里没有商品上下文，所以**不带 spuId** ——
 *   店小蜜据此知道买家问的是"这家店"而不是某件商品。
 * ★ 没登录时会被路由守卫带去登录页，回来接着开会话；与商品页那个按钮一致。
 */
function contactShop(): void {
  if (!shop.value) return
  void router.push({
    name: 'support',
    query: {
      shopId: shop.value.id,
      source: TICKET_SOURCE.OTHER,
      subject: `关于「${shop.value.name}」`,
    },
  })
}

/**
 * 拉店铺信息。
 *
 * ★ 拉不到（店铺不存在 / 已删）就整页走空状态，**不弹错误提示** ——
 *   用户点进来看到"店铺不存在"，比一个红条加一片空白清楚得多。
 * ★ 店铺**不看 status**：一期没有"停用店铺"这个机制（docs/18 §5.1），
 *   所以这里不存在"已停用的店照常营业"。哪天加了停用，这个页面要一起改。
 */
async function loadShop(): Promise<void> {
  shopLoading.value = true
  try {
    shop.value = await fetchShop(shopId.value)
  } catch {
    notFound.value = true
  } finally {
    shopLoading.value = false
  }
}

onMounted(async () => {
  await loadShop()
  if (notFound.value) return
  // 兜底：网格没量出列数时至少把第一屏拉出来（正常路径下它已经拉过了）
  ensureLoaded()
})
</script>

<template>
  <div class="page">
    <el-empty v-if="notFound" description="店铺不存在或已关闭" />

    <template v-else>
      <!-- 店招。与商品卡用同一套 --card-* 语言，读起来是"这家店"而不是另一套 UI -->
      <div v-loading="shopLoading" class="head-wrap">
        <section v-if="shop" class="shop-head">
          <img
            v-if="shop.logo"
            :src="shop.logo"
            class="shop-logo"
            alt=""
            @error="onImageError"
          />
          <span v-else class="shop-logo shop-logo-fallback">{{ shop.name.slice(0, 1) }}</span>
          <div class="shop-info">
            <h1 class="shop-name">{{ shop.name }}</h1>
            <p v-if="shop.description" class="shop-desc">{{ shop.description }}</p>
          </div>
          <!-- 店招上唯一的动作。库存/客服这类"要问人"的事，进店的人第一眼就该找得到 -->
          <button type="button" class="shop-service" @click="contactShop">联系客服</button>
        </section>
      </div>

      <div class="result-bar">
        <span class="result-count">{{ resultText }}</span>
        <el-select v-model="sort" class="sort" @change="onSortChange">
          <el-option v-for="o in SORTS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
      </div>

      <ProductGrid
        :items="items"
        :loading="loading"
        :loading-more="loadingMore"
        :has-more="hasMore"
        empty-text="这家店还没有上架商品"
        :show-shop-name="false"
        @load-more="load(false)"
        @page-size="onGridPageSize"
      />
    </template>
  </div>
</template>

<style scoped>
/* 店招在加载中也占住高度，不然拉到之后整页会往下跳一下 */
.head-wrap {
  min-height: 104px;
}

.shop-head {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-5);
  background: var(--card-bg);
  border: var(--card-border);
  border-radius: var(--card-radius);
}

.shop-logo {
  width: 64px;
  height: 64px;
  flex: 0 0 auto;
  border-radius: var(--radius-md);
}

/* 「联系客服」：描边小胶囊，靠 `margin-left:auto` 推到店招最右边。
   与商品详情页那个同名按钮同一套语言 —— 它跳出当前页去客服会话，
   和「进店逛逛」这类站内跳转不是一回事 */
.shop-service {
  flex: 0 0 auto;
  margin-left: auto;
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  background: var(--color-bg-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  cursor: pointer;
  transition:
    color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out);
}

.shop-service:hover {
  color: var(--color-accent);
  border-color: var(--color-accent);
}

img.shop-logo {
  object-fit: cover;
  border: 1px solid var(--color-border);
  background: var(--color-bg-inset);
}

/* 没传 logo 时用店名首字当字母头像 —— 比一块灰方块像样得多 */
.shop-logo-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
}

.shop-info {
  min-width: 0;
}

.shop-name {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.shop-desc {
  margin-top: var(--space-2);
  font-size: var(--text-sm);
  line-height: var(--leading-normal);
  color: var(--color-text-secondary);
}

.result-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  margin: var(--space-5) 0 var(--space-4);
  font-size: var(--text-sm);
}

.result-count {
  color: var(--color-text-secondary);
}

.sort {
  width: 150px;
}
</style>
