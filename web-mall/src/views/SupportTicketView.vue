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
  type TicketMessage,
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
/** 智能客服（后端的 `SENDER_AI`）。★ 只用来挑头像与配色 —— 名字一律用后端给的
 *  `senderTypeText`，前端不自己维护第二份发送方文案表。 */
const SENDER_AI = 5

/**
 * 头像里的字。
 *
 * ★ 买家走的是**自己的头像图**（见模板），这里是它没有头像时的兜底。
 *   **不用昵称首字** —— 昵称是用户自己填的，拿它当头像会出现
 *   "标签写着「买家」、头像却写着「商」"这种自相矛盾（演示账号的昵称就是「商家2号」）。
 * 智能客服给一个显眼的「AI」；其余（商家 / 平台客服 / 系统）取角色名首字 ——
 * 名字本来就在后端的 `senderTypeText` 里，不另写一份。
 */
function avatarText(m: TicketMessage): string {
  if (m.senderType === SENDER_USER) return '我'
  if (m.senderType === SENDER_AI) return 'AI'
  return m.senderTypeText.slice(0, 1)
}

/** 头像底色的三种角色：我 / 智能客服 / 店里的其他人（商家、平台、系统） */
function avatarClass(m: TicketMessage): string {
  if (m.senderType === SENDER_USER) return 'me'
  return m.senderType === SENDER_AI ? 'ai' : 'staff'
}

/** 我自己的头像图（没设过就空串，模板退回字母徽章） */
const myAvatar = computed(() => auth.user?.avatar ?? '')

const ticketNo = computed(() => String(route.params.ticketNo))
const ticket = ref<TicketDetail | null>(null)
const loading = ref(false)
/**
 * 发完消息后的那段等待里显示「等待客服回复 ···」。
 *
 * ★ 只在 `watchForReply` 那个窗口内置真（见 `REPLY_WATCH_MS`）。不做后端状态：
 *   "我刚发完消息"这件事买家自己的浏览器就知道（与不做 `aiReplying` 同一个理由）。
 * ★ 措辞用「等待客服回复」而不是「对方正在输入」：我们**不知道**是店小蜜还是
 *   商家本人来接，写成"正在输入"就是在替谁撒谎。
 */
const waitingReply = ref(false)
/** 等待窗口的序号：并列开出多个时，只有最新的那个有权撤掉等待条（见 `watchForReply`） */
let replyWatchId = 0
/**
 * 发完消息后密集轮询的窗口。这段时间里线程末尾一直挂着「等待客服回复 ···」，
 * **对方一开口就撤**（不是等窗口走完）。
 *
 * ★ 取 60 秒而不是 10 秒：窗口一过等待条就撤了，之后只剩页面那个 15 秒的轮询 ——
 *   模型偶尔要二三十秒才答完，窗口太短就变成"等待条先消失、答案过一会儿才蹦出来"，
 *   正是用户报的那个观感。
 */
const REPLY_WATCH_MS = 60_000
const loadingOlder = ref(false)
const sending = ref(false)
const uploading = ref(false)
const closing = ref(false)
const requesting = ref(false)
const draft = ref('')
const picked = ref<{ path: string; url: string }[]>([])
const fileInput = ref<HTMLInputElement | null>(null)

/** 组件卸载后短轮询要停 —— 否则它会在后台空转，还往已卸载的组件里写值 */
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
  void watchForReply()
}

/**
 * 线程末尾那条是不是**对方**发的（不是买家自己）。
 *
 * ★ 只看最后一条就够：买家连着发两条、机器再答，末条仍是对方的。
 *   拿不到就按"还没回"处理 —— 保守一点，宁可多等一会儿，也别把等待条提前撤掉。
 */
function hasReply(): boolean {
  const last = ticket.value?.messages.at(-1)
  return last !== undefined && last.senderType !== SENDER_USER
}

