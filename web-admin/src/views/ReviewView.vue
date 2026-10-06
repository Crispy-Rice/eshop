<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  AUDIT_ACTIONS,
  REVIEW_BLOCKED,
  REVIEW_PENDING,
  REVIEW_PUBLISHED,
  auditReview,
  fetchAuditQueue,
  fetchMerchantReviews,
  replyReview,
  reviewTagType,
  type AuditAction,
  type AuditQueueItem,
  type Review,
} from '@/api/review'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()

/** 运营（admin）才有审核队列 —— 其他角色看不到这个 tab */
const isAdmin = computed(() => auth.user?.role === 'admin')
/** 没有店铺的运营（平台账号）只能看审核队列 */
const hasShop = computed(() => Boolean(auth.user?.shopId))

const tab = ref<'shop' | 'audit'>('shop')

// ---------- 商家：本店铺评价 ----------
const reviews = ref<Review[]>([])
const shopCursor = ref<string | null>(null)
const shopMore = ref(false)
const shopStatus = ref<number | undefined>(undefined)

// ---------- 运营：审核队列 ----------
const queue = ref<AuditQueueItem[]>([])
const auditCursor = ref<string | null>(null)
const auditMore = ref(false)
const auditStatus = ref<number>(REVIEW_PENDING)
const pendingCount = ref(0)
const secondAuditCount = ref(0)

/**
 * 「待抽检」不是状态码，是这个视图的哨兵值 —— 它查的是
 * **已发布 ∧ 待抽检**（机审放行、先发后审的那批），跟"已发布"列表不是一回事。
 */
const SECOND_AUDIT_TAB = -1

const loading = ref(false)
const loadingMore = ref(false)
const acting = ref(false)

/** 回复弹窗 */
const replyVisible = ref(false)
const replyTarget = ref<Review | null>(null)
const replyForm = reactive({ content: '' })
/** 处置弹窗（屏蔽 / 驳回要填理由） */
const auditVisible = ref(false)
const auditTarget = ref<AuditQueueItem | null>(null)
const auditForm = reactive<{ action: string; remark: string }>({ action: '', remark: '' })

const SHOP_STATUS_TABS = [
  { label: '全部', value: undefined },
  { label: '已发布', value: REVIEW_PUBLISHED },
  { label: '已屏蔽', value: REVIEW_BLOCKED },
] as const

const AUDIT_STATUS_TABS = [
  { label: '待审核', value: REVIEW_PENDING },
  { label: '待抽检', value: SECOND_AUDIT_TAB },
  { label: '已发布', value: REVIEW_PUBLISHED },
  { label: '已屏蔽', value: REVIEW_BLOCKED },
] as const

