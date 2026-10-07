import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  ask,
  fetchConversation,
  fetchConversationByNo,
  fetchConversations,
  handoff,
  isInflight,
  type AssistantMessage,
  type Conversation,
  type ConversationListItem,
  type HandoffReceipt,
} from '@/api/assistant'
import { useAuthStore } from '@/stores/auth'

/**
 * AI 助手面板的状态。
 *
 * ★ 会话是**多条**的：面板随时显示"当前这一条"，可以在「历史对话」里切回以前任何一条。
 *   「新对话」= 本地把当前会话号清空 —— **不发请求**，后端在下次提问时才建新会话
 *   （这样列表里不会出现一堆空会话）。
 *
 * ★ 消息 id 是自增整数（不是雪花），所以下面比较新旧用的是 `Number(...)`。
 *   用字符串比会在位数变化时出错（"9" > "10"）。
 */
export const useAssistantStore = defineStore('assistant', () => {
  const open = ref(false)
  /** chat = 对话视图；history = 历史对话列表 */
  const view = ref<'chat' | 'history'>('chat')

  const available = ref(true)
  const conversationNo = ref<string | null>(null)
  const title = ref<string | null>(null)
  const messages = ref<AssistantMessage[]>([])
  const hasMore = ref(false)
  const nextCursor = ref<string | null>(null)

  const conversations = ref<ConversationListItem[]>([])
  const listCursor = ref<string | null>(null)
  const listHasMore = ref(false)

  const loading = ref(false)
  const loadingEarlier = ref(false)
  const loadingList = ref(false)

  /** 最后一次提交的助手消息 id —— 轮询靠它判断"我这条答完了没"。 */
  const awaitedId = ref<string | null>(null)

  function applyFull(conv: Conversation): void {
    available.value = conv.available
    conversationNo.value = conv.conversationNo
    title.value = conv.title
    messages.value = conv.messages
    hasMore.value = conv.hasMore
    nextCursor.value = conv.nextCursor
  }

  /**
   * 轮询用：**只替换尾部**，别把用户已经翻出来的更早那些冲掉。
   *
   * 接口一次只返回**最新一页**，直接 `messages = conv.messages` 的话，
   * 用户点了「加载更早」之后过 1.5 秒就被重置回一页 —— 这是最容易漏的一个 bug。
   */
  function applyTail(conv: Conversation): void {
    available.value = conv.available
    conversationNo.value = conv.conversationNo
    title.value = conv.title
    const first = conv.messages.at(0)
    const oldest = first ? Number(first.id) : null
    const kept = oldest === null ? [] : messages.value.filter((m) => Number(m.id) < oldest)
    messages.value = [...kept, ...conv.messages]
    hasMore.value = conv.hasMore
    nextCursor.value = conv.nextCursor
  }

  async function refresh(): Promise<void> {
    const auth = useAuthStore()
    if (!auth.isLoggedIn) {
      reset()
      return
    }
    loading.value = true
    try {
      // 已经有当前会话就拉那一条（切过会话之后不能再回去拉"最近一条"）
      const conv = conversationNo.value
        ? await fetchConversationByNo(conversationNo.value)
        : await fetchConversation()
      applyTail(conv)
    } finally {
      loading.value = false
    }
  }

  /** 提交一个问题，返回要等的那条助手消息 id。 */
  async function submit(question: string): Promise<string> {
    const receipt = await ask(question, conversationNo.value)
    conversationNo.value = receipt.conversationNo
    awaitedId.value = receipt.messageId
    await refresh()
    return receipt.messageId
  }

  /** 轮询回调：返回 true = 那条消息已经有着落（成功或失败），可以停了。 */
  function settled(): boolean {
    if (!awaitedId.value) return true
    const target = messages.value.find((m) => m.id === awaitedId.value)
    if (!target) return false
    return !isInflight(target)
  }

  async function pollOnce(): Promise<boolean> {
    await refresh()
    return settled()
  }

  /** 往上翻更早的消息。返回这一次补了几条（组件据此保持滚动位置）。 */
  async function loadEarlier(): Promise<number> {
    if (!conversationNo.value || !nextCursor.value || loadingEarlier.value) return 0
    loadingEarlier.value = true
    try {
      const conv = await fetchConversationByNo(conversationNo.value, nextCursor.value)
      messages.value = [...conv.messages, ...messages.value]
      hasMore.value = conv.hasMore
      nextCursor.value = conv.nextCursor
      return conv.messages.length
    } finally {
      loadingEarlier.value = false
    }
  }

  /** 拉会话列表（「历史对话」侧栏）。`more` 为真时接着上一页。 */
  async function loadConversations(more = false): Promise<void> {
    loadingList.value = true
    try {
      const page = await fetchConversations(more ? listCursor.value : null)
      conversations.value = more ? [...conversations.value, ...page.items] : page.items
      listCursor.value = page.nextCursor
      listHasMore.value = page.hasMore
    } finally {
      loadingList.value = false
    }
  }

  /** 切到某一条会话（并回到对话视图）。 */
  async function switchTo(no: string): Promise<void> {
    loading.value = true
    try {
      applyFull(await fetchConversationByNo(no))
      awaitedId.value = null
      view.value = 'chat'
    } finally {
      loading.value = false
    }
  }

  /**
   * 开始新对话。
   *
   * ★ **只清本地、不发请求** —— 后端在"不传会话号"时会建新会话。
   *   这样列表里不会出现一条空会话（用户点了新对话又走了）。
   */
  function startNew(): void {
    conversationNo.value = null
    title.value = null
    messages.value = []
    hasMore.value = false
    nextCursor.value = null
    awaitedId.value = null
    view.value = 'chat'
  }

  /**
   * 转人工：把**当前正看着的这条会话**交给人工。
   *
   * ★ `conversationNo` 必须带上：用户可能刚从「历史对话」翻回一条旧的，
   *   不带就变成"转最近动过的那条"，摘要和用户看到的对不上。
   *   还没问过话时也能转 —— 那一句 `note` 就是工单首条（后端允许）。
   */
  async function toHuman(note?: string): Promise<HandoffReceipt> {
    const receipt = await handoff(note, conversationNo.value)
    // 转人工之后这条会话就交给客服了，面板里清空即可（工单在客服页看）
    awaitedId.value = null
    await refresh()
    return receipt
  }

  function reset(): void {
    open.value = false
    view.value = 'chat'
    available.value = true
    conversationNo.value = null
    title.value = null
    messages.value = []
    hasMore.value = false
    nextCursor.value = null
    conversations.value = []
    listCursor.value = null
    listHasMore.value = false
    awaitedId.value = null
  }

  return {
    open,
    view,
    available,
    conversationNo,
    title,
    messages,
    hasMore,
    nextCursor,
    conversations,
    listHasMore,
    loading,
    loadingEarlier,
    loadingList,
    refresh,
    submit,
    pollOnce,
    loadEarlier,
    loadConversations,
    switchTo,
    startNew,
    toHuman,
    reset,
  }
})
