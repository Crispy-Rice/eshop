<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import {
  createShopFaq,
  deleteShopFaq,
  fetchShopBotSetting,
  fetchShopBotStats,
  fetchShopFaqs,
  updateShopBotSetting,
  updateShopFaq,
  type ShopBotSetting,
  type ShopBotStats,
  type ShopFaq,
} from '@/api/assistant'
import { ErrorCode, isBizError } from '@/api/errors'
import { formatDateTime } from '@/utils/money'

const loading = ref(false)
/** 没开店时后端回 403「请先开通店铺」—— 正常走导航进不来，直接敲 URL 才可能撞上 */
const noShop = ref(false)

const setting = ref<ShopBotSetting>({ aiEnabled: false })
const stats = ref<ShopBotStats | null>(null)
const savingSwitch = ref(false)

const faqs = ref<ShopFaq[]>([])

const dialogVisible = ref(false)
/** null = 正在新增。PUT 收的是整条，所以编辑时也必须把问题与回答带上 */
const editingId = ref<string | null>(null)
const savingFaq = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ question: '', answer: '', enabled: true })

/**
 * ★ 两条都带 `whitespace: true`：只填空格时前端的必填校验要拦下来。
 *   后端 `min_length=1` 对 `" "` 是放行的（它只数长度），存进去会变成一条
 *   `question=""` 的问答 —— 提示词里就是一行空白。
 */
const rules: FormRules = {
  question: [
    { required: true, whitespace: true, message: '请填写问题', trigger: 'blur' },
    { max: 200, message: '问题不超过 200 字', trigger: 'blur' },
  ],
  answer: [
    { required: true, whitespace: true, message: '请填写回答', trigger: 'blur' },
    { max: 1000, message: '回答不超过 1000 字', trigger: 'blur' },
  ],
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [s, list] = await Promise.all([fetchShopBotSetting(), fetchShopFaqs()])
    setting.value = s
    faqs.value = list
    noShop.value = false
    await loadStats()
  } catch (e) {
    if (isBizError(e, ErrorCode.FORBIDDEN)) noShop.value = true
    else ElMessage.error(isBizError(e) ? e.message : '加载失败')
  } finally {
    loading.value = false
  }
}

/** 计数是锦上添花：拉失败就不显示，不弹错误打扰（开关与问答才是这一页的重点）。 */
async function loadStats(): Promise<void> {
  try {
    stats.value = await fetchShopBotStats()
  } catch {
    stats.value = null
  }
}

/**
 * 开 / 关。
 *
 * ★ 用 `before-change` 而不是 `@change`：**请求成功才让滑块动**。用 `@change`
 *   的话滑块先动、请求再飞，失败时界面显示「已开启」而库里还是关的 ——
 *   而这一页最不能出错的恰恰是"开关到底开没开"。
 *
 * ★★ **这里绝对不要自己写 `setting.aiEnabled`**。el-switch 的 `before-change`
 *   返回真之后会调 `handleChange()`，而它是在**那一刻**才用
 *   `checked ? inactiveValue : activeValue` 算目标值（见 element-plus 的
 *   switch 实现）—— 我们先把它改成新值，它就会**再翻一次**，界面与库里正好相反。
 *   （这个坑实地测出来了：库里已是"关"，滑块还显示"开"。）
 *   所以这里只做副作用：提示 + 刷新计数，**模型值交给组件翻**。
 */
async function beforeToggle(): Promise<boolean> {
  savingSwitch.value = true
  try {
    const saved = await updateShopBotSetting(!setting.value.aiEnabled)
    ElMessage.success(saved.aiEnabled ? '已开启' : '已关闭')
    await loadStats()
    return true
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
    return false
  } finally {
    savingSwitch.value = false
  }
}

function openCreate(): void {
  editingId.value = null
  form.question = ''
  form.answer = ''
  form.enabled = true
  dialogVisible.value = true
}

function openEdit(row: ShopFaq): void {
  editingId.value = row.id
  form.question = row.question
  form.answer = row.answer
  form.enabled = row.enabled
  dialogVisible.value = true
}

async function submitFaq(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  savingFaq.value = true
  const payload = {
    question: form.question.trim(),
    answer: form.answer.trim(),
    enabled: form.enabled,
  }
  try {
    if (editingId.value) {
      const saved = await updateShopFaq(editingId.value, payload)
      const i = faqs.value.findIndex((f) => f.id === saved.id)
      if (i >= 0) faqs.value[i] = saved
    } else {
      faqs.value.push(await createShopFaq(payload))
    }
    dialogVisible.value = false
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    savingFaq.value = false
  }
}

/** 行内启停。★ PUT 收的是**整条**，所以问题与回答必须一起回传，否则会被改空。
 *
 * ★ 同样**不写 `row.enabled`**（理由见 `beforeToggle`）：只把服务端回来的
 *   `updatedAt` 对齐，`enabled` 交给组件自己翻。
 */
async function beforeToggleFaq(row: ShopFaq): Promise<boolean> {
  try {
    const saved = await updateShopFaq(row.id, {
      question: row.question,
      answer: row.answer,
      enabled: !row.enabled,
    })
    row.updatedAt = saved.updatedAt
    return true
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
    return false
  }
}

