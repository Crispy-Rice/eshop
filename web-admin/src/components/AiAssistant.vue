<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { isBizError } from '@/api/errors'
import { useAnswerPoll } from '@/composables/useAnswerPoll'
import { useAssistantStore } from '@/stores/assistant'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/money'

/**
 * 后台 AI 助手：右下角悬浮按钮 + 对话面板。
 *
 * ★ 挂在 `App.vue` 里（路由是扁平的、没有 layout 嵌套，那是唯一能盖住所有页面的位置）。
 *   z-index 取 900 —— 高于 header(200) 才不被压住，低于 overlay(1000) 才不盖住对话框。
 *
 * ★ 回答按**纯文本**渲染（`white-space: pre-wrap`）。后端提示词里也明确要求模型
 *   别输出 markdown 语法 —— 项目里没有 markdown 渲染库，而引入它意味着把
 *   **模型输出当 HTML 渲染**（其中混着商品标题这类用户可控文本），不值。
 *
 * ★ **滚动**：只在"最后一条消息变了"时自动滚到底。往上翻页时最后那条没变，
 *   所以不会把用户正在读的位置冲掉 —— 监听 `messages.length` 就会犯这个错。
 */

const auth = useAuthStore()
const store = useAssistantStore()

const draft = ref('')
const listEl = ref<HTMLElement | null>(null)
const poll = useAnswerPoll(() => store.pollOnce())

/** 什么时候显示入口：登录了、且这个账号能用助手（运营恒可；商家要开店） */
const visible = computed(() => auth.isLoggedIn && store.available)

const busy = computed(() =>
  store.messages.some((m) => m.status === 'pending' || m.status === 'running'),
)
const canSend = computed(() => draft.value.trim().length > 0 && !busy.value)

const lastMessageId = computed(() => store.messages.at(-1)?.id ?? null)

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const el = listEl.value
  if (el) el.scrollTop = el.scrollHeight
}

// ★ 监听的是**最后一条的 id**，不是 `length`；见文件头的说明
watch(lastMessageId, () => void scrollToBottom())

async function toggle(): Promise<void> {
  store.open = !store.open
  if (!store.open) return
  try {
    await store.refresh()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '助手暂时打不开')
  }
  await scrollToBottom()
  // 顺手把历史列表备好，切过去时不用等
  void store.loadConversations().catch(() => undefined)
}

async function onSend(): Promise<void> {
  const question = draft.value.trim()
  if (!question || busy.value) return
  draft.value = ''
  try {
    await store.submit(question)
    await scrollToBottom()
    await poll.start()
    if (poll.timedOut.value) ElMessage.warning('助手这次答得有点久，可以稍后刷新看看')
    await scrollToBottom()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '提问失败')
    draft.value = question
  }
}

function onNew(): void {
  store.startNew()
}

/** 往上翻更早的消息。★ 插在视口上方的内容会把页面顶下去，所以要补回滚动位置。 */
async function onLoadEarlier(): Promise<void> {
  const el = listEl.value
  const beforeHeight = el ? el.scrollHeight : 0
  const beforeTop = el ? el.scrollTop : 0
  try {
    const added = await store.loadEarlier()
    if (!el || added === 0) return
    await nextTick()
    el.scrollTop = beforeTop + (el.scrollHeight - beforeHeight)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载更早的消息失败')
  }
}

async function openHistory(): Promise<void> {
  store.view = 'history'
  try {
    await store.loadConversations()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载历史对话失败')
  }
}

async function onSwitch(no: string): Promise<void> {
  try {
    await store.switchTo(no)
    await scrollToBottom()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '切换对话失败')
  }
}

/**
 * 转人工。先要一句「补充说明」，它同时解决两件事：
 *
 * ① 旧文案是 404「还没有可以转交的对话」—— 一个**死胡同**：用户就是想找客服，
 *    系统却回一句"没有对话可转"。而现在一句没问就点转人工是**合理诉求**。
 * ② 说明会写进工单首条，客服不必从零问起。
 *
 * 只有"没对话、也没写说明"才拦下来 —— 那时这条工单里确实一个字都没有。
 */
