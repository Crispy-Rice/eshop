<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { uploadImage } from '@/api/files'
import { isBizError } from '@/api/errors'
import {
  TICKET_CLOSED,
  TICKET_OPEN,
  closeAsAdmin,
  closeAsMerchant,
  fetchAdminTicket,
  fetchAdminTickets,
  fetchMerchantTicket,
  fetchMerchantTickets,
  replyAsAdmin,
  replyAsMerchant,
  type TicketDetail,
  type TicketListItem,
} from '@/api/support'
import { usePoll } from '@/composables/usePoll'
import { useAuthStore } from '@/stores/auth'
import { useSupportStore } from '@/stores/support'
import { formatDateTime } from '@/utils/money'

const auth = useAuthStore()
const support = useSupportStore()

const MAX_IMAGES = 3
/** 买家（senderType 1）在左，客服侧（商家/平台/系统）在右 */
const SENDER_USER = 1

/**
 * 有店铺 = 商家视角（只看本店）；否则平台视角（全部店铺 + 平台级）。
 * 与 `App.vue` 的菜单判据、以及后端两组端点的守卫一致。
 */
const isShopSide = computed(() => Boolean(auth.user?.shopId))

const TABS = [
  { key: 'pending', label: '待回复' },
  { key: 'open', label: '进行中' },
  { key: 'closed', label: '已结束' },
  { key: 'all', label: '全部' },
] as const
type TabKey = (typeof TABS)[number]['key']

const tab = ref<TabKey>('pending')
const items = ref<TicketListItem[]>([])
const cursor = ref<string | null>(null)
const hasMore = ref(false)
const loading = ref(false)
const loadingMore = ref(false)

const drawerVisible = ref(false)
const currentNo = ref<string | null>(null)
const detail = ref<TicketDetail | null>(null)
const detailLoading = ref(false)
const draft = ref('')
const sending = ref(false)
const uploading = ref(false)
const picked = ref<{ path: string; url: string }[]>([])
const fileInput = ref<HTMLInputElement | null>(null)

/** 入库存的是相对路径，渲染要拼 /media/（与商城、售后页同一套约定）。 */
function mediaUrl(path: string): string {
  return `/media/${path}`
}

async function load(reset = true): Promise<void> {
  if (reset) {
    loading.value = true
    cursor.value = null
  } else {
    loadingMore.value = true
  }
  const params = {
    cursor: reset ? undefined : (cursor.value ?? undefined),
    limit: 20,
  }
  try {
    const page = isShopSide.value
      ? await fetchMerchantTickets({ ...params, pendingOnly: tab.value === 'pending' })
      : await fetchAdminTickets({
          ...params,
          pendingOnly: tab.value === 'pending',
          status:
            tab.value === 'open'
              ? TICKET_OPEN
              : tab.value === 'closed'
                ? TICKET_CLOSED
                : undefined,
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

async function switchTab(key: TabKey): Promise<void> {
  if (tab.value === key) return
  tab.value = key
  await load(true)
}

async function openDetail(row: TicketListItem): Promise<void> {
  currentNo.value = row.ticketNo
  drawerVisible.value = true
  detail.value = null
  draft.value = ''
  picked.value = []
  await loadDetail()
}

async function loadDetail(silent = false): Promise<void> {
  const no = currentNo.value
  if (!no) return
  if (!silent) detailLoading.value = true
  try {
    detail.value = isShopSide.value
      ? await fetchMerchantTicket(no)
      : await fetchAdminTicket(no)
    // 打开详情即已读（后端推进客服侧游标），顺手把角标刷到最新
    await support.refresh()
  } catch (e) {
    if (!silent) ElMessage.error(isBizError(e) ? e.message : '加载会话失败')
  } finally {
    detailLoading.value = false
  }
}

async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (picked.value.length >= MAX_IMAGES) {
    ElMessage.warning(`最多 ${MAX_IMAGES} 张图片`)
    return
  }
  uploading.value = true
  try {
    const img = await uploadImage(file, 'support')
    picked.value.push({ path: img.path, url: img.thumbUrl || img.url })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '图片上传失败')
  } finally {
    uploading.value = false
  }
}

async function onSend(): Promise<void> {
  const no = detail.value?.ticketNo
  const body = draft.value.trim()
  if (!no || (!body && picked.value.length === 0) || sending.value) return
  sending.value = true
  try {
    const images = picked.value.map((f) => f.path)
    if (isShopSide.value) {
      await replyAsMerchant(no, body || '[图片]', images)
    } else {
      await replyAsAdmin(no, body || '[图片]', images)
    }
    draft.value = ''
    picked.value = []
    await loadDetail(true)
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '发送失败')
  } finally {
    sending.value = false
  }
}

async function onClose(): Promise<void> {
  const no = detail.value?.ticketNo
  if (!no) return
  try {
    await ElMessageBox.confirm('结束后会话标记为已结束。买家再发消息会自动重新打开。', '结束会话', {
      type: 'warning',
      confirmButtonText: '结束会话',
    })
  } catch {
    return
  }
  try {
    if (isShopSide.value) {
      await closeAsMerchant(no)
    } else {
      await closeAsAdmin(no)
    }
    await loadDetail(true)
    await load(true)
    ElMessage.success('会话已结束')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  }
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  await support.refresh()
  await load(true)
})

