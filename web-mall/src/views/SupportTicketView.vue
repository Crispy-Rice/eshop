<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { uploadImage } from '@/api/files'
import { isBizError } from '@/api/errors'
import {
  closeTicket,
  fetchTicket,
  requestHuman,
  sendTicketMessage,
  type TicketDetail,
} from '@/api/support'
import { usePoll } from '@/composables/usePoll'
import { useAuthStore } from '@/stores/auth'
import { useNotifyStore } from '@/stores/notify'
import { formatDateTime } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()
const notify = useNotifyStore()
const route = useRoute()
const router = useRouter()

const MAX_IMAGES = 3
/** 我 = 买家（senderType 1）。右侧气泡。 */
const SENDER_USER = 1

const ticketNo = computed(() => String(route.params.ticketNo))
const ticket = ref<TicketDetail | null>(null)
const loading = ref(false)
const loadingOlder = ref(false)
const sending = ref(false)
const uploading = ref(false)
const closing = ref(false)
const requesting = ref(false)
const draft = ref('')
const picked = ref<{ path: string; url: string }[]>([])
const fileInput = ref<HTMLInputElement | null>(null)

/** 组件卸载后短轮询要停 —— 否则它会在后台空转 20 秒，还往已卸载的组件里写值 */
let unmounted = false
onBeforeUnmount(() => {
  unmounted = true
})

/** 入库存的是**相对路径**，渲染要拼 /media/（与后台售后页同一套约定）。 */
function mediaUrl(path: string): string {
  return `/media/${path}`
}

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    ticket.value = await fetchTicket(ticketNo.value)
    if (!silent) await notify.refresh()
  } catch (e) {
    // 静默轮询失败不打扰用户：网络抖一下不该弹错误
    if (!silent) ElMessage.error(isBizError(e) ? e.message : '加载会话失败')
  } finally {
    loading.value = false
  }
}

async function loadOlder(): Promise<void> {
  const detail = ticket.value
  if (!detail?.nextMessageCursor) return
  loadingOlder.value = true
  try {
    const older = await fetchTicket(ticketNo.value, detail.nextMessageCursor)
    ticket.value = {
      ...detail,
      messages: [...older.messages, ...detail.messages],
      hasMoreMessages: older.hasMoreMessages,
      nextMessageCursor: older.nextMessageCursor,
    }
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载更早的消息失败')
  } finally {
    loadingOlder.value = false
  }
}

async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // 清掉 value，否则连续选同一个文件不会再触发 change
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
  const body = draft.value.trim()
  if ((!body && picked.value.length === 0) || sending.value) return
  const before = ticket.value?.messages.length ?? 0
  sending.value = true
  try {
    // 正文是必填的（后端 min_length=1），只发图时给一个占位文字
    await sendTicketMessage(
      ticketNo.value,
      body || '[图片]',
      picked.value.map((f) => f.path),
    )
    draft.value = ''
    picked.value = []
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '发送失败')
  } finally {
    sending.value = false
  }
  void watchForReply(before)
}

/**
 * 发完消息后密集看几眼（最多 20 秒），看到新消息就停。
 *
 * ★ 页面本身的轮询是 15 秒一档，而智能客服通常几秒内就答完 —— 不密集看的话，
 *   用户会觉得"问完没人理"。
 * ★ **不复用 usePoll**：它是固定周期的，start/stop 在闭包私有、任务内部无法自停，
 *   而这里要的正是"拿到就停"（与 web-admin 的 useAnswerPoll 同一个理由）。
 */
async function watchForReply(baseline: number): Promise<void> {
  const deadline = Date.now() + 20_000
  while (!unmounted && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1500))
    if (unmounted) return
    await load(true)
    if ((ticket.value?.messages.length ?? 0) > baseline) return
  }
}

async function onRequestHuman(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '转人工后会通知商家本人来回复你。智能客服答得了的问题不必转人工。',
      '转人工客服',
      { confirmButtonText: '转人工' },
    )
  } catch {
    return // 用户取消
  }
  requesting.value = true
  try {
    await requestHuman(ticketNo.value)
    await load(true)
    await notify.refresh()
    ElMessage.success('已通知商家，请稍候')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  } finally {
    requesting.value = false
  }
}

async function onClose(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '结束后本次会话标记为已结束。之后再发消息会自动重新打开同一条。',
      '结束会话',
      { type: 'warning', confirmButtonText: '结束会话' },
    )
  } catch {
    return // 用户取消
  }
  closing.value = true
  try {
    await closeTicket(ticketNo.value)
    await load(true)
    ElMessage.success('会话已结束')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  } finally {
    closing.value = false
  }
}

function goOrder(): void {
  const no = ticket.value?.context.orderMainNo
  if (no) void router.push({ name: 'order-detail', params: { orderMainNo: no } })
}

function goRefund(): void {
  const no = ticket.value?.context.refundNo
  if (no) void router.push({ name: 'refund-detail', params: { refundNo: no } })
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: route.fullPath } })
    return
  }
  await load()
})

