<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  TICKET_SOURCE,
  fetchMyTickets,
  openTicket,
  type OpenTicketPayload,
  type TicketListItem,
} from '@/api/support'
import { isBizError } from '@/api/errors'
import { useAuthStore } from '@/stores/auth'
import { useNotifyStore } from '@/stores/notify'
import { formatDateTime } from '@/utils/money'

const auth = useAuthStore()
const notify = useNotifyStore()
const route = useRoute()
const router = useRouter()

const items = ref<TicketListItem[]>([])
const cursor = ref<string | null>(null)
const hasMore = ref(false)
const loading = ref(false)
const loadingMore = ref(false)
const opening = ref(false)

async function load(reset = true): Promise<void> {
  if (reset) {
    loading.value = true
    cursor.value = null
  } else {
    loadingMore.value = true
  }
  try {
    const page = await fetchMyTickets({
      cursor: reset ? undefined : (cursor.value ?? undefined),
      limit: 10,
    })
    items.value = reset ? page.items : [...items.value, ...page.items]
    cursor.value = page.nextCursor
    hasMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载会话失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

/** 开会话并直接进到那一条。已有进行中的会话时后端返回的就是同一条。 */
async function startTicket(payload: OpenTicketPayload): Promise<void> {
  if (opening.value) return
  opening.value = true
  try {
    const ticket = await openTicket(payload)
    await router.replace({
      name: 'support-ticket',
      params: { ticketNo: ticket.ticketNo },
    })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '无法发起咨询')
  } finally {
    opening.value = false
  }
}

function contactPlatform(): void {
  void startTicket({ source: TICKET_SOURCE.APPEAL })
}

function goTicket(item: TicketListItem): void {
  void router.push({ name: 'support-ticket', params: { ticketNo: item.ticketNo } })
}

/**
 * 带上下文的入口。商品页 / 订单页 / 售后页都用**查询参数**指到这里：
 * `/support?shopId=…&orderMainNo=…`。
 *
 * ★ 上下文只是展示与跳转用；归属永远由后端按会话自己的 user_id 判 ——
 *   前端把单号放进 query 不构成任何授权。
 */
function contextFromQuery(): OpenTicketPayload | null {
  const q = route.query
  const shopId = typeof q.shopId === 'string' ? q.shopId : undefined
  const orderMainNo = typeof q.orderMainNo === 'string' ? q.orderMainNo : undefined
  const orderSubNo = typeof q.orderSubNo === 'string' ? q.orderSubNo : undefined
  const refundNo = typeof q.refundNo === 'string' ? q.refundNo : undefined
  const source = typeof q.source === 'string' ? Number(q.source) : undefined
  // 标题：商品页会带上商品名（那才知道买家在问什么）。订单/售后不带 ——
  // 后端已经能从 source + 单号生成「关于订单 M…」，再传一遍是冗余。
  const subject = typeof q.subject === 'string' && q.subject ? q.subject.slice(0, 120) : undefined
  if (!shopId && !orderMainNo && !orderSubNo && !refundNo) return null
  return { shopId, orderMainNo, orderSubNo, refundNo, source, subject }
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: route.fullPath } })
    return
  }
  const context = contextFromQuery()
  if (context) {
    await startTicket(context)
    return
  }
  await notify.refresh()
  await load(true)
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <h1 class="page-title">客服</h1>
      <el-button type="primary" size="small" :loading="opening" @click="contactPlatform">
        联系平台客服
      </el-button>
    </header>

    <div v-loading="loading" class="list">
      <el-empty v-if="!loading && items.length === 0" description="还没有咨询记录">
        <el-button type="primary" :loading="opening" @click="contactPlatform">
          有问题？联系平台客服
        </el-button>
      </el-empty>

      <article v-for="t in items" :key="t.ticketNo" class="card" @click="goTicket(t)">
        <header class="card-head">
          <span class="shop">{{ t.shopName }}</span>
          <span class="source">{{ t.sourceText }}</span>
          <span class="status" :class="t.status === 10 ? 'tone-accent' : 'tone-muted'">
            {{ t.statusText }}
          </span>
        </header>

        <div class="card-body">
          <p class="subject">{{ t.subject }}</p>
          <!-- lastSenderType 4 = 系统 = 还没有人说过话（后端新建时的初值） -->
          <p v-if="t.lastSenderType === 4" class="meta">还没有消息，说点什么吧</p>
          <p v-else class="meta">
            最后回复：{{ t.lastSenderText }} · {{ formatDateTime(t.lastMessageAt) }}
          </p>
        </div>

        <footer class="card-foot">
          <span v-if="t.unread > 0" class="unread">{{ t.unread }} 条新消息</span>
          <span v-else-if="t.staffOwesReply" class="waiting">等待客服回复</span>
          <span class="no tnum">{{ t.ticketNo }}</span>
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
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}

.page-title {
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
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

.source {
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

.tone-accent {
  background: var(--color-accent-soft);
  color: var(--color-accent);
}

.tone-muted {
  background: var(--color-bg-subtle);
  color: var(--color-text-tertiary);
}

.card-body {
  padding: var(--space-4);
}

.subject {
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
}

.meta {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.card-foot {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
}

.unread {
  padding: 2px var(--space-3);
  border-radius: var(--radius-pill);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
}

.waiting {
  font-size: var(--text-xs);
  color: var(--color-warning);
}

.no {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
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
</style>