/** 抽屉开着时轮询：客服盯着等买家补充信息是常见姿势。 */
usePoll(
  () => {
    if (drawerVisible.value) void loadDetail(true)
  },
  15_000,
  { immediate: false },
)
</script>

<template>
  <div class="page">
    <div class="head">
      <div>
        <h2 class="title">客服</h2>
        <p class="lead">
          {{
            isShopSide
              ? '买家的咨询会进到这里。回复后买家会收到站内信提醒。'
              : '全部店铺的咨询会话，可以介入任意一条。'
          }}
        </p>
      </div>
      <el-button :loading="loading" @click="load(true)">刷新</el-button>
    </div>

    <div class="tabs">
      <el-button
        v-for="t in TABS"
        :key="t.key"
        size="small"
        :type="tab === t.key ? 'primary' : 'default'"
        @click="switchTab(t.key)"
      >
        {{ t.label }}
        <span v-if="t.key === 'pending' && support.pending > 0" class="tnum">
          ({{ support.pending }})
        </span>
      </el-button>
    </div>

    <el-table v-loading="loading" :data="items" size="small" @row-click="openDetail">
      <el-table-column label="买家" min-width="170">
        <template #default="{ row }">
          <span>{{ row.buyerNickname || '—' }}</span>
          <span class="muted"> · {{ row.buyerPhone || '—' }}</span>
        </template>
      </el-table-column>

      <el-table-column v-if="!isShopSide" label="店铺" min-width="130">
        <template #default="{ row }">{{ row.shopName }}</template>
      </el-table-column>

      <el-table-column label="主题" min-width="200">
        <template #default="{ row }">
          <span>{{ row.subject }}</span>
          <el-tag size="small" effect="plain" class="tag">{{ row.sourceText }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === TICKET_OPEN ? 'warning' : 'info'">
            {{ row.status === TICKET_OPEN ? '进行中' : '已结束' }}
          </el-tag>
          <el-tag v-if="row.staffOwesReply" size="small" type="danger" class="tag">待回复</el-tag>
        </template>
      </el-table-column>

      <el-table-column label="最后消息" min-width="180">
        <template #default="{ row }">
          <span class="muted">
            {{ row.lastSenderType === 4 ? '还没有消息' : row.lastSenderText }} ·
            {{ formatDateTime(row.lastMessageAt) }}
          </span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="90" align="right">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="openDetail(row)">查看</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="hasMore" class="more">
      <el-button :loading="loadingMore" @click="load(false)">加载更多</el-button>
    </div>

    <el-drawer v-model="drawerVisible" title="客服会话" size="520px">
      <div v-loading="detailLoading" class="detail">
        <template v-if="detail">
          <header class="detail-head">
            <div>
              <p class="subject">{{ detail.subject }}</p>
              <p class="muted small">
                {{ detail.shopName }} · {{ detail.sourceText }} ·
                {{ detail.buyerNickname || '买家' }}（{{ detail.buyerPhone || '未留手机号' }}）
              </p>
            </div>
            <el-button
              v-if="detail.status === TICKET_OPEN"
              size="small"
              @click="onClose"
            >
              结束会话
            </el-button>
          </header>

          <div v-if="detail.context.orderMainNo || detail.context.refundNo" class="context">
            <span class="muted small">
              {{ detail.context.orderMainNo ? `订单 ${detail.context.orderMainNo}` : '' }}
              {{ detail.context.refundNo ? `售后 ${detail.context.refundNo}` : '' }}
              —— 到订单 / 售后页按单号查
            </span>
          </div>

          <section class="thread">
            <p v-if="detail.messages.length === 0" class="muted small">还没有消息</p>
            <div
              v-for="m in detail.messages"
              :key="m.id"
              class="row"
              :class="{ mine: m.senderType !== SENDER_USER }"
            >
              <div class="bubble">
                <div class="meta">
                  <span>{{ m.senderTypeText }}</span>
                  <span class="muted">{{ formatDateTime(m.createdAt) }}</span>
                </div>
                <p class="body">{{ m.body }}</p>
                <div v-if="m.images.length" class="shots">
                  <img v-for="img in m.images" :key="img" :src="mediaUrl(img)" alt="会话图片" />
                </div>
              </div>
            </div>
          </section>

          <div class="composer">
            <p v-if="detail.status === TICKET_CLOSED" class="muted small">
              会话已结束。回复会自动重新打开。
            </p>
            <div v-if="picked.length" class="thumbs">
              <div v-for="(f, i) in picked" :key="f.path" class="thumb">
                <img :src="f.url" alt="待发送图片" />
                <button type="button" class="remove" @click="picked.splice(i, 1)">×</button>
              </div>
            </div>
            <el-input
              v-model="draft"
              type="textarea"
              :rows="3"
              maxlength="2000"
              resize="none"
              placeholder="回复买家…"
            />
            <div class="actions">
              <input ref="fileInput" type="file" accept="image/*" hidden @change="onPick" />
              <el-button
                size="small"
                :loading="uploading"
                :disabled="uploading || picked.length >= MAX_IMAGES"
                @click="fileInput?.click()"
              >
                图片
              </el-button>
              <el-button
                type="primary"
                size="small"
                :loading="sending"
                :disabled="!draft.trim() && picked.length === 0"
                @click="onSend"
              >
                回复
              </el-button>
            </div>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.title {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.lead {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.tabs {
  display: flex;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.tag {
  margin-left: var(--space-2);
}

.muted {
  color: var(--color-text-tertiary);
}

.small {
  font-size: var(--text-xs);
}

.more {
  display: flex;
  justify-content: center;
  padding: var(--space-3);
}

.detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-height: 200px;
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.subject {
  font-size: var(--text-base);
  color: var(--color-text);
}

.context {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.thread {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 46vh;
  overflow-y: auto;
}

.row {
  display: flex;
}

.row.mine {
  justify-content: flex-end;
}

.bubble {
  max-width: 82%;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.row.mine .bubble {
  background: var(--color-accent-soft);
  border-color: transparent;
}

.meta {
  display: flex;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.body {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
}

.shots {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.shots img {
  width: 72px;
  height: 72px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--media-bg);
}

.composer {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}

.thumbs {
  display: flex;
  gap: var(--space-2);
}

.thumb {
  position: relative;
  width: 52px;
  height: 52px;
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: var(--radius-sm);
}

.remove {
  position: absolute;
  top: -6px;
  right: -6px;
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 50%;
  background: var(--color-text-secondary);
  color: var(--color-bg-surface);
  font-size: var(--text-xs);
  cursor: pointer;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
</style>