// ============================================================
// 商家评价
// ============================================================
async function loadShop(reset = true): Promise<void> {
  if (reset) loading.value = true
  else loadingMore.value = true
  try {
    const page = await fetchMerchantReviews({
      status: shopStatus.value,
      cursor: reset ? undefined : (shopCursor.value ?? undefined),
      limit: 20,
    })
    reviews.value = reset ? page.items : [...reviews.value, ...page.items]
    shopCursor.value = page.nextCursor
    shopMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载评价失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchShopStatus(value: number | undefined): Promise<void> {
  if (shopStatus.value === value) return
  shopStatus.value = value
  await loadShop(true)
}

function openReply(review: Review): void {
  replyTarget.value = review
  replyForm.content = ''
  replyVisible.value = true
}

async function submitReply(): Promise<void> {
  if (!replyTarget.value) return
  if (!replyForm.content.trim()) {
    ElMessage.warning('写点什么再回复')
    return
  }
  acting.value = true
  try {
    await replyReview(replyTarget.value.reviewId, replyForm.content.trim())
    ElMessage.success('已回复')
    replyVisible.value = false
    await loadShop(true)
  } catch (e) {
    // 超过 3 条会被服务端拒绝，如实提示
    ElMessage.error(isBizError(e) ? e.message : '回复失败')
  } finally {
    acting.value = false
  }
}

// ============================================================
// 审核队列
// ============================================================
async function loadAudit(reset = true): Promise<void> {
  if (reset) loading.value = true
  else loadingMore.value = true
  try {
    // 「待抽检」查的是已发布 ∧ 待抽检，不是某个状态码
    const secondAudit = auditStatus.value === SECOND_AUDIT_TAB
    const page = await fetchAuditQueue({
      status: secondAudit ? REVIEW_PUBLISHED : auditStatus.value,
      secondAuditOnly: secondAudit,
      cursor: reset ? undefined : (auditCursor.value ?? undefined),
      limit: 20,
    })
    queue.value = reset ? page.items : [...queue.value, ...page.items]
    auditCursor.value = page.nextCursor
    auditMore.value = page.hasMore
    pendingCount.value = page.pendingCount
    secondAuditCount.value = page.secondAuditCount
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载审核队列失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchAuditStatus(value: number): Promise<void> {
  if (auditStatus.value === value) return
  auditStatus.value = value
  await loadAudit(true)
}

/** 通过 / 解除屏蔽：直接做，不需要填理由 */
async function quickAudit(item: AuditQueueItem, action: AuditAction): Promise<void> {
  acting.value = true
  try {
    await auditReview(item.review.reviewId, action)
    ElMessage.success('已处置')
    await loadAudit(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '处置失败')
  } finally {
    acting.value = false
  }
}

/** 屏蔽 / 驳回：要填理由，走弹窗 */
function openAudit(item: AuditQueueItem, action: AuditAction): void {
  auditTarget.value = item
  auditForm.action = action
  auditForm.remark = ''
  auditVisible.value = true
}

async function submitAudit(): Promise<void> {
  if (!auditTarget.value) return
  acting.value = true
  try {
    await auditReview(
      auditTarget.value.review.reviewId,
      auditForm.action as AuditAction,
      auditForm.remark || undefined,
    )
    ElMessage.success('已处置')
    auditVisible.value = false
    await loadAudit(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '处置失败')
  } finally {
    acting.value = false
  }
}

// ---------- tab 切换 ----------
async function switchTab(next: 'shop' | 'audit'): Promise<void> {
  if (tab.value === next) return
  tab.value = next
  if (next === 'shop') await loadShop(true)
  else await loadAudit(true)
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  // 平台账号没有店铺，"本店评价"必然拉不到数据 —— 直接落在审核队列上，
  // 否则一进页面先弹一个"请先开通店铺"的报错
  if (isAdmin.value && !hasShop.value) {
    tab.value = 'audit'
    await loadAudit(true)
    return
  }
  await loadShop(true)
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">评价管理</h2>
      <span class="hint">
        处置评价会同步更新商品评分；被屏蔽的评价保留在库里，只是不再展示
      </span>
      <div class="spacer" />
      <el-button
        size="small"
        :loading="loading"
        @click="tab === 'shop' ? loadShop(true) : loadAudit(true)"
      >
        刷新
      </el-button>
    </div>

    <div v-if="isAdmin || hasShop" class="tabs">
      <el-radio-group :model-value="tab" size="small" @change="switchTab($event as 'shop' | 'audit')">
        <el-radio-button v-if="hasShop" value="shop">本店评价</el-radio-button>
        <el-radio-button v-if="isAdmin" value="audit">
          审核队列
          <span v-if="pendingCount > 0" class="badge tnum">{{ pendingCount }}</span>
        </el-radio-button>
      </el-radio-group>
    </div>

    <el-empty v-if="!isAdmin && !hasShop" description="开通店铺后可以管理本店评价" />

    <!-- ---------- 本店评价 ---------- -->
    <template v-if="tab === 'shop'">
      <el-radio-group
        :model-value="shopStatus"
        size="small"
        @change="switchShopStatus($event as number | undefined)"
      >
        <el-radio-button v-for="s in SHOP_STATUS_TABS" :key="s.label" :value="s.value">
          {{ s.label }}
        </el-radio-button>
      </el-radio-group>

      <div class="panel">
        <el-empty v-if="!loading && reviews.length === 0" description="暂无评价" />

        <el-table v-else v-loading="loading" :data="reviews">
          <el-table-column label="评价" min-width="380">
            <template #default="{ row }">
              <div class="review-cell">
                <div class="review-line">
                  <span class="stars tnum">{{ '★'.repeat(row.score) }}</span>
                  <span class="who">{{ row.nickname }}</span>
                  <span v-if="row.isFollowUp" class="flag">追评</span>
                  <span v-if="row.suspect" class="flag suspect">疑似刷评</span>
                  <span class="when">{{ formatDateTime(row.createdAt) }}</span>
                </div>
                <p v-if="row.content" class="content">{{ row.content }}</p>
                <p v-else class="content muted">（只打了分）</p>
                <div v-if="row.images.length" class="thumbs">
                  <img
                    v-for="img in row.images"
                    :key="img.path"
                    :src="img.thumbUrl"
                    alt="评价图片"
                    @error="onImageError"
                  />
                </div>
                <p class="spec">{{ row.specText }} · 购买 {{ row.buyCount }} 件</p>
                <div v-for="(reply, i) in row.replies" :key="i" class="reply">
                  <span class="reply-label">{{ reply.replyTypeText }}</span>
                  <span>{{ reply.content }}</span>
                </div>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="reviewTagType(row.status)" size="small" effect="light">
                {{ row.statusText }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="110" align="right" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="row.status === REVIEW_PUBLISHED && !row.isFollowUp"
                size="small"
                @click="openReply(row)"
              >
                回复
              </el-button>
              <span v-else class="cell-sub">—</span>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="shopMore" class="more">
          <el-button size="small" :loading="loadingMore" @click="loadShop(false)">加载更多</el-button>
        </div>
      </div>
    </template>

    <!-- ---------- 审核队列 ---------- -->
    <template v-else>
      <el-radio-group
        :model-value="auditStatus"
        size="small"
        @change="switchAuditStatus($event as number)"
      >
        <el-radio-button v-for="s in AUDIT_STATUS_TABS" :key="s.label" :value="s.value">
          {{ s.label }}
          <span v-if="s.value === SECOND_AUDIT_TAB && secondAuditCount > 0" class="badge tnum">
            {{ secondAuditCount }}
          </span>
        </el-radio-button>
      </el-radio-group>

      <div class="panel">
        <el-empty v-if="!loading && queue.length === 0" description="队列是空的" />

        <el-table v-else v-loading="loading" :data="queue">
          <el-table-column label="评价" min-width="380">
            <template #default="{ row }">
              <div class="review-cell">
                <div class="review-line">
                  <span class="stars tnum">{{ '★'.repeat(row.review.score) }}</span>
                  <span class="who">{{ row.review.nickname }}</span>
                  <span class="flag reason">{{ row.reasonText }}</span>
                </div>
                <p class="content">{{ row.review.content || '（只打了分）' }}</p>
                <div v-if="row.review.images.length" class="thumbs">
                  <img
                    v-for="img in row.review.images"
                    :key="img.path"
                    :src="img.thumbUrl"
                    alt="评价图片"
                    @error="onImageError"
                  />
                </div>
                <p class="spec tnum">
                  {{ row.orderMainNo }} · {{ formatDateTime(row.review.createdAt) }}
                </p>
                <p v-if="row.review.auditRemark" class="spec">
                  上次处置：{{ row.review.auditRemark }}
                </p>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="reviewTagType(row.review.status)" size="small" effect="light">
                {{ row.review.statusText }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="220" align="right" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="row.review.status === REVIEW_PENDING"
                type="primary"
                size="small"
                :loading="acting"
                @click="quickAudit(row, AUDIT_ACTIONS.APPROVE)"
              >
                通过
              </el-button>
              <el-button
                v-if="row.review.status === REVIEW_PENDING"
                size="small"
                @click="openAudit(row, AUDIT_ACTIONS.REJECT)"
              >
                驳回
              </el-button>
              <!-- 抽检队列里的行：状态已是「已发布」，处置就是"确认无误"（不改状态，
                   只把这条移出抽检队列）或"屏蔽" -->
              <el-button
                v-if="row.review.status === REVIEW_PUBLISHED && row.review.needSecondAudit"
                type="primary"
                size="small"
                :loading="acting"
                @click="quickAudit(row, AUDIT_ACTIONS.APPROVE)"
              >
                抽检通过
              </el-button>
              <el-button
                v-if="row.review.status === REVIEW_PUBLISHED"
                size="small"
                @click="openAudit(row, AUDIT_ACTIONS.BLOCK)"
              >
                屏蔽
              </el-button>
              <el-button
                v-if="row.review.status === REVIEW_BLOCKED"
                size="small"
                :loading="acting"
                @click="quickAudit(row, AUDIT_ACTIONS.UNBLOCK)"
              >
                解除屏蔽
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="auditMore" class="more">
          <el-button size="small" :loading="loadingMore" @click="loadAudit(false)">加载更多</el-button>
        </div>
      </div>
    </template>

    <!-- 回复 -->
    <el-dialog v-model="replyVisible" title="回复评价" width="460px">
      <p v-if="replyTarget" class="dlg-quote">
        {{ replyTarget.nickname }}：{{ replyTarget.content || '（只打了分）' }}
      </p>
      <el-input
        v-model="replyForm.content"
        type="textarea"
        :rows="3"
        maxlength="500"
        show-word-limit
        placeholder="回复会展示在评价下方，买家能看到"
      />
      <template #footer>
        <el-button @click="replyVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitReply">提交回复</el-button>
      </template>
    </el-dialog>

    <!-- 屏蔽 / 驳回 -->
    <el-dialog
      v-model="auditVisible"
      :title="auditForm.action === AUDIT_ACTIONS.BLOCK ? '屏蔽评价' : '驳回评价'"
      width="460px"
    >
      <p v-if="auditTarget" class="dlg-quote">{{ auditTarget.review.content || '（只打了分）' }}</p>
      <el-input
        v-model="auditForm.remark"
        type="textarea"
        :rows="3"
        maxlength="255"
        show-word-limit
        placeholder="处置理由（内部记录）"
      />
      <el-alert
        v-if="auditForm.action === AUDIT_ACTIONS.BLOCK"
        type="warning"
        :closable="false"
        show-icon
        title="屏蔽后评价不再展示，并会从商品评分里扣除"
      />
      <el-alert
        v-else
        type="info"
        :closable="false"
        show-icon
        title="驳回是终态，之后不能再改成通过"
      />
      <template #footer>
        <el-button @click="auditVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitAudit">确认</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 只用语义 token，见 docs/17-frontend-design-system.md */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.hint {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.spacer {
  flex: 1;
}

.tabs {
  display: flex;
}

.badge {
  margin-left: var(--space-1);
  padding: 0 5px;
  border-radius: var(--radius-pill);
  background: var(--color-price);
  color: #fff;
  font-size: var(--text-xs);
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

/* ---------- 表格里的评价单元格 ---------- */

.review-cell {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) 0;
}

.review-line {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.stars {
  color: var(--color-warning);
  font-size: var(--text-sm);
  letter-spacing: 1px;
}

.who {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.when {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

/* 状态/风险标记用柔和底 */
.flag {
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-size: var(--text-xs);
}

.flag.suspect {
  background: var(--color-warning-soft);
  color: var(--color-warning);
}

.flag.reason {
  background: var(--color-bg-subtle);
  color: var(--color-text-tertiary);
}

.content {
  font-size: var(--text-sm);
  color: var(--color-text);
  line-height: var(--leading-snug);
  white-space: pre-wrap;
  word-break: break-word;
}

.muted {
  color: var(--color-text-placeholder);
}

.thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.thumbs img {
  width: 56px;
  height: 56px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--media-bg);
}

.spec,
.cell-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.reply {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--color-bg-subtle);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.reply-label {
  flex: 0 0 auto;
  color: var(--color-accent);
}

.more {
  display: flex;
  justify-content: center;
  padding: var(--space-3);
}

.dlg-quote {
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: var(--leading-snug);
}
</style>