/**
 * 15 秒轮询对方的新消息。
 *
 * ★ 轮询拿到的是**最新一页**，所以它会把"往前翻过的历史"收回去 ——
 *   P0 接受的取舍：聊天视图本来也是以底部最新为主，翻历史是临时动作。
 */
usePoll(() => load(true), 15_000, { immediate: false })
</script>

<template>
  <div v-loading="loading" class="page">
    <header class="page-head">
      <div class="head-left">
        <h1 class="page-title">{{ ticket?.subject ?? '会话' }}</h1>
        <p v-if="ticket" class="sub">
          {{ ticket.shopName }} · {{ ticket.sourceText }} ·
          <span :class="ticket.status === 10 ? 'live' : 'done'">{{ ticket.statusText }}</span>
        </p>
      </div>
      <div class="head-right">
        <el-button
          v-if="ticket && ticket.status === 10"
          size="small"
          :type="ticket.needHuman ? 'default' : 'primary'"
          :disabled="ticket.needHuman"
          :loading="requesting"
          @click="onRequestHuman"
        >
          {{ ticket.needHuman ? '已转人工' : '转人工' }}
        </el-button>
        <el-button
          v-if="ticket && ticket.status === 10"
          size="small"
          :loading="closing"
          @click="onClose"
        >
          结束会话
        </el-button>
        <el-button size="small" @click="router.push({ name: 'support' })">返回列表</el-button>
      </div>
    </header>

    <div v-if="ticket" class="context">
      <span class="no tnum">{{ ticket.ticketNo }}</span>
      <el-button v-if="ticket.context.orderMainNo" link type="primary" @click="goOrder">
        查看订单 {{ ticket.context.orderMainNo }}
      </el-button>
      <el-button v-if="ticket.context.refundNo" link type="primary" @click="goRefund">
        查看售后 {{ ticket.context.refundNo }}
      </el-button>
    </div>

    <section class="thread">
      <div v-if="ticket?.hasMoreMessages" class="older">
        <el-button size="small" :loading="loadingOlder" @click="loadOlder">加载更早的消息</el-button>
      </div>

      <p v-if="ticket && ticket.messages.length === 0" class="empty">
        还没有消息。描述一下你的问题，客服会尽快回复。
      </p>

      <div
        v-for="m in ticket?.messages ?? []"
        :key="m.id"
        class="row"
        :class="{ mine: m.senderType === SENDER_USER }"
      >
        <div class="bubble">
          <div class="meta">
            <span>{{ m.senderTypeText }}</span>
            <span class="time">{{ formatDateTime(m.createdAt) }}</span>
          </div>
          <p class="body">{{ m.body }}</p>
          <div v-if="m.images.length" class="shots">
            <img
              v-for="img in m.images"
              :key="img"
              :src="mediaUrl(img)"
              alt="会话图片"
              @error="onImageError"
            />
          </div>
        </div>
      </div>
    </section>

    <footer class="composer">
      <p v-if="ticket && ticket.status === 30" class="closed-hint">
        会话已结束。再发一条消息会自动重新打开。
      </p>

      <div v-if="picked.length" class="thumbs">
        <div v-for="(f, i) in picked" :key="f.path" class="thumb">
          <img :src="f.url" alt="待发送图片" />
          <button type="button" class="remove" title="移除" @click="picked.splice(i, 1)">×</button>
        </div>
      </div>

      <div class="input-row">
        <el-input
          v-model="draft"
          type="textarea"
          :rows="2"
          maxlength="2000"
          resize="none"
          placeholder="描述你的问题…"
          @keydown.enter.exact.prevent="onSend"
        />
        <div class="actions">
          <input
            ref="fileInput"
            type="file"
            accept="image/*"
            hidden
            @change="onPick"
          />
          <el-button
            size="small"
            :disabled="uploading || picked.length >= MAX_IMAGES"
            :loading="uploading"
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
            发送
          </el-button>
        </div>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  min-height: 320px;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}

.page-title {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.sub {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.live {
  color: var(--color-accent);
}

.done {
  color: var(--color-text-tertiary);
}

.head-right {
  display: flex;
  gap: var(--space-2);
}

.context {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.thread {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-bg-surface);
  min-height: 200px;
}

.older {
  display: flex;
  justify-content: center;
}

.empty {
  text-align: center;
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
  padding: var(--space-5) 0;
}

.row {
  display: flex;
}

.row.mine {
  justify-content: flex-end;
}

.bubble {
  max-width: 76%;
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
  color: var(--color-text-tertiary);
}

.time {
  color: var(--color-text-placeholder);
}

.body {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text);
  line-height: var(--leading-relaxed);
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
  width: 88px;
  height: 88px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--media-bg);
}

.composer {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-bg-surface);
}

.closed-hint {
  font-size: var(--text-xs);
  color: var(--color-warning);
}

.thumbs {
  display: flex;
  gap: var(--space-2);
}

.thumb {
  position: relative;
  width: 56px;
  height: 56px;
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--media-bg);
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
  line-height: 1;
  cursor: pointer;
}

.input-row {
  display: flex;
  gap: var(--space-3);
  align-items: flex-end;
}

.actions {
  display: flex;
  gap: var(--space-2);
}
</style>
