<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { fetchShop, type ShopInfo } from '@/api/auth'
import { addToCart } from '@/api/cart'
import { isBizError } from '@/api/errors'
import { fetchSkuStock } from '@/api/inventory'
import { fetchSpu, type SkuDetail, type SpuDetail } from '@/api/product'
import {
  FILTERS,
  SORTS,
  fetchSpuReviewStats,
  fetchSpuReviews,
  type Review,
  type ReviewFilter,
  type ReviewSort,
  type ReviewStats,
} from '@/api/review'
import StarRating from '@/components/StarRating.vue'
import { useAuthStore } from '@/stores/auth'
import { useCartStore } from '@/stores/cart'
import { formatDateTime, formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const cart = useCartStore()

const spu = ref<SpuDetail | null>(null)
/** 卖这件商品的店铺。匿名可读，拉不到就不显示这一块 */
const shop = ref<ShopInfo | null>(null)
const loading = ref(false)
const notFound = ref(false)
const quantity = ref(1)
/** groupId → valueId */
const selected = ref<Record<string, string>>({})

const spuId = computed(() => String(route.params.spuId))

/**
 * 某个规格值当前是否可选。
 *
 * 判断依据：是否存在一个 SKU，它包含这个值，**并且**包含其它规格组已选中的值。
 * 只看"其它组"是刻意的——同一组里两个值不可能同时出现在一个 SKU 上。
 */
function isValueAvailable(groupId: string, valueId: string): boolean {
  const skus = spu.value?.skus ?? []
  return skus.some((sku) => {
    if (!sku.specValueIds.includes(valueId)) return false
    return Object.entries(selected.value).every(
      ([gid, vid]) => gid === groupId || sku.specValueIds.includes(vid),
    )
  })
}

/** 选满所有规格组后命中的 SKU */
const currentSku = computed<SkuDetail | null>(() => {
  const groups = spu.value?.specGroups ?? []
  // filter(Boolean) 不会收窄类型，用类型谓词让 TS 知道这里已经没有 undefined
  const chosen = groups
    .map((g) => selected.value[g.id])
    .filter((v): v is string => Boolean(v))
  if (chosen.length !== groups.length || groups.length === 0) return null
  return spu.value?.skus.find((sku) => chosen.every((v) => sku.specValueIds.includes(v))) ?? null
})

const allSelected = computed(() => {
  const groups = spu.value?.specGroups ?? []
  return groups.length > 0 && groups.every((g) => selected.value[g.id])
})

/**
 * 选完之后如果某个规格组只剩一个可选值，自动补上。
 * 比如只有"黑色 256G"可选时，用户点一个颜色就应该直接把容量也带上。
 */
function autoSelectSingleOptions(): void {
  let changed = true
  let guard = 0
  while (changed && guard++ < 10) {
    changed = false
    for (const group of spu.value?.specGroups ?? []) {
      if (selected.value[group.id]) continue
      const available = group.values.filter((v) => isValueAvailable(group.id, v.id))
      const only = available[0]
      if (available.length === 1 && only) {
        selected.value[group.id] = only.id
        changed = true
      }
    }
  }
}

function pickValue(groupId: string, valueId: string): void {
  if (!isValueAvailable(groupId, valueId)) return

  if (selected.value[groupId] === valueId) {
    delete selected.value[groupId]
  } else {
    selected.value[groupId] = valueId
    autoSelectSingleOptions()
  }
}

/**
 * 进页面时默认选中一组规格。
 *
 * 逐组挑"当前还可选"的第一个值，**每选一个就重算后面几组的可选性** ——
 * 之所以不能简单取各组第一个，是因为那可能落到"原色钛 + 512G"这种根本没生产的
 * 组合上，页面一进来就是"该组合不可选"，比不预选还糟。
 *
 * 这样贪心选出来的组合必定对应一个真实 SKU：每一步的候选值都被某个同时含
 * 之前所有已选值的 SKU 见证，归纳下去最后一组也有同一个 SKU 兜住。
 */
function selectFirstAvailable(): void {
  for (const group of spu.value?.specGroups ?? []) {
    const first = group.values.find((v) => isValueAvailable(group.id, v.id))
    if (first) selected.value[group.id] = first.id
  }
}

const displayPrice = computed(() => {
  if (currentSku.value) return `¥${formatYuan(currentSku.value.price)}`
  if (!spu.value) return ''
  return spu.value.priceMin === spu.value.priceMax
    ? `¥${formatYuan(spu.value.priceMin)}`
    : `¥${formatYuan(spu.value.priceMin)} ~ ¥${formatYuan(spu.value.priceMax)}`
})

const mainImage = computed(() => currentSku.value?.coverImage || spu.value?.mainImage || '')

/** 库存档位文案。后端只给档位不给真实库存（docs/03 §9）。 */
const stockText = ref<string | null>(null)

// 评价区
const reviewStats = ref<ReviewStats | null>(null)
const reviews = ref<Review[]>([])
const reviewSort = ref<ReviewSort>('latest')
const reviewFilter = ref<ReviewFilter>('all')
const reviewCursor = ref<string | null>(null)
const reviewMore = ref(false)
const reviewLoadingMore = ref(false)

watch(currentSku, async (sku) => {
  if (!sku) {
    stockText.value = null
    return
  }
  try {
    stockText.value = (await fetchSkuStock(sku.id)).text
  } catch {
    // 库存查不到不该挡住浏览商品，静默降级为不展示
    stockText.value = null
  }
})

const skuHint = computed(() => {
  if (currentSku.value) return `已选：${currentSku.value.specText}（${currentSku.value.skuCode}）`
  if (allSelected.value) return '该规格组合暂不可售，请换一个组合'
  return `请选择 ${spu.value?.specGroups.map((g) => g.name).join(' / ') ?? ''}`
})

const adding = ref(false)

async function onAddToCart(): Promise<void> {
  if (!currentSku.value) {
    ElMessage.warning('请先选择完整规格')
    return
  }
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: route.fullPath } })
    return
  }

  adding.value = true
  try {
    await addToCart(currentSku.value.id, quantity.value)
    // 角标要跟着变，否则用户看不出加成功了
    await cart.refresh()
    ElMessage.success(`已加入购物车（${quantity.value} 件）`)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加入购物车失败')
  } finally {
    adding.value = false
  }
}

