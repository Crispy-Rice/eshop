<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  fetchNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  type SiteMessage,
} from '@/api/notify'
import { isBizError } from '@/api/errors'
import { useAuthStore } from '@/stores/auth'
import { useNotifyStore } from '@/stores/notify'
import { formatDateTime } from '@/utils/money'

const auth = useAuthStore()
const notify = useNotifyStore()
const router = useRouter()

const items = ref<SiteMessage[]>([])
const cursor = ref<string | null>(null)
const hasMore = ref(false)
const unreadOnly = ref(false)
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
    const page = await fetchNotifications({
      unreadOnly: unreadOnly.value,
      cursor: reset ? undefined : (cursor.value ?? undefined),
      limit: 15,
    })
    items.value = reset ? page.items : [...items.value, ...page.items]
    cursor.value = page.nextCursor
    hasMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载消息失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchFilter(only: boolean): Promise<void> {
  if (unreadOnly.value === only) return
  unreadOnly.value = only
  await load(true)
}

/** 点一条：先标已读，再按 linkType 跳到对应的页面。 */
async function open(item: SiteMessage): Promise<void> {
  if (!item.isRead) {
    try {
      await markNotificationRead(item.id)
      item.isRead = true
      await notify.refresh()
    } catch {
      // 标记已读失败不该挡住"看详情"，静默即可
    }
  }
  if (!item.linkValue) return
  if (item.linkType === 'TICKET') {
    void router.push({ name: 'support-ticket', params: { ticketNo: item.linkValue } })
  } else if (item.linkType === 'ORDER') {
    void router.push({ name: 'order-detail', params: { orderMainNo: item.linkValue } })
  } else if (item.linkType === 'REFUND') {
    void router.push({ name: 'refund-detail', params: { refundNo: item.linkValue } })
  }
}

async function onMarkAll(): Promise<void> {
  try {
    await markAllNotificationsRead()
    await notify.refresh()
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  }
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: '/notifications' } })
    return
  }
  await load(true)
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <h1 class="page-title">消息</h1>
      <el-button v-if="notify.unread > 0" size="small" @click="onMarkAll">全部标为已读</el-button>
    </header>

    <div class="tabs" role="tablist">
      <button
        type="button"
        class="tab"
        role="tab"
        :class="{ active: !unreadOnly }"
        :aria-selected="!unreadOnly"
        @click="switchFilter(false)"
      >
        全部
      </button>
      <button
        type="button"
        class="tab"
        role="tab"
        :class="{ active: unreadOnly }"
        :aria-selected="unreadOnly"
        @click="switchFilter(true)"
      >
        未读
      </button>
    </div>

    <div v-loading="loading" class="list">
      <el-empty v-if="!loading && items.length === 0" description="还没有消息" />

      <article
        v-for="m in items"
        :key="m.id"
        class="card"
        :class="{ unread: !m.isRead }"
        @click="open(m)"
      >
        <header class="card-head">
          <span class="type">{{ m.msgTypeText }}</span>
          <span class="title">{{ m.title }}</span>
          <span v-if="!m.isRead" class="dot" aria-label="未读" />
        </header>
        <p v-if="m.body" class="body">{{ m.body }}</p>
        <footer class="card-foot">
          <span class="time">{{ formatDateTime(m.createdAt) }}</span>
          <span v-if="m.linkValue" class="go">查看 ›</span>
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
  gap: var(--space-3);
  min-height: 120px;
}

.card {
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-bg-surface);
  cursor: pointer;
}

.card.unread {
  border-color: var(--color-accent);
}

.card-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.type {
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-subtle);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.title {
  font-size: var(--text-base);
  color: var(--color-text);
}

.dot {
  margin-left: auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-accent);
}

.body {
  margin-top: var(--space-2);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: var(--leading-snug);
  word-break: break-word;
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: var(--space-2);
}

.time {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.go {
  font-size: var(--text-xs);
  color: var(--color-accent);
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
