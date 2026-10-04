<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import {
  REFUND_CLOSED,
  REFUND_MERCHANT_REJECTED,
  REFUND_QUALITY_FAILED,
  REFUND_SUCCESS,
  REFUND_USER_REVOKED,
  REFUND_WAIT_RETURN,
  fillReturnExpress,
  fetchRefund,
  refundStatusTone,
  revokeRefund,
  type Refund,
} from '@/api/aftersale'
import { isBizError } from '@/api/errors'
import { formatDateTime, formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const route = useRoute()
const router = useRouter()

const refund = ref<Refund | null>(null)
const loading = ref(false)
const acting = ref(false)

const returnVisible = ref(false)
const returnForm = ref({ expressCompany: '', expressNo: '' })
const returnFormRef = ref<FormInstance>()
const CARRIERS = ['顺丰速运', '中通快递', '圆通速递', '韵达快递', '京东物流', '邮政EMS']

/**
 * 与后端 `RefundReturnRequest` 对齐：快递公司 2~32 位、快递单号 4~64 位。
 *
 * ★ 这两条必须在前端有。后端只回一句「参数错误」，用户填了个 3 位的单号
 *   根本不知道自己错在哪 —— 跟收货地址那边是同一个毛病。
 */
const returnRules: FormRules = {
  expressCompany: [
    { required: true, message: '请选择或填写快递公司', trigger: 'change' },
    { min: 2, max: 32, message: '快递公司名称 2~32 个字符', trigger: 'blur' },
  ],
  expressNo: [
    { required: true, message: '请填写快递单号', trigger: 'blur' },
    { min: 4, max: 64, message: '快递单号至少 4 位', trigger: 'blur' },
  ],
}

const refundNo = computed(() => String(route.params.refundNo ?? ''))

/** 走到哪一步了。四个环节的进度条让用户知道"还要等多久、等谁" */
const STEPS = computed(() => {
  const r = refund.value
  if (!r) return []
  const isReturn = r.refundType === 2
  const steps: { label: string; done: boolean; at: string | null }[] = [
    { label: '提交申请', done: true, at: r.applyTime },
    { label: '商家审核', done: r.status !== 10, at: r.merchantHandleTime },
  ]
  if (isReturn) {
    steps.push(
      { label: '买家寄回', done: [40, 50, 60, 70].includes(r.status), at: r.returnTime },
      { label: '商家签收质检', done: [60, 70].includes(r.status), at: r.qualityTime ?? r.receiveTime },
    )
  }
  steps.push({ label: '退款到账', done: r.status === REFUND_SUCCESS, at: r.refundTime })
  return steps
})

/** 终态且不是成功 —— 这些状态要显示"为什么没退成" */
const failed = computed(() =>
  [REFUND_MERCHANT_REJECTED, REFUND_QUALITY_FAILED, REFUND_CLOSED, REFUND_USER_REVOKED].includes(
    refund.value?.status ?? 0,
  ),
)

async function load(): Promise<void> {
  loading.value = true
  try {
    refund.value = await fetchRefund(refundNo.value)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载售后详情失败')
  } finally {
    loading.value = false
  }
}

function openReturn(): void {
  returnForm.value = { expressCompany: '', expressNo: '' }
  returnVisible.value = true
}

async function submitReturn(): Promise<void> {
  const valid = await returnFormRef.value?.validate().catch(() => false)
  if (!valid) return

  acting.value = true
  try {
    await fillReturnExpress(refundNo.value, {
      expressCompany: returnForm.value.expressCompany,
      expressNo: returnForm.value.expressNo.trim(),
    })
    ElMessage.success('已提交退货物流')
    returnVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '提交失败')
  } finally {
    acting.value = false
  }
}