async function onHandoff(): Promise<void> {
  let note = ''
  try {
    const { value } = await ElMessageBox.prompt(
      '想让客服帮你解决什么？可留空 —— 留空就把最近的对话一起转过去。',
      '转人工',
      {
        confirmButtonText: '提交',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '例如：这单为什么发不出去',
        // 允许留空（空串是合法的）；只挡过长的
        inputValidator: (v: string) => v.length <= 500 || '说明最多 500 字',
      },
    )
    note = (value ?? '').trim()
  } catch {
    return // 点取消 / 关闭
  }
  if (!note && store.messages.length === 0) {
    ElMessage.warning('先问助手一句，或者写上想让客服帮你什么')
    return
  }
  try {
    const receipt = await store.toHuman(note || undefined)
    ElMessage.success(
      receipt.reused
        ? '已把问题交给客服（接着之前的会话）'
        : '已创建工单，客服会在「我提交的」里回复',
    )
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '转人工失败')
  }
}

function bubbleClass(status: string): string {
  return status === 'failed' || status === 'degraded' ? 'ai-bubble warn' : 'ai-bubble'
}
</script>

<template>
  <div v-if="visible" class="ai-assistant">
    <div v-if="store.open" class="ai-panel">
      <header class="ai-head">
        <span class="ai-title">{{ store.view === 'history' ? '历史对话' : '助手' }}</span>
        <span class="ai-spacer" />
        <template v-if="store.view === 'history'">
          <el-button link size="small" @click="store.view = 'chat'">返回</el-button>
        </template>
        <template v-else>
          <el-button link size="small" @click="openHistory">历史</el-button>
          <el-button link size="small" @click="onNew">新对话</el-button>
          <el-button link size="small" @click="onHandoff">转人工</el-button>
        </template>
        <el-button link size="small" @click="toggle">收起</el-button>
      </header>

      <!-- 历史对话列表 -->
      <div v-if="store.view === 'history'" v-loading="store.loadingList" class="ai-thread">
        <p v-if="store.conversations.length === 0" class="ai-empty">还没有历史对话。</p>
        <button
          v-for="c in store.conversations"
          :key="c.conversationNo"
          type="button"
          class="ai-conv"
          :class="{ active: c.conversationNo === store.conversationNo }"
          @click="onSwitch(c.conversationNo)"
        >
          <span class="ai-conv-title">{{ c.title || '（无标题）' }}</span>
          <span class="ai-conv-time">{{ formatDateTime(c.updatedAt) }}</span>
        </button>
        <el-button
          v-if="store.listHasMore"
          class="ai-more"
          size="small"
          @click="store.loadConversations(true)"
        >
          加载更早的对话
        </el-button>
      </div>

      <!-- 对话 -->
      <div v-else ref="listEl" class="ai-thread">
        <el-button
          v-if="store.hasMore"
          class="ai-more"
          size="small"
          :loading="store.loadingEarlier"
          @click="onLoadEarlier"
        >
          加载更早的消息
        </el-button>
        <p v-if="store.messages.length === 0" class="ai-empty">
          问点什么吧 —— 比如「今天有几笔待发货」「为什么这个商品发不出去」「运费怎么配」。
        </p>
        <div
          v-for="m in store.messages"
          :key="m.id"
          class="ai-row"
          :class="{ mine: m.role === 'user' }"
        >
          <!-- 头像 + 名字：一眼看出哪句是谁说的。AI 用实心强调色，
               与用户那个灰底区分开 —— 这正是"这是机器人"的第一层标识 -->
          <span class="ai-avatar" :class="m.role === 'user' ? 'user' : 'bot'">
            {{ m.role === 'user' ? '我' : 'AI' }}
          </span>
          <div class="ai-col">
            <span class="ai-who">{{ m.role === 'user' ? '我' : '助手' }}</span>
            <div v-if="m.role === 'user'" class="ai-bubble mine">{{ m.content }}</div>
            <!-- ★ 还在飞的那一条：三点跳动 + 一句"正在查询…"。
                 光有动画没有字，用户不知道是在查还是在卡住 -->
            <div
              v-else-if="m.status === 'pending' || m.status === 'running'"
              class="ai-bubble pending"
            >
              <span class="ai-dot" /><span class="ai-dot" /><span class="ai-dot" />
              <span class="ai-thinking">正在查询…</span>
            </div>
            <div v-else :class="bubbleClass(m.status)">{{ m.content }}</div>
          </div>
        </div>
      </div>

      <footer v-if="store.view === 'chat'" class="ai-foot">
        <el-input
          v-model="draft"
          type="textarea"
          :rows="2"
          resize="none"
          maxlength="2000"
          show-word-limit
          placeholder="回车换行；点「发送」提交"
          @keydown.enter.exact.prevent="onSend"
        />
        <div class="ai-actions">
          <span v-if="busy" class="ai-hint">正在查询…</span>
          <span v-else-if="poll.timedOut.value" class="ai-hint">上次没等到回复，可以再问一次</span>
          <el-button type="primary" size="small" :disabled="!canSend" @click="onSend">发送</el-button>
        </div>
      </footer>
    </div>

    <button
      class="ai-fab"
      type="button"
      :title="store.open ? '收起助手' : '打开助手'"
      @click="toggle"
    >
      {{ store.open ? '×' : 'AI' }}
    </button>
  </div>
