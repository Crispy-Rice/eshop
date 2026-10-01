<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  REFUND_APPLYING,
  REFUND_SUCCESS,
  REFUND_WAIT_RECEIVE,
  REFUND_WAIT_RETURN,
  fetchRefunds,
  refundStatusTone,
  type RefundListItem,
} from '@/api/aftersale'
import { isBizError } from '@/api/errors'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime, formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()
const router = useRouter()

const TABS: { label: string; status: number | undefined }[] = [
  { label: '全部', status: undefined },
  { label: '待处理', status: REFUND_APPLYING },
  { label: '待寄回', status: REFUND_WAIT_RETURN },
  { label: '待收货', status: REFUND_WAIT_RECEIVE },
  { label: '已完成', status: REFUND_SUCCESS },
]

const tab = ref(0)
const items = ref<RefundListItem[]>([])
const cursor = ref<string | null>(null)
const hasMore = ref(false)
const loading = ref(false)
const loadingMore = ref(false)

async function load(reset = true): Promise<void> {
  if (reset) {
    loading.value = true
    cursor.value = null
  } else {
    loadingMore.value = true
  }
  try {
    const page = await fetchRefunds({
      status: TABS[tab.value]?.status,
      cursor: reset ? undefined : (cursor.value ?? undefined),
      limit: 10,
    })
    items.value = reset ? page.items : [...items.value, ...page.items]
    cursor.value = page.nextCursor
    hasMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载售后列表失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchTab(index: number): Promise<void> {
  if (tab.value === index) return
  tab.value = index
  await load(true)
}

function goDetail(item: RefundListItem): void {
  void router.push({ name: 'refund-detail', params: { refundNo: item.refundNo } })
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: '/refunds' } })
    return
  }
  await load(true)
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <h1 class="page-title">退款/售后</h1>
    </header>

    <div class="tabs" role="tablist">
      <button
        v-for="(t, i) in TABS"
        :key="t.label"
        type="button"
        class="tab"
        role="tab"
        :class="{ active: i === tab }"
        :aria-selected="i === tab"
        @click="switchTab(i)"
      >
        {{ t.label }}
      </button>
    </div>

    <div v-loading="loading" class="list">
      <el-empty v-if="!loading && items.length === 0" description="还没有售后记录">
        <el-button type="primary" @click="router.push({ name: 'orders' })">去我的订单</el-button>
      </el-empty>

      <article v-for="r in items" :key="r.refundNo" class="card" @click="goDetail(r)">
        <header class="card-head">
          <span class="shop">{{ r.shopName }}</span>
          <span class="type">{{ r.refundTypeText }}</span>
          <span class="status" :class="`tone-${refundStatusTone(r.status)}`">{{ r.statusText }}</span>
        </header>

        <div class="card-body">
          <div class="cover">
            <img :src="r.previewImage" :alt="r.previewTitle" @error="onImageError" />
          </div>
          <div class="brief">
            <p class="title">{{ r.previewTitle }}</p>
            <p class="meta">共 {{ r.itemCount }} 件 · 申请于 {{ formatDateTime(r.applyTime) }}</p>
            <p class="no tnum">{{ r.refundNo }}</p>
          </div>
          <div class="amount">
            <span class="label">{{ r.status === REFUND_SUCCESS ? '已退' : '退款' }}</span>
            <span class="value tnum">¥{{ formatYuan(r.totalRefund) }}</span>
          </div>
        </div>

        <footer v-if="r.canFillReturn || r.canRevoke" class="card-foot">
          <span v-if="r.canFillReturn" class="hint">商家已同意，请寄回并填写单号</span>
          <el-button size="small" type="primary" @click.stop="goDetail(r)">去处理</el-button>
        </footer>
      </article>

      <div v-if="hasMore" class="more">
        <el-button :loading="loadingMore" @click="load(false)">加载更多</el-button>
      </div>
      <p v-else-if="items.length > 0" class="list-end">没有更多了</p>
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
  padding: var(--space-1) var(--space-4);
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

.tab:hover {
  color: var(--color-text);
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
  cursor: pointer;
}

.card-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
}

.shop {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.type {
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-surface);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.status {
  margin-left: auto;
  padding: 2px var(--space-3);
  border-radius: var(--radius-pill);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
}

.tone-warn {
  background: var(--color-warning-soft);
  color: var(--color-warning);
}

.tone-accent {
  background: var(--color-accent-soft);
  color: var(--color-accent);
}

.tone-success {
  background: var(--color-success-soft);
  color: var(--color-success);
}

.tone-muted {
  background: var(--color-bg-subtle);
  color: var(--color-text-tertiary);
}

.card-body {
  display: grid;
  grid-template-columns: 64px minmax(140px, 1fr) auto;
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
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.brief {
  min-width: 0;
}

.title {
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
}

.meta,
.no {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.amount {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}

.label {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.value {
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
}

.hint {
  margin-right: auto;
  font-size: var(--text-xs);
  color: var(--color-warning);
}

.more {
  display: flex;
  justify-content: center;
  padding: var(--space-2);
}

.list-end {
  text-align: center;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
  padding: var(--space-2);
}

@media (max-width: 768px) {
  .tabs {
    align-self: stretch;
    overflow-x: auto;
  }
}
</style>