async function onRevoke(): Promise<void> {
  try {
    await ElMessageBox.confirm('撤销后本次售后申请作废，确定撤销吗？', '撤销售后', {
      confirmButtonText: '撤销申请',
      cancelButtonText: '再想想',
      type: 'warning',
    })
  } catch {
    return
  }
  acting.value = true
  try {
    await revokeRefund(refundNo.value)
    ElMessage.success('已撤销')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '撤销失败')
  } finally {
    acting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="page">
    <el-empty v-if="!loading && refund === null" description="售后单不存在">
      <el-button type="primary" @click="router.push({ name: 'refunds' })">返回售后列表</el-button>
    </el-empty>

    <template v-else-if="refund">
      <!-- 状态 -->
      <section class="hero" :class="`tone-${refundStatusTone(refund.status)}`">
        <div class="hero-main">
          <span class="hero-status">{{ refund.statusText }}</span>
          <span class="hero-type">{{ refund.refundTypeText }}</span>
        </div>
        <p class="hero-sub">
          <template v-if="refund.status === REFUND_SUCCESS">
            退款 ¥{{ formatYuan(refund.totalRefund) }} 已原路退回
          </template>
          <template v-else-if="refund.status === REFUND_WAIT_RETURN">
            请把商品寄回并填写退货单号，商家签收质检后才会退款
          </template>
          <template v-else-if="failed">
            {{ refund.rejectReason ?? refund.qualityRemark ?? '本次售后未达成' }}
          </template>
          <template v-else>商家正在处理中，请耐心等待</template>
        </p>
      </section>

      <!-- 进度 -->
      <section class="panel">
        <header class="panel-head">处理进度</header>
        <ol class="steps">
          <li v-for="(s, i) in STEPS" :key="s.label" class="step" :class="{ done: s.done }">
            <span class="dot">{{ s.done ? '✓' : i + 1 }}</span>
            <span class="step-label">{{ s.label }}</span>
            <span v-if="s.at" class="step-time">{{ formatDateTime(s.at) }}</span>
          </li>
        </ol>
      </section>

      <!-- 退货物流 -->
      <section v-if="refund.refundType === 2" class="panel">
        <header class="panel-head">退货物流</header>
        <div v-if="refund.returnExpressNo" class="logi">
          <span class="logi-label">{{ refund.returnExpress }}</span>
          <span class="tnum">{{ refund.returnExpressNo }}</span>
          <span v-if="refund.returnTime" class="logi-time">{{ formatDateTime(refund.returnTime) }}</span>
        </div>
        <div v-else class="logi empty">
          {{ refund.canFillReturn ? '尚未填写退货单号' : '等待您寄回商品' }}
        </div>
        <div v-if="refund.canFillReturn" class="logi-action">
          <el-button type="primary" size="small" @click="openReturn">填写退货单号</el-button>
        </div>
      </section>

      <!-- 商品 -->
      <section class="panel">
        <header class="panel-head">{{ refund.shopName }}</header>
        <ul class="items">
          <li v-for="it in refund.items" :key="it.orderItemId" class="item">
            <div class="cover">
              <img :src="it.coverImage" :alt="it.title" @error="onImageError" />
            </div>
            <div class="info">
              <h3 class="title">{{ it.title }}</h3>
              <p class="spec">{{ it.specText || '—' }}</p>
              <p v-if="it.stockRestored" class="restored">库存已回补</p>
            </div>
            <div class="num">×{{ it.refundNum }}</div>
            <div class="amount tnum">¥{{ formatYuan(it.refundAmount) }}</div>
          </li>
        </ul>
      </section>

      <!-- 退款明细 -->
      <section class="panel">
        <header class="panel-head">退款明细</header>
        <dl class="breakdown">
          <div class="row">
            <dt>商品款</dt>
            <dd class="tnum">¥{{ formatYuan(refund.refundAmount) }}</dd>
          </div>
          <div class="row">
            <dt>运费</dt>
            <dd class="tnum">¥{{ formatYuan(refund.refundFreight) }}</dd>
          </div>
          <div class="row total">
            <dt>{{ refund.status === REFUND_SUCCESS ? '已退' : '预计退款' }}</dt>
            <dd class="tnum">¥{{ formatYuan(refund.totalRefund) }}</dd>
          </div>
        </dl>

        <dl class="breakdown meta">
          <div class="row">
            <dt>售后单号</dt>
            <dd class="tnum">{{ refund.refundNo }}</dd>
          </div>
          <div class="row">
            <dt>申请原因</dt>
            <dd>{{ refund.reasonTypeText }}</dd>
          </div>
          <div v-if="refund.reasonDesc" class="row">
            <dt>补充说明</dt>
            <dd>{{ refund.reasonDesc }}</dd>
          </div>
          <div class="row">
            <dt>申请时间</dt>
            <dd>{{ formatDateTime(refund.applyTime) }}</dd>
          </div>
          <div v-if="refund.refundTime" class="row">
            <dt>退款时间</dt>
            <dd>{{ formatDateTime(refund.refundTime) }}</dd>
          </div>
          <div v-if="refund.qualityRemark" class="row">
            <dt>质检结果</dt>
            <dd>{{ refund.qualityResultText }} · {{ refund.qualityRemark }}</dd>
          </div>
        </dl>
      </section>

      <footer class="action-bar">
        <el-button @click="router.push({ name: 'refunds' })">返回列表</el-button>
        <el-button
          v-if="refund.orderMainNo"
          @click="router.push({ name: 'order-detail', params: { orderMainNo: refund.orderMainNo } })"
        >
          查看订单
        </el-button>
        <el-button v-if="refund.canRevoke" :loading="acting" @click="onRevoke">撤销申请</el-button>
      </footer>
    </template>

    <el-dialog
      v-model="returnVisible"
      title="填写退货物流"
      width="420px"
      :close-on-click-modal="false"
    >
      <el-form ref="returnFormRef" :model="returnForm" :rules="returnRules" label-width="76px">
        <el-form-item label="快递公司" prop="expressCompany">
          <el-select
            v-model="returnForm.expressCompany"
            placeholder="选择或输入"
            filterable
            allow-create
          >
            <el-option v-for="c in CARRIERS" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="快递单号" prop="expressNo">
          <el-input v-model="returnForm.expressNo" placeholder="如 SF1234567890" maxlength="64" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="returnVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitReturn">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding-bottom: 76px;
}