</template>

<style scoped>
.ai-assistant {
  position: fixed;
  right: var(--space-5);
  bottom: var(--space-5);
  /* 高于 header(200)、低于 overlay(1000)：既不被 header 压住，也不会盖住对话框 */
  z-index: 900;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-3);
}

.ai-fab {
  width: 44px;
  height: 44px;
  border: none;
  border-radius: var(--radius-pill);
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  box-shadow: var(--shadow-lg);
}

.ai-panel {
  display: flex;
  flex-direction: column;
  width: 380px;
  max-width: calc(100vw - var(--space-5) * 2);
  height: 480px;
  max-height: calc(100vh - 120px);
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.ai-head {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
}

.ai-title {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.ai-spacer {
  flex: 1;
}

.ai-thread {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.ai-empty {
  margin: 0;
  font-size: var(--text-sm);
  line-height: var(--leading-normal);
  color: var(--color-text-tertiary);
}

.ai-more {
  align-self: center;
}

.ai-conv {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.ai-conv:hover {
  background: var(--color-bg-hover);
}

.ai-conv.active {
  border-color: var(--color-border-strong);
  background: var(--color-accent-soft);
}

.ai-conv-title {
  flex: 1;
  font-size: var(--text-sm);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ai-conv-time {
  flex: none;
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

/* 一条消息 = 头像 + 名字/气泡一列。用户那一侧整行翻过来（头像在右） */
.ai-row {
  display: flex;
  gap: var(--space-2);
  align-items: flex-start;
}

.ai-row.mine {
  flex-direction: row-reverse;
}

.ai-avatar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: var(--radius-pill);
  font-size: var(--text-xs);
  line-height: 1;
}

/* AI 用实心强调色：与用户那个灰底一眼区分开 */
.ai-avatar.bot {
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-weight: var(--weight-medium);
}

.ai-avatar.user {
  background: var(--color-bg-hover);
  color: var(--color-text-secondary);
}

.ai-col {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.ai-row.mine .ai-col {
  align-items: flex-end;
}

.ai-who {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.ai-thinking {
  margin-left: var(--space-1);
  color: var(--color-text-secondary);
  /* 不参与收缩：一缩就会把「正在查询…」从中间折成两行 */
  flex: 0 0 auto;
}

.ai-bubble {
  max-width: 86%;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text);
  font-size: var(--text-sm);
  line-height: var(--leading-normal);
  /* 纯文本渲染：换行保留、不解析 markdown（见上面的注释） */
  white-space: pre-wrap;
  word-break: break-word;
}

.ai-bubble.mine {
  background: var(--color-accent-soft);
}

.ai-bubble.warn {
  background: var(--color-bg-hover);
  color: var(--color-text-secondary);
}

/*
 * 还在飞的那一条：三点 + 「正在查询…」。
 *
 * ★ 这一条**不是正文气泡**，两处要压掉 `.ai-bubble` 上的通用样式：
 *   - `white-space: pre-wrap` + `word-break: break-word` 会把「正在查询…」断成两行；
 *   - `max-width: 86%` 会落进**循环依赖**（百分比按列宽算，而列宽又按内容算），
 *     实测列宽被压到 112px、气泡 96px —— 行里明明有 344px。
 *   取消上限 + 不折行之后，气泡按内容撑到 111.8px，右侧留白也正常。
 */
.ai-bubble.pending {
  display: flex;
  gap: 4px;
  align-items: center;
  max-width: none;
  white-space: nowrap;
}

.ai-dot {
  flex: 0 0 auto;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--color-text-tertiary);
  animation: blink 1.2s infinite ease-in-out;
}

.ai-dot:nth-child(2) {
  animation-delay: 0.2s;
}

.ai-dot:nth-child(3) {
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

.ai-foot {
  padding: var(--space-2) var(--space-3) var(--space-3);
  border-top: 1px solid var(--color-border);
}

.ai-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.ai-hint {
  flex: 1;
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}
</style>
