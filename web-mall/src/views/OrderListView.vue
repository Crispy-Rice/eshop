<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  ORDER_FINISHED,
  ORDER_WAIT_DELIVER,
  ORDER_WAIT_PAY,
  ORDER_WAIT_RECEIVE,
  cancelOrder,
  fetchOrders,
  orderStatusTone,
  type OrderListItem,
} from '@/api/trade'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime, formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()
const router = useRouter()

/** 分栏。与后端 status 一一对应，undefined 表示"全部" */
const TABS: { label: string; status: number | undefined }[] = [
  { label: '全部', status: undefined },
  { label: '待付款', status: ORDER_WAIT_PAY },
  { label: '待发货', status: ORDER_WAIT_DELIVER },
  { label: '待收货', status: ORDER_WAIT_RECEIVE },
  { label: '已完成', status: ORDER_FINISHED },
]

const tab = ref(0)
const items = ref<OrderListItem[]>([])
const cursor = ref<string | null>(null)
const hasMore = ref(false)
const loading = ref(false)
/** 首次加载与"加载更多"要分开，否则加载更多时整页会闪一下 */
const loadingMore = ref(false)

async function load(reset = true): Promise<void> {
  if (reset) {
    loading.value = true
    cursor.value = null
  } else {
    loadingMore.value = true
  }
  try {
    const page = await fetchOrders({
      status: TABS[tab.value]?.status,
      cursor: reset ? undefined : (cursor.value ?? undefined),
      limit: 10,
    })
    items.value = reset ? page.items : [...items.value, ...page.items]
    cursor.value = page.nextCursor
    hasMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载订单失败')
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

async function onCancel(order: OrderListItem): Promise<void> {
  try {
    // 取消是幂等的，失败重试也不会出问题
    await cancelOrder(order.orderMainNo)
    ElMessage.success('订单已取消')
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '取消失败')
  }
}

function goDetail(order: OrderListItem): void {
  void router.push({ name: 'order-detail', params: { orderMainNo: order.orderMainNo } })
}

function goPay(order: OrderListItem): void {
  void router.push({ name: 'order-detail', params: { orderMainNo: order.orderMainNo } })
}

const empty = computed(() => !loading.value && items.value.length === 0)

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: '/orders' } })
    return
  }
  await load(true)
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <h1 class="page-title">我的订单</h1>
    </header>

    <!-- 分栏。切换即重新拉第一页（游标随筛选条件失效） -->
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
      <el-empty v-if="empty" description="还没有订单">
        <el-button type="primary" @click="router.push({ name: 'home' })">去逛逛</el-button>
      </el-empty>

      <article v-for="o in items" :key="o.orderMainNo" class="card">
        <header class="card-head" @click="goDetail(o)">
          <span class="no tnum">{{ o.orderMainNo }}</span>
          <span class="time">{{ formatDateTime(o.createTime) }}</span>
          <span class="status" :class="`tone-${orderStatusTone(o.status)}`">
            {{ o.statusText }}
          </span>
        </header>

        <div class="card-body" @click="goDetail(o)">
          <div class="thumbs">
            <img
              v-for="(img, i) in o.previewImages"
              :key="i"
              :src="img"
              :alt="o.previewTitles[i] ?? '商品'"
              @error="onImageError"
            />
            <span v-if="o.itemKindCount > o.previewImages.length" class="more-thumb">
              +{{ o.itemKindCount - o.previewImages.length }}
            </span>
          </div>
          <div class="brief">
            <p class="titles">{{ o.previewTitles.join(' / ') }}</p>
            <p class="meta">
              共 <span class="tnum">{{ o.totalNum }}</span> 件 ·
              <span class="tnum">{{ o.shopCount }}</span> 个店铺
            </p>
          </div>
        </div>

        <footer class="card-foot">
          <div class="amount">
            <!-- 没付款的订单显示「应付」—— 显示「实付」会让人以为钱已经花了 -->
            {{ o.payStatus >= 1 ? '实付' : '应付' }}
            <span class="tnum payable">¥{{ formatYuan(o.payableAmount) }}</span>
          </div>
          <div class="actions">
            <!-- 待付款时把剩余时间摆在按钮旁边，比只显示"待付款"更有推动力 -->
            <span v-if="o.canPay && o.payRemainSeconds > 0" class="countdown tnum">
              剩余 {{ Math.ceil(o.payRemainSeconds / 60) }} 分钟
            </span>
            <el-button v-if="o.canCancel" size="small" @click.stop="onCancel(o)">取消订单</el-button>
            <el-button v-if="o.canPay" type="primary" size="small" @click.stop="goPay(o)">
              去支付
            </el-button>
            <el-button v-else size="small" @click.stop="goDetail(o)">查看详情</el-button>
          </div>
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

.page-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}

.page-title {
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

/* ---------- 分栏 ---------- */

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

/* ---------- 订单卡片 ---------- */

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

.card-head {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
  cursor: pointer;
}

.no {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.time {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

/* 状态靠右，用柔和的底色标签 */
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
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4);
  cursor: pointer;
}

.thumbs {
  display: flex;
  gap: var(--space-2);
  flex: 0 0 auto;
}

.thumbs img {
  width: 64px;
  height: 64px;
  object-fit: cover;
  border-radius: var(--radius-md);
  background: var(--media-bg);
}

.more-thumb {
  width: 64px;
  height: 64px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}

.brief {
  min-width: 0;
  flex: 1;
}

.titles {
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.meta {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
}

.amount {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.payable {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

.actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.countdown {
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
  .card-foot {
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-2);
  }

  .amount {
    text-align: right;
  }

  .actions {
    justify-content: flex-end;
  }

  .tabs {
    align-self: stretch;
    overflow-x: auto;
  }
}
</style>