/**
 * 发完消息后密集看几眼（`REPLY_WATCH_MS` 之内，每 1.5 秒一次），**看到对方的回话就停**。
 *
 * ★ 页面本身的轮询是 15 秒一档，而智能客服通常几秒内就答完 —— 不密集看的话，
 *   用户会觉得"问完没人理"。
 * ★ 判据是"最后一条不是我自己发的"，**不是"消息条数变多"**：条数在消息发出去的
 *   那一刻就已经 +1 了，拿它当基线，等待条会在第一次轮询（1.5 秒）就自己撤掉 ——
 *   这个坑踩过，线上表现为"加载中只闪一下就没了"。
 * ★ **不复用 usePoll**：它是固定周期的，start/stop 在闭包私有、任务内部无法自停，
 *   而这里要的正是"拿到就停"（与 web-admin 的 useAnswerPoll 同一个理由）。
 */
async function watchForReply(): Promise<void> {
  // 对方已经回了（商家正好在线，或者答得极快）就不摆这条等待
  if (hasReply()) return
  const deadline = Date.now() + REPLY_WATCH_MS
  // 窗口里线程末尾挂一条「等待客服回复 ···」，让用户知道有人在处理
  waitingReply.value = true
  // 窗口里又发了一条时会开出第二个 watcher，只有**最新**那个有权把等待条撤掉
  const id = ++replyWatchId
  try {
    while (!unmounted && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1500))
      if (unmounted) return
      await load(true)
      if (hasReply()) return
    }
  } finally {
    if (id === replyWatchId) waitingReply.value = false
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
        <!-- 买家用自己的头像图（有的话）；其余一律字母徽章。
             不用昵称首字当头像 —— 那是用户自己填的，会出现"标签写「买家」、
             头像写「商」"这种自相矛盾（演示账号的昵称就叫「商家2号」） -->
        <span v-if="m.senderType === SENDER_USER && myAvatar" class="avatar me">
          <img :src="myAvatar" alt="" @error="onImageError" />
        </span>
        <span v-else class="avatar" :class="avatarClass(m)">{{ avatarText(m) }}</span>
        <div class="col">
          <span class="who">{{ m.senderTypeText }}</span>
          <div class="bubble">
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
            <div class="time">{{ formatDateTime(m.createdAt) }}</div>
          </div>
        </div>
      </div>

      <!--
        等回复：三点跳动。★ **不给它配头像** —— 这会儿还不知道是店小蜜还是
        商家本人接，配一个就等于替其中一方表态了。
      -->
      <p v-if="waitingReply" class="waiting">
        等待客服回复<span class="dots"><i /><i /><i /></span>
      </p>
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
          show-word-limit
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

/* 一条消息 = 头像 + 名字/气泡一列。买家那一侧整行翻过来（头像在右） */
.row {
  display: flex;
  gap: var(--space-2);
  align-items: flex-start;
}

.row.mine {
  flex-direction: row-reverse;
}

.avatar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-pill);
  font-size: var(--text-xs);
  line-height: 1;
}

/* 三种底色：我（灰）/ 智能客服（实心强调色，一眼认出是机器人）/ 店里的人 */
.avatar.me {
  background: var(--color-bg-hover);
  color: var(--color-text-secondary);
}

.avatar.ai {
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-weight: var(--weight-medium);
}

.avatar.staff {
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
}

.avatar img {
  width: 100%;
  height: 100%;
  border-radius: inherit;
  object-fit: cover;
}

.col {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  /* 留出头像的位置，气泡才不会顶满整行 */
  max-width: calc(76% + 32px);
}

.row.mine .col {
  align-items: flex-end;
}

.who {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.bubble {
  max-width: 100%;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.row.mine .bubble {
  background: var(--color-accent-soft);
  border-color: transparent;
}

/* 时间挪到气泡**底部**（名字已经移到气泡上方、和头像一起） */
.time {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

/*
  等回复那一条。★ 不给它配头像（还不知道谁来接），所以它是一条**状态行**而不是消息气泡。
*/
.waiting {
  display: flex;
  align-items: center;
  gap: 2px;
  /* 缩进到左边那条消息的**气泡**左缘（头像 28px + 间距 8px）——
     它不是一条消息，不该顶格读起来像谁说的话 */
  padding-left: 36px;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.dots {
  display: inline-flex;
  gap: 3px;
  margin-left: var(--space-1);
}

.dots i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--color-text-tertiary);
  animation: blink 1.2s infinite ease-in-out;
}

.dots i:nth-child(2) {
  animation-delay: 0.2s;
}

.dots i:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes blink {
  0%,
  80%,
  100% {
    opacity: 0.25;
  }
  40% {
    opacity: 1;
  }
}

.body {
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