/* ---------- 状态 ---------- */

.hero {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-5);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
}

.hero.tone-warn {
  background: var(--color-warning-soft);
  border-color: transparent;
}

.hero.tone-accent {
  background: var(--color-accent-soft);
  border-color: transparent;
}

.hero.tone-success {
  background: var(--color-success-soft);
  border-color: transparent;
}

.hero.tone-muted {
  background: var(--color-bg-subtle);
}

.hero-main {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}

.hero-status {
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.hero-type {
  padding: 2px var(--space-3);
  border-radius: var(--radius-pill);
  background: var(--color-bg-surface);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.hero-sub {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: var(--leading-snug);
}

/* ---------- 进度 ---------- */

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.panel-head {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.steps {
  list-style: none;
  margin: 0;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.step {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.step.done {
  color: var(--color-text);
}

.dot {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border-strong);
  font-size: var(--text-xs);
  flex: 0 0 auto;
}

.step.done .dot {
  background: var(--color-accent);
  border-color: var(--color-accent);
  color: var(--color-accent-contrast);
}

.step-time {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

/* ---------- 物流 ---------- */

.logi {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  padding: var(--space-4);
  font-size: var(--text-sm);
  color: var(--color-text);
}

.logi.empty {
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
}

.logi-label {
  color: var(--color-text-tertiary);
}

.logi-time {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.logi-action {
  padding: 0 var(--space-4) var(--space-4);
}

/* ---------- 商品 ---------- */

.items {
  list-style: none;
  margin: 0;
  padding: 0;
}

.item {
  display: grid;
  grid-template-columns: 64px minmax(140px, 1fr) 60px 100px;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.item:last-child {
  border-bottom: none;
}

.cover {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--media-bg);
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.info {
  min-width: 0;
}

.title {
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
}

.spec {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.restored {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-success);
}

.num {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
  text-align: right;
}

.amount {
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
  text-align: right;
}

/* ---------- 明细 ---------- */

.breakdown {
  margin: 0;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.breakdown.meta {
  border-top: 1px dashed var(--color-border);
}

.breakdown.meta .row {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: var(--text-base);
  color: var(--color-text-secondary);
}

.total {
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.total dd {
  font-size: var(--text-2xl);
  color: var(--color-price);
}

/* ---------- 操作条 ---------- */

.action-bar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: var(--z-sticky);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
  padding: var(--space-3) var(--layout-gutter);
  background: var(--color-bg-surface);
  border-top: 1px solid var(--color-border);
}

@media (max-width: 900px) {
  .item {
    grid-template-columns: 56px 1fr auto;
    grid-template-areas:
      "cover info amount"
      "cover num amount";
    gap: var(--space-2);
  }

  .cover {
    grid-area: cover;
    width: 56px;
    height: 56px;
  }

  .info {
    grid-area: info;
  }

  .num {
    grid-area: num;
    text-align: left;
  }

  .amount {
    grid-area: amount;
  }
}
</style>