async function onDelete(row: ShopFaq): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除「${row.question}」？AI 以后不会再照这条答。`, '删除问答', {
      type: 'warning',
      confirmButtonText: '删除',
    })
  } catch {
    return
  }
  try {
    await deleteShopFaq(row.id)
    faqs.value = faqs.value.filter((f) => f.id !== row.id)
    ElMessage.success('已删除')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '删除失败')
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="head">
      <div>
        <h2 class="title">智能客服</h2>
        <p class="lead">买家在商城端联系客服时，让它先替你答；答不了的仍然转到你的待回复队列里</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="panel">
      <el-empty v-if="noShop" description="你还没有店铺">
        <p class="empty-hint">先去「我的商品」页开通店铺，再回来开启智能客服</p>
      </el-empty>

      <template v-else>
        <div v-loading="loading" class="switch-row">
          <el-switch
            v-model="setting.aiEnabled"
            :loading="savingSwitch"
            :before-change="beforeToggle"
            active-text="已开启"
            inactive-text="已关闭"
          />
          <div class="switch-copy">
            <p class="switch-title">
              {{
                setting.aiEnabled
                  ? 'AI 正在以本店的名义回答买家'
                  : 'AI 暂时不会回答买家'
              }}
            </p>
            <p class="switch-hint">
              开启后，买家在商品页 / 订单页 / 客服会话里问的「这件有货吗」「我的订单到哪了」，AI
              会先查本店数据回答；它答不了、或者买家点了「转人工」的，都留在下面的待回复队列里等你。
              开始回答后随时可以关掉。
            </p>
          </div>
        </div>

        <div class="stats">
          <div class="stat">
            <span class="stat-num tnum">{{ stats?.answered ?? '—' }}</span>
            <span class="stat-label">今日已答</span>
          </div>
          <div class="stat">
            <span class="stat-num tnum">{{ stats?.notAnswered ?? '—' }}</span>
            <span class="stat-label">未回答</span>
          </div>
          <div class="stat">
            <span class="stat-num tnum">{{ stats?.pending ?? '—' }}</span>
            <span class="stat-label">处理中</span>
          </div>
          <div class="stat">
            <span class="stat-num tnum">{{ stats?.tokensToday ?? '—' }}</span>
            <span class="stat-label">今日 token</span>
          </div>
        </div>
        <p class="stats-hint">
          「未回答」= AI 这次没接话（它答不了、或者临时限流），这些会话都还在你的待回复队列里
        </p>
      </template>
    </div>

    <div v-if="!noShop" class="panel">
      <div class="panel-head">
        <div>
          <h3 class="panel-title">问答</h3>
          <p class="panel-hint">
            你写的这些 AI 会优先按着答。没写到的它会自己查本店的商品、订单与售后数据；都答不了就转给你。
          </p>
        </div>
        <el-button type="primary" size="small" @click="openCreate">新增问答</el-button>
      </div>

      <el-table v-loading="loading" :data="faqs" size="small">
        <el-table-column label="问题" min-width="180">
          <template #default="{ row }">{{ row.question }}</template>
        </el-table-column>

        <el-table-column label="回答" min-width="300">
          <template #default="{ row }">
            <span class="answer">{{ row.answer }}</span>
          </template>
        </el-table-column>

        <el-table-column label="启用" width="80" align="center">
          <template #default="{ row }">
            <el-switch v-model="row.enabled" :before-change="() => beforeToggleFaq(row)" />
          </template>
        </el-table-column>

        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">
            <span class="muted small">{{ formatDateTime(row.updatedAt) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="120" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="还没有问答" :image-size="60">
            <p class="empty-hint">
              不写也能用 —— AI 会查本店的商品与订单数据。这里写的是你希望它照着说的话，比如发什么快递、退换货怎么算
            </p>
          </el-empty>
        </template>
      </el-table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑问答' : '新增问答'"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="问题" prop="question">
          <el-input
            v-model="form.question"
            maxlength="200"
            show-word-limit
            placeholder="买家会怎么问，比如「发什么快递」"
          />
        </el-form-item>
        <el-form-item label="回答" prop="answer">
          <el-input
            v-model="form.answer"
            type="textarea"
            :rows="4"
            maxlength="1000"
            show-word-limit
            placeholder="AI 会按这个意思答，比如「默认顺丰，48 小时内发出」"
          />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" active-text="参与回答" inactive-text="暂不使用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingFaq" @click="submitFaq">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
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

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-5) var(--space-6);
}

.panel :deep(.el-empty) {
  padding: var(--space-8) 0;
}

.empty-hint {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text-placeholder);
}

.switch-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  min-height: 40px;
}

.switch-copy {
  flex: 1;
}

.switch-title {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.switch-hint {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  line-height: 1.7;
  color: var(--color-text-tertiary);
}

.stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-8);
  margin-top: var(--space-5);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border);
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.stat-num {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.stat-label {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.stats-hint {
  margin-top: var(--space-3);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-3);
}

.panel-title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.panel-hint {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

/* 回答列：压到两行，表格才不会被长文本撑成一面墙 */
.answer {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  color: var(--color-text-secondary);
  white-space: pre-wrap;
}

.muted {
  color: var(--color-text-tertiary);
}

.small {
  font-size: var(--text-xs);
}
</style>
