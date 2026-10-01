<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  REASONS,
  TYPE_RETURN_REFUND,
  applyRefund,
  checkRefund,
  type RefundCheck,
} from '@/api/aftersale'
import { isBizError } from '@/api/errors'
import { newIdempotencyKey } from '@/utils/idempotency'
import { formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const route = useRoute()
const router = useRouter()

const check = ref<RefundCheck | null>(null)
const loading = ref(false)
const submitting = ref(false)

/** 每个订单项退几件。0 = 不退 */
const nums = reactive<Record<string, number>>({})
const reason = ref<number>(REASONS[0]!.value)
const desc = ref('')

const orderSubNo = computed(() => String(route.params.orderSubNo ?? ''))

/** 用户实际要退的件数不超过可退上限 */
function maxOf(orderItemId: string): number {
  return check.value?.items.find((i) => i.orderItemId === orderItemId)?.maxNum ?? 0
}

const picked = computed(() =>
  (check.value?.items ?? [])
    .map((i) => ({ item: i, num: nums[i.orderItemId] ?? 0 }))
    .filter((x) => x.num > 0),
)

/** 整个子单是否全部退完 —— 决定运费退不退，所以要在界面上说清楚 */
const wholeSub = computed(() => {
  const items = check.value?.items ?? []
  if (items.length === 0) return false
  return items.every((i) => (nums[i.orderItemId] ?? 0) >= i.maxNum)
})

/**
 * 预估退款金额。
 *
 * **只是预估**：真正的金额由服务端用差额法在提交时定稿（按件的取整与
 * "最后一笔补齐"都必须以库里的 `refunded_*` 为准）。这里展示是为了让用户
 * 在点提交之前心里有数。
 */
const estimate = computed(() => {
  if (!check.value) return { items: 0, freight: 0, total: 0 }
  const items = picked.value.reduce((s, x) => s + x.item.unitPayable * x.num, 0)
  const freight = wholeSub.value ? check.value.maxFreight : 0
  return { items, freight, total: items + freight }
})

const isReturn = computed(() => check.value?.refundType === TYPE_RETURN_REFUND)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await checkRefund(orderSubNo.value)
    check.value = data
    for (const item of data.items) {
      // 默认全退（大多数售后是整单退），用户可以改
      nums[item.orderItemId] = item.maxNum
    }
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载售后信息失败')
  } finally {
    loading.value = false
  }
}

function toggleItem(orderItemId: string): void {
  nums[orderItemId] = (nums[orderItemId] ?? 0) > 0 ? 0 : maxOf(orderItemId)
}

/**
 * 提交申请。
 *
 * ★ 幂等键在**发起前生成一次**并持有：网络卡住重试要沿用同一个键，
 *   否则等于没有幂等。
 */