/**
 * 拉店铺信息。
 *
 * ★ 这一块是**锦上添花**，拉不到不能影响商品页本身：店铺被删、接口 404，
 *   都只是不显示，不弹错。所以这里自己吞掉异常。
 */
async function loadShop(shopId: string): Promise<void> {
  try {
    shop.value = await fetchShop(shopId)
  } catch {
    shop.value = null
  }
}

async function load(): Promise<void> {
  loading.value = true
  notFound.value = false
  selected.value = {}
  quantity.value = 1
  shop.value = null
  try {
    spu.value = await fetchSpu(spuId.value)
    selectFirstAvailable()
    // 上面已经选满一组；这里兜住"某个组一个可选值都没有"的畸形 SKU 矩阵
    autoSelectSingleOptions()
    await loadShop(spu.value.shopId)
    await loadReviews(true)
  } catch (e) {
    spu.value = null
    if (isBizError(e) && e.code === 'NOT_FOUND') {
      notFound.value = true
    } else {
      ElMessage.error(isBizError(e) ? e.message : '加载失败')
    }
  } finally {
    loading.value = false
  }
}

// ============================================================
// 评价区
// ============================================================
/**
 * 评价是**公开接口**，未登录也能看 —— 否则游客进来一片空白，
 * 反而降低了转化。加载失败也不该让整个详情页报错，所以单独 try。
 */
async function loadReviews(reset = true): Promise<void> {
  if (reset) {
    reviewCursor.value = null
    reviewLoadingMore.value = false
  }
  try {
    const [stats, page] = await Promise.all([
      fetchSpuReviewStats(spuId.value),
      fetchSpuReviews(spuId.value, {
        sort: reviewSort.value,
        filter: reviewFilter.value,
        cursor: reset ? undefined : (reviewCursor.value ?? undefined),
        limit: reset ? 3 : 10,
      }),
    ])
    reviewStats.value = stats
    reviews.value = reset ? page.items : [...reviews.value, ...page.items]
    reviewCursor.value = page.nextCursor
    reviewMore.value = page.hasMore
  } catch {
    // 评价拉不到不影响买货，静默降级
    reviewStats.value = null
    reviews.value = []
  }
}

async function loadMoreReviews(): Promise<void> {
  reviewLoadingMore.value = true
  try {
    await loadReviews(false)
  } finally {
    reviewLoadingMore.value = false
  }
}

