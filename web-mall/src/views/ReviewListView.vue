<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  REVIEW_STATUS_TEXT,
  fetchMyReviews,
  fetchPendingReviews,
  type PendingReviewItem,
  type Review,
} from '@/api/review'
import StarRating from '@/components/StarRating.vue'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()
const router = useRouter()

/** 两个 tab：待评价（要去做事）与我的评价（历史）。默认进"待评价" */
const tab = ref<'pending' | 'mine'>('pending')

const pending = ref<PendingReviewItem[]>([])
const pendingCursor = ref<string | null>(null)
const pendingMore = ref(false)

const mine = ref<Review[]>([])
const mineCursor = ref<string | null>(null)
const mineMore = ref(false)

const loading = ref(false)
const loadingMore = ref(false)

async function loadPending(reset = true): Promise<void> {
  if (reset) loading.value = true
  else loadingMore.value = true
  try {
    const page = await fetchPendingReviews({
      cursor: reset ? undefined : (pendingCursor.value ?? undefined),
      limit: 10,
    })
    pending.value = reset ? page.items : [...pending.value, ...page.items]
    pendingCursor.value = page.nextCursor
    pendingMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载待评价列表失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function loadMine(reset = true): Promise<void> {
  if (reset) loading.value = true
  else loadingMore.value = true
  try {
    const page = await fetchMyReviews({
      cursor: reset ? undefined : (mineCursor.value ?? undefined),
      limit: 10,
    })
    mine.value = reset ? page.items : [...mine.value, ...page.items]
    mineCursor.value = page.nextCursor
    mineMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载我的评价失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchTab(next: 'pending' | 'mine'): Promise<void> {
  if (tab.value === next) return
  tab.value = next
  if (next === 'pending' && pending.value.length === 0) await loadPending(true)
  if (next === 'mine' && mine.value.length === 0) await loadMine(true)
}

function goSubmit(item: PendingReviewItem): void {
  void router.push({ name: 'review-submit', params: { orderItemId: item.orderItemId } })
}

function goProduct(spuId: string): void {
  void router.push({ name: 'product-detail', params: { spuId } })
}

/** 追评入口：已评价的首评上挂"追评"按钮 */
function goFollowUp(review: Review): void {
  void router.push({
    name: 'review-submit',
    query: { followUp: review.reviewId },
  })
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: '/reviews' } })
    return
  }
  await loadPending(true)
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <h1 class="page-title">评价</h1>
    </header>

    <div class="tabs" role="tablist">
      <button
        type="button"
        class="tab"
        role="tab"
        :class="{ active: tab === 'pending' }"
        :aria-selected="tab === 'pending'"
        @click="switchTab('pending')"
      >
        待评价
      </button>
      <button
        type="button"
        class="tab"
        role="tab"
        :class="{ active: tab === 'mine' }"
        :aria-selected="tab === 'mine'"
        @click="switchTab('mine')"
      >
        我的评价
      </button>
    </div>

    <!-- ---------- 待评价 ---------- -->
    <div v-if="tab === 'pending'" v-loading="loading" class="list">
      <el-empty v-if="!loading && pending.length === 0" description="没有待评价的商品">
        <el-button type="primary" @click="router.push({ name: 'orders' })">去我的订单</el-button>
      </el-empty>

      <article v-for="item in pending" :key="item.orderItemId" class="card">
        <div class="row">
          <div class="cover" @click="goProduct(item.spuId)">
            <img :src="item.coverImage" :alt="item.title" @error="onImageError" />
          </div>
          <div class="info">
            <h3 class="title" @click="goProduct(item.spuId)">{{ item.title }}</h3>
            <p class="spec">{{ item.specText || '—' }}</p>
            <p class="meta">共 {{ item.num }} 件</p>
          </div>
          <div class="action">
            <el-button type="primary" size="small" @click="goSubmit(item)">去评价</el-button>
          </div>
        </div>
      </article>

      <div v-if="pendingMore" class="more">
        <el-button :loading="loadingMore" @click="loadPending(false)">加载更多</el-button>
      </div>
    </div>

    <!-- ---------- 我的评价 ---------- -->
    <div v-else v-loading="loading" class="list">
      <el-empty v-if="!loading && mine.length === 0" description="还没有发过评价" />

      <article v-for="r in mine" :key="r.reviewId" class="card">
        <header class="card-head">
          <StarRating :score="r.score" size="sm" />
          <span v-if="r.isFollowUp" class="tag">追评</span>
          <span class="status" :class="`st-${r.status}`">{{ REVIEW_STATUS_TEXT[r.status] }}</span>
          <span class="time">{{ formatDateTime(r.createdAt) }}</span>
        </header>

        <div class="card-body">
          <p v-if="r.content" class="content">{{ r.content }}</p>
          <p v-else class="content muted">（只打了分）</p>
          <div v-if="r.images.length" class="thumbs">
            <img
              v-for="img in r.images"
              :key="img.path"
              :src="img.thumbUrl"
              alt="评价图片"
              @error="onImageError"
            />
          </div>
          <p class="meta">{{ r.specText }} · 购买 {{ r.buyCount }} 件</p>
        </div>

        <!-- 商家回复 -->
        <div v-if="r.replies.length" class="replies">
          <div v-for="(reply, i) in r.replies" :key="i" class="reply">
            <span class="reply-label">{{ reply.replyTypeText }}</span>
            <span class="reply-text">{{ reply.content }}</span>
          </div>
        </div>

        <footer class="card-foot">
          <el-button
            v-if="!r.isFollowUp && !r.followUp"
            size="small"
            @click="goFollowUp(r)"
          >
            追评
          </el-button>
          <span v-else-if="r.followUp" class="done">已追评</span>
        </footer>
      </article>

      <div v-if="mineMore" class="more">
        <el-button :loading="loadingMore" @click="loadMine(false)">加载更多</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.page-title {
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.tabs {
  display: flex;
  gap: var(--space-1);
  padding: var(--space-1);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  align-self: flex-start;
}

.tab {
  padding: var(--space-1) var(--space-5);
  border: none;
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out);
}

.tab.active {
  background: var(--color-bg-surface);
  color: var(--color-text);
  font-weight: var(--weight-medium);
  box-shadow: var(--shadow-xs);
}

.list {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  min-height: 120px;
}

.card {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

/* ---------- 待评价 ---------- */

.row {
  display: grid;
  grid-template-columns: 64px minmax(120px, 1fr) auto;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4);
}

.cover {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--media-bg);
  cursor: pointer;
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.title {
  font-size: var(--text-base);
  color: var(--color-text);
  cursor: pointer;
  line-height: var(--leading-snug);
}

.spec,
.meta {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

/* ---------- 我的评价 ---------- */

.card-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
}

.tag {
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-size: var(--text-xs);
}

.status {
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
}

/* 状态用柔和底（docs/17 §5） */
.st-1 {
  background: var(--color-success-soft);
  color: var(--color-success);
}

.st-0 {
  background: var(--color-warning-soft);
  color: var(--color-warning);
}

.st-2,
.st-3 {
  background: var(--color-bg-subtle);
  color: var(--color-text-tertiary);
}

.time {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.card-body {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.content {
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
  white-space: pre-wrap;
  word-break: break-word;
}

.muted {
  color: var(--color-text-placeholder);
}

.thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.thumbs img {
  width: 88px;
  height: 88px;
  object-fit: cover;
  border-radius: var(--radius-md);
  background: var(--media-bg);
}

.replies {
  padding: 0 var(--space-4) var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.reply {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
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

.card-foot {
  display: flex;
  justify-content: flex-end;
  padding: 0 var(--space-4) var(--space-4);
}

.done {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.more {
  display: flex;
  justify-content: center;
  padding: var(--space-2);
}

@media (max-width: 768px) {
  .tabs {
    align-self: stretch;
  }

  .row {
    grid-template-columns: 56px 1fr auto;
    gap: var(--space-3);
  }

  .cover {
    width: 56px;
    height: 56px;
  }
}
</style>