async function onSubmit(): Promise<void> {
  if (picked.value.length === 0) {
    ElMessage.warning('请选择要退的商品')
    return
  }
  const key = newIdempotencyKey()
  submitting.value = true
  try {
    const refund = await applyRefund(
      {
        orderSubNo: orderSubNo.value,
        items: picked.value.map((x) => ({ orderItemId: x.item.orderItemId, num: x.num })),
        reasonType: reason.value,
        reasonDesc: desc.value || undefined,
        refundType: check.value?.refundType,
      },
      key,
    )
    ElMessage.success('售后申请已提交')
    void router.replace({ name: 'refund-detail', params: { refundNo: refund.refundNo } })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '提交失败')
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="page">
    <el-empty v-if="!loading && check && !check.refundable" :description="check.reason ?? '暂时不能申请售后'">
      <el-button type="primary" @click="router.back()">返回</el-button>
    </el-empty>

    <template v-else-if="check">
      <!-- 类型与窗口 -->
      <section class="panel">
        <header class="panel-head">
          申请售后
          <span class="type-tag">{{ check.refundTypeText }}</span>
        </header>
        <div class="hint-box">
          <p v-if="isReturn" class="hint">
            已发货的订单需要先寄回商品，商家签收并质检合格后才会退款。
          </p>
          <p v-else class="hint">
            商品还没发出，商家同意后会直接退款，无需寄回。
          </p>
          <p v-if="check.deadline" class="hint subtle">
            请在 {{ check.deadline.slice(0, 10) }} 前完成申请。
          </p>
          <p v-for="n in check.notices" :key="n" class="hint warn">{{ n }}</p>
        </div>
      </section>

      <!-- 选商品与数量 -->
      <section class="panel">
        <header class="panel-head">
          选择要退的商品
          <span class="sub">最多可退 {{ formatYuan(check.maxItemAmount) }} 元商品款</span>
        </header>
        <ul class="items">
          <li v-for="it in check.items" :key="it.orderItemId" class="item">
            <button
              type="button"
              class="pick"
              :class="{ on: (nums[it.orderItemId] ?? 0) > 0 }"
              @click="toggleItem(it.orderItemId)"
            >
              {{ (nums[it.orderItemId] ?? 0) > 0 ? '✓' : '' }}
            </button>
            <div class="cover">
              <img :src="it.coverImage" :alt="it.title" @error="onImageError" />
            </div>
            <div class="info">
              <h3 class="title">{{ it.title }}</h3>
              <p class="spec">{{ it.specText || '—' }}</p>
              <p class="meta">
                买 {{ it.num }} 件<template v-if="it.refundedNum > 0">，已退 {{ it.refundedNum }} 件</template>
                · 实付 <span class="tnum">¥{{ formatYuan(it.unitPayable) }}</span>/件
              </p>
            </div>
            <div class="qty">
              <el-input-number
                v-model="nums[it.orderItemId]"
                :min="0"
                :max="it.maxNum"
                size="small"
                :disabled="it.maxNum === 0"
              />
              <span class="max">可退 {{ it.maxNum }}</span>
            </div>
          </li>
        </ul>
      </section>

      <!-- 原因 -->
      <section class="panel">
        <header class="panel-head">申请原因</header>
        <div class="reasons">
          <button
            v-for="r in REASONS"
            :key="r.value"
            type="button"
            class="reason"
            :class="{ on: reason === r.value }"
            @click="reason = r.value"
          >
            {{ r.label }}
          </button>
        </div>
        <div class="desc">
          <el-input
            v-model="desc"
            type="textarea"
            :rows="2"
            maxlength="255"
            show-word-limit
            placeholder="补充说明（选填），有助于商家更快处理"
          />
        </div>
      </section>

      <!-- 预估金额 -->
      <section class="panel">
        <header class="panel-head">退款预估</header>
        <dl class="breakdown">
          <div class="row">
            <dt>商品款</dt>
            <dd class="tnum">¥{{ formatYuan(estimate.items) }}</dd>
          </div>
          <div class="row">
            <dt>运费{{ wholeSub ? '' : '（部分退货不退）' }}</dt>
            <dd class="tnum">¥{{ formatYuan(estimate.freight) }}</dd>
          </div>
          <div class="row total">
            <dt>预计退款</dt>
            <dd class="tnum">¥{{ formatYuan(estimate.total) }}</dd>
          </div>
          <p class="notice">
            最终金额以提交时服务端计算为准（多次部分退货会按差额法补齐，累计不会多退或少退）。
          </p>
        </dl>
      </section>

      <footer class="action-bar">
        <el-button @click="router.back()">返回</el-button>
        <el-button
          type="primary"
          size="large"
          :loading="submitting"
          :disabled="picked.length === 0"
          @click="onSubmit"
        >
          提交申请
        </el-button>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding-bottom: 76px;
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.panel-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.type-tag {
  padding: 2px var(--space-3);
  border-radius: var(--radius-pill);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  font-size: var(--text-xs);
}

.sub {
  margin-left: auto;
  font-size: var(--text-xs);
  font-weight: var(--weight-normal);
  color: var(--color-text-tertiary);
}

.hint-box {
  padding: var(--space-3) var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.hint {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: var(--leading-snug);
}

.hint.subtle {
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
}

.hint.warn {
  color: var(--color-warning);
  font-size: var(--text-xs);
}

/* ---------- 商品 ---------- */

.items {
  list-style: none;
  margin: 0;
  padding: 0;
}

.item {
  display: grid;
  grid-template-columns: 24px 64px minmax(140px, 1fr) 150px;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.item:last-child {
  border-bottom: none;
}

.pick {
  width: 20px;
  height: 20px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-bg-surface);
  color: var(--color-accent-contrast);
  font-size: var(--text-xs);
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out);
}

.pick.on {
  background: var(--color-accent);
  border-color: var(--color-accent);
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

.spec,
.meta {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.qty {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-1);
}

.max {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

/* ---------- 原因 ---------- */

.reasons {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-4) var(--space-4) var(--space-2);
}

.reason {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-surface);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease-out),
    background-color var(--dur-fast) var(--ease-out);
}

.reason:hover {
  border-color: var(--color-accent);
}

.reason.on {
  border-color: var(--color-accent);
  background: var(--color-accent-soft);
  color: var(--color-text);
}

.desc {
  padding: var(--space-2) var(--space-4) var(--space-4);
}

/* ---------- 金额 ---------- */

.breakdown {
  margin: 0;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
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

.notice {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
  line-height: var(--leading-snug);
}

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
    grid-template-columns: 24px 56px 1fr;
    grid-template-areas:
      "pick cover info"
      "pick cover qty";
    gap: var(--space-2);
  }

  .pick {
    grid-area: pick;
  }

  .cover {
    grid-area: cover;
    width: 56px;
    height: 56px;
  }

  .info {
    grid-area: info;
  }

  .qty {
    grid-area: qty;
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
  }
}
</style>