async function changeSort(value: ReviewSort): Promise<void> {
  if (reviewSort.value === value) return
  reviewSort.value = value
  await loadReviews(true)
}

async function changeFilter(value: ReviewFilter): Promise<void> {
  if (reviewFilter.value === value) return
  reviewFilter.value = value
  await loadReviews(true)
}

onMounted(load)
watch(spuId, load)
</script>

<template>
  <div v-loading="loading" class="page">
    <el-result
      v-if="notFound"
      icon="warning"
      title="商品不存在或已下架"
      sub-title="它可能已经被下架，或者链接不对"
    >
      <template #extra>
        <el-button type="primary" @click="router.push({ name: 'home' })">回到商品列表</el-button>
      </template>
    </el-result>

    <template v-else-if="spu">
      <el-breadcrumb class="crumb" separator="/">
        <el-breadcrumb-item :to="{ name: 'home' }">全部商品</el-breadcrumb-item>
        <el-breadcrumb-item>{{ spu.title }}</el-breadcrumb-item>
      </el-breadcrumb>

      <div class="panel">
        <div class="gallery">
          <img :src="mainImage" :alt="spu.title" class="main-image" @error="onImageError" />
        </div>

        <div class="info">
          <h1 class="title">{{ spu.title }}</h1>
          <p v-if="spu.subTitle" class="subtitle">{{ spu.subTitle }}</p>

          <div class="price-box">
            <span class="price tnum">{{ displayPrice }}</span>
            <span class="sold tnum">已售 {{ spu.totalSold }}</span>
            <span v-if="stockText" class="stock tnum">{{ stockText }}</span>
          </div>

          <div v-for="group in spu.specGroups" :key="group.id" class="spec-group">
            <div class="spec-label">{{ group.name }}</div>
            <div class="spec-values">
              <button
                v-for="value in group.values"
                :key="value.id"
                type="button"
                class="spec-value"
                :class="{
                  active: selected[group.id] === value.id,
                  disabled: !isValueAvailable(group.id, value.id),
                }"
                :disabled="!isValueAvailable(group.id, value.id)"
                :title="isValueAvailable(group.id, value.id) ? value.value : '该规格组合暂不可选'"
                @click="pickValue(group.id, value.id)"
              >
                <img v-if="value.image" :src="value.image" :alt="value.value" class="swatch" />
                {{ value.value }}
              </button>
            </div>
          </div>

          <div class="spec-group">
            <div class="spec-label">数量</div>
            <el-input-number v-model="quantity" :min="1" :max="200" />
          </div>

          <p class="sku-line" :class="{ ok: !!currentSku }">{{ skuHint }}</p>

          <el-button
            type="primary"
            size="large"
            class="buy"
            :loading="adding"
            @click="onAddToCart"
          >
            加入购物车
          </el-button>

          <!-- 卖家是谁。店铺接口匿名可读，没登录也看得到 -->
          <div v-if="shop" class="shop">
            <img v-if="shop.logo" :src="shop.logo" class="shop-logo" alt="" @error="onImageError" />
            <span v-else class="shop-logo shop-logo-fallback">{{ shop.name.slice(0, 1) }}</span>
            <div class="shop-text">
              <div class="shop-name">{{ shop.name }}</div>
              <div v-if="shop.description" class="shop-desc">{{ shop.description }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- ---------- 评价区 ---------- -->
      <section class="reviews">
        <header class="reviews-head">
          <h2 class="reviews-title">商品评价</h2>
          <!-- ★ 没有评价时不显示好评率，也不显示 5.0 分（后端返回 null） -->
          <div v-if="reviewStats && reviewStats.reviewCount > 0" class="reviews-summary">
            <div class="score-box">
              <span class="score tnum">{{ reviewStats.avgScore?.toFixed(1) }}</span>
              <StarRating :score="reviewStats.avgScore ?? 0" size="sm" />
              <span class="score-sub tnum">{{ reviewStats.reviewCount }} 条评价</span>
            </div>
            <div class="good-box">
              <span class="good-rate tnum">{{ Math.round((reviewStats.goodRate ?? 0) * 100) }}%</span>
              <span class="good-label">好评率</span>
            </div>
          </div>
          <span v-else-if="reviewStats" class="reviews-empty-hint">暂无评价</span>
        </header>

        <template v-if="reviewStats && reviewStats.reviewCount > 0">
          <!-- 排序与筛选。三种组合各有一条部分索引，翻页都是游标 -->
          <div class="reviews-bar">
            <div class="chips">
              <button
                v-for="s in SORTS"
                :key="s.value"
                type="button"
                class="chip"
                :class="{ on: reviewSort === s.value }"
                @click="changeSort(s.value)"
              >
                {{ s.label }}
              </button>
            </div>
            <div class="chips">
              <button
                v-for="f in FILTERS"
                :key="f.value"
                type="button"
                class="chip"
                :class="{ on: reviewFilter === f.value }"
                @click="changeFilter(f.value)"
              >
                {{ f.label }}
              </button>
            </div>
          </div>

          <p v-if="reviews.length === 0" class="reviews-none">这个筛选下还没有评价</p>

          <ul class="review-list">
            <li v-for="r in reviews" :key="r.reviewId" class="review">
              <div class="review-head">
                <StarRating :score="r.score" size="sm" />
                <span class="review-user">{{ r.nickname }}</span>
                <span class="review-time">{{ formatDateTime(r.createdAt) }}</span>
              </div>
              <p v-if="r.content" class="review-content">{{ r.content }}</p>
              <div v-if="r.images.length" class="review-thumbs">
                <img
                  v-for="img in r.images"
                  :key="img.path"
                  :src="img.thumbUrl"
                  alt="评价图片"
                  @error="onImageError"
                />
              </div>
              <p class="review-spec">{{ r.specText }}</p>

              <!-- 追评紧随首评，标注出来 -->
              <div v-if="r.followUp" class="follow-up">
                <span class="follow-tag">追评</span>
                <span class="follow-time">{{ formatDateTime(r.followUp.createdAt) }}</span>
                <p class="follow-content">{{ r.followUp.content }}</p>
                <div v-if="r.followUp.images.length" class="review-thumbs">
                  <img
                    v-for="img in r.followUp.images"
                    :key="img.path"
                    :src="img.thumbUrl"
                    alt="追评图片"
                    @error="onImageError"
                  />
                </div>
              </div>

              <div v-if="r.replies.length" class="review-replies">
                <div v-for="(reply, i) in r.replies" :key="i" class="review-reply">
                  <span class="reply-label">{{ reply.replyTypeText }}</span>
                  <span class="reply-text">{{ reply.content }}</span>
                </div>
              </div>
            </li>
          </ul>

          <div v-if="reviewMore" class="reviews-more">
            <el-button size="small" :loading="reviewLoadingMore" @click="loadMoreReviews">
              查看全部评价
            </el-button>
          </div>
        </template>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page {
  min-height: 320px;
}

.crumb {
  margin-bottom: var(--space-4);
}

.panel {
  display: flex;
  gap: var(--space-8);
  flex-wrap: wrap;
  padding: var(--space-6);
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

/* ---------- 主图 ---------- */

.gallery {
  flex: 0 0 400px;
  max-width: 100%;
}

.main-image {
  width: 100%;
  aspect-ratio: 1 / 1;
  object-fit: cover;
  border-radius: var(--radius-md);
  background: var(--media-bg);
}

/* ---------- 右侧信息 ---------- */

.info {
  flex: 1;
  min-width: 320px;
}

.title {
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.subtitle {
  margin-top: var(--space-2);
  font-size: var(--text-base);
  color: var(--color-text-tertiary);
}

/* 价格面板：整页唯一的强色区域，视线第一落点 */
.price-box {
  display: flex;
  align-items: baseline;
  gap: var(--space-4);
  margin: var(--space-5) 0;
  padding: var(--space-4) var(--space-5);
  background: var(--color-price-soft);
  border: 1px solid color-mix(in srgb, var(--color-price) 18%, transparent);
  border-radius: var(--radius-md);
}

.price {
  font-size: var(--text-3xl);
  font-weight: var(--weight-semibold);
  line-height: var(--leading-tight);
  color: var(--color-price);
}

.sold {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

/* 库存档位：用警示色，但不抢价格的视觉权重 */
.stock {
  margin-left: auto;
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-warning);
}

/* ---------- 规格选择 ---------- */

.spec-group {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}

.spec-label {
  flex: 0 0 56px;
  font-size: var(--text-sm);
  line-height: 36px;
  color: var(--color-text-tertiary);
}

.spec-values {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.spec-value {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-4);
  font-size: var(--text-sm);
  line-height: 20px;
  color: var(--color-text);
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease-out),
    background-color var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out);
}

.spec-value:hover:not(.disabled) {
  border-color: var(--color-accent);
  color: var(--color-accent);
}

/* 选中态用强调色 —— 跟着皮肤走，618 变红、双 11 变紫 */
.spec-value.active {
  border-color: var(--color-accent);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-weight: var(--weight-medium);
}

/* 无效组合：虚线 + 删除线，一眼看得出是"这个格子没有货" */
.spec-value.disabled {
  border-style: dashed;
  border-color: var(--color-border);
  background: var(--color-bg-subtle);
  color: var(--color-text-placeholder);
  text-decoration: line-through;
  cursor: not-allowed;
}

.swatch {
  width: 18px;
  height: 18px;
  border-radius: var(--radius-xs);
  object-fit: cover;
}

.sku-line {
  min-height: 20px;
  margin: var(--space-5) 0 var(--space-3);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.sku-line.ok {
  color: var(--color-accent);
}

.buy {
  width: 200px;
}

/* --------------------------------------------------------------------------
 * 店铺
 *
 * 放在购买区下面：先让人把东西买了，再回答"这是哪家店"。
 * ------------------------------------------------------------------------*/

.shop {
  margin-top: var(--space-5);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border);
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.shop-logo {
  width: 40px;
  height: 40px;
  border-radius: var(--radius-md);
  flex: 0 0 auto;
}

img.shop-logo {
  object-fit: cover;
  border: 1px solid var(--color-border);
  background: var(--color-bg-inset);
}

/* 店铺没传 logo 时用店名首字当字母头像 —— 比一块灰方块像样得多 */
.shop-logo-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
}

.shop-name {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.shop-desc {
  margin-top: 2px;
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

@media (max-width: 768px) {
  .panel {
    gap: var(--space-5);
    padding: var(--space-4);
  }

  .gallery {
    flex: 1 1 100%;
  }

  .buy {
    width: 100%;
  }
}

/* ---------- 评价区 ---------- */

.reviews {
  grid-column: 1 / -1;
  margin-top: var(--space-6);
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.reviews-head {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
}

.reviews-title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.reviews-summary {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  margin-left: auto;
}

.score-box {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

/* 平均分用价格色（同一套"强调数字"的语义） */
.score {
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
  line-height: 1;
}

.score-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.good-box {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.good-rate {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-success);
  line-height: 1.2;
}

.good-label {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.reviews-empty-hint {
  margin-left: auto;
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.reviews-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.chips {
  display: flex;
  gap: var(--space-1);
}

.chip {
  padding: var(--space-1) var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out);
}

.chip:hover {
  color: var(--color-text);
}

.chip.on {
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-weight: var(--weight-medium);
}

.reviews-none {
  padding: var(--space-6);
  text-align: center;
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.review-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.review {
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.review:last-child {
  border-bottom: none;
}

.review-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.review-user {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.review-time {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.review-content {
  margin-top: var(--space-2);
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
  white-space: pre-wrap;
  word-break: break-word;
}

.review-thumbs {
  margin-top: var(--space-2);
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.review-thumbs img {
  width: 84px;
  height: 84px;
  object-fit: cover;
  border-radius: var(--radius-md);
  background: var(--media-bg);
}

.review-spec {
  margin-top: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

/* 追评：缩进 + 左侧竖线，视觉上明确"这是附在首评下面的" */
.follow-up {
  margin-top: var(--space-3);
  padding: var(--space-3);
  border-left: 2px solid var(--color-accent);
  border-radius: var(--radius-sm);
  background: var(--color-bg-subtle);
}

.follow-tag {
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-size: var(--text-xs);
}

.follow-time {
  margin-left: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.follow-content {
  margin-top: var(--space-2);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: var(--leading-snug);
  white-space: pre-wrap;
  word-break: break-word;
}

.review-replies {
  margin-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.review-reply {
  display: flex;
  gap: var(--space-2);
  font-size: var(--text-sm);
}

.reply-label {
  flex: 0 0 auto;
  color: var(--color-accent);
}

.reply-text {
  color: var(--color-text-secondary);
  line-height: var(--leading-snug);
}

.reviews-more {
  display: flex;
  justify-content: center;
  padding: var(--space-3);
}
</style>
