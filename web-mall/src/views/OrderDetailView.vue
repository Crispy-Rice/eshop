<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  ORDER_CLOSED,
  ORDER_FINISHED,
  ORDER_REFUNDED,
  ORDER_WAIT_DELIVER,
  ORDER_WAIT_PAY,
  ORDER_WAIT_RECEIVE,
  cancelOrder,
  createPayment,
  fetchOrder,
  orderStatusTone,
  receiveOrder,
  type OrderMain,
} from '@/api/trade'
import { formatDateTime, formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const route = useRoute()
const router = useRouter()

const order = ref<OrderMain | null>(null)
const loading = ref(false)
const acting = ref(false)

/** 本地倒计时。基准是服务端给的 payRemainSeconds，不依赖浏览器时钟 */
const remain = ref(0)
let timer: number | undefined

const orderMainNo = computed(() => String(route.params.orderMainNo ?? ''))
const expired = computed(() => remain.value <= 0)

const remainText = computed(() => {
  const total = Math.max(0, remain.value)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
})

/** 整单退款状态单独展示 —— 它和履约状态是正交的两件事（docs/07 §5） */
const showPayStatus = computed(
  () => order.value !== null && order.value.payStatus >= 2,
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await fetchOrder(orderMainNo.value)
    order.value = data
    remain.value = data.payRemainSeconds
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载订单失败')
  } finally {
    loading.value = false
  }
}

function startTimer(): void {
  timer = window.setInterval(() => {
    if (remain.value > 0) {
      remain.value -= 1
      // 走到 0 时刷新一次：让后端把"已超时"的状态如实反映出来
      if (remain.value === 0) void load()
    }
  }, 1000)
}

async function onCancel(): Promise<void> {
  try {
    await ElMessageBox.confirm('取消后库存与优惠券会立即释放，确定取消吗？', '取消订单', {
      confirmButtonText: '取消订单',
      cancelButtonText: '再想想',
      type: 'warning',
    })
  } catch {
    return // 用户点了"再想想"
  }
  acting.value = true
  try {
    await cancelOrder(orderMainNo.value)
    ElMessage.success('订单已取消')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '取消失败')
  } finally {
    acting.value = false
  }
}

async function onReceive(orderSubNo: string): Promise<void> {
  try {
    await ElMessageBox.confirm('确认已收到商品？确认后不可撤销。', '确认收货', {
      confirmButtonText: '确认收货',
      cancelButtonText: '还没收到',
    })
  } catch {
    return
  }
  acting.value = true
  try {
    await receiveOrder(orderSubNo)
    ElMessage.success('已确认收货')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '确认收货失败')
  } finally {
    acting.value = false
  }
}

/** 去支付：先建支付单（幂等），再进支付页 */
async function onPay(): Promise<void> {
  acting.value = true
  try {
    const payment = await createPayment(orderMainNo.value)
    void router.push({ name: 'payment', params: { payNo: payment.payNo } })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '发起支付失败')
  } finally {
    acting.value = false
  }
}

function goProduct(skuId: string): void {
  void router.push({ name: 'home', query: { skuId } })
}

/** 申请售后。**按子单申请** —— 跨店订单要分别对各店发起 */
function goRefund(orderSubNo: string): void {
  void router.push({ name: 'refund-apply', params: { orderSubNo } })
}

/** 这三个状态的子单可以申请售后（待付款走取消订单，已退款/已关闭没得退） */
function canApplyRefund(sub: { status: number; canAftersale: boolean }): boolean {
  return sub.canAftersale && [ORDER_WAIT_DELIVER, ORDER_WAIT_RECEIVE, ORDER_FINISHED].includes(sub.status)
}

/**
 * 能不能评价这个子单。
 *
 * ★ 已退款（70）也要给入口 —— **部分退款后剩余商品仍可评价**，
 * 逐件能不能评由统一入口去判（资格判定要看订单项级的退款数量）。
 */
function canReview(sub: { status: number }): boolean {
  return [ORDER_FINISHED, ORDER_REFUNDED].includes(sub.status)
}

function goReviews(): void {
  void router.push({ name: 'reviews' })
}

onMounted(async () => {
  await load()
  startTimer()
})

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<template>
  <div v-loading="loading" class="page">
    <el-empty v-if="!loading && order === null" description="订单不存在">
      <el-button type="primary" @click="router.push({ name: 'orders' })">返回订单列表</el-button>
    </el-empty>

    <template v-else-if="order">
      <!-- 状态条。倒计时是这里唯一"活"的东西，其余都是快照 -->
      <section class="hero" :class="`tone-${orderStatusTone(order.status)}`">
        <div class="hero-main">
          <span class="hero-status">{{ order.statusText }}</span>
          <span v-if="showPayStatus" class="hero-pay">{{ order.payStatusText }}</span>
        </div>
        <div v-if="order.status === ORDER_WAIT_PAY && !expired" class="hero-sub">
          请在 <span class="tnum countdown">{{ remainText }}</span> 内完成支付，超时订单将自动关闭
        </div>
        <div v-else-if="order.status === ORDER_CLOSED" class="hero-sub">
          订单已关闭，库存与优惠券已释放
        </div>
        <div v-else-if="order.status === ORDER_WAIT_RECEIVE" class="hero-sub">
          商家已发货，收到商品后请确认收货（超期将自动确认）
        </div>
      </section>

      <!-- 收货信息 -->
      <section class="panel">
        <header class="panel-head">收货信息</header>
        <div class="receiver">
          <div class="recv-line">
            <span class="recv-name">{{ order.receiverName }}</span>
            <span class="recv-phone tnum">{{ order.receiverPhone }}</span>
          </div>
          <p class="recv-addr">
            {{ order.receiverProvince }}{{ order.receiverCity }}{{ order.receiverDistrict
            }}{{ order.receiverDetail }}
          </p>
          <p v-if="order.buyerRemark" class="recv-remark">买家留言：{{ order.buyerRemark }}</p>
        </div>
      </section>

      <!-- 子单：一个店铺一张卡，各自有独立状态与物流 -->
      <section v-for="sub in order.subs" :key="sub.orderSubNo" class="panel">
        <header class="panel-head sub-head">
          <span class="shop-name">{{ sub.shopName }}</span>
          <span class="sub-status" :class="`tone-${orderStatusTone(sub.status)}`">
            {{ sub.statusText }}
          </span>
          <span class="sub-no tnum">{{ sub.orderSubNo }}</span>
        </header>

        <ul class="items">
          <li v-for="it in sub.items" :key="it.skuId" class="item" @click="goProduct(it.skuId)">
            <div class="cover">
              <img :src="it.coverImage" :alt="it.title" @error="onImageError" />
            </div>
            <div class="info">
              <h3 class="title">{{ it.title }}</h3>
              <p class="spec">{{ it.specText || '—' }}</p>
            </div>
            <div class="price-cell">
              <span class="unit tnum">¥{{ formatYuan(it.unitPrice) }}</span>
              <span class="qty">×{{ it.num }}</span>
            </div>
            <div class="amount-cell">
              <span v-if="it.discountAmount > 0" class="was tnum">
                ¥{{ formatYuan(it.itemAmount) }}
              </span>
              <span class="final tnum">¥{{ formatYuan(it.payableAmount) }}</span>
            </div>
          </li>
        </ul>

        <!-- 物流：只有已发货才有 -->
        <div v-if="sub.deliveries.length" class="logistics">
          <div v-for="d in sub.deliveries" :key="d.deliveryNo" class="logi-row">
            <span class="logi-label">物流</span>
            <span class="logi-value">
              {{ d.expressCompany }}
              <span class="tnum">{{ d.expressNo }}</span>
            </span>
            <span v-if="d.deliverTime" class="logi-time">{{ formatDateTime(d.deliverTime) }}</span>
          </div>
        </div>

        <footer class="sub-foot">
          <div class="sub-amount">
            <span class="sub-amount-item">
              商品 <span class="tnum">¥{{ formatYuan(sub.totalAmount) }}</span>
            </span>
            <span v-if="sub.discountAmount > 0" class="sub-amount-item minus">
              优惠 <span class="tnum">-¥{{ formatYuan(sub.discountAmount) }}</span>
            </span>
            <span class="sub-amount-item">
              运费 <span class="tnum">¥{{ formatYuan(sub.freightAmount) }}</span>
            </span>
            <span class="sub-amount-item strong">
              实付 <span class="tnum">¥{{ formatYuan(sub.payableAmount) }}</span>
            </span>
          </div>
          <el-button
            v-if="sub.status === ORDER_WAIT_RECEIVE"
            type="primary"
            size="small"
            :loading="acting"
            @click="onReceive(sub.orderSubNo)"
          >
            确认收货
          </el-button>
          <el-button v-if="canApplyRefund(sub)" size="small" @click="goRefund(sub.orderSubNo)">
            申请售后
          </el-button>
          <el-button v-if="canReview(sub)" size="small" @click="goReviews">评价</el-button>
        </footer>
      </section>

      <!-- 金额明细（整单） -->
      <section class="panel">
        <header class="panel-head">金额明细</header>
        <dl class="breakdown">
          <div class="row">
            <dt>商品总额</dt>
            <dd class="tnum">¥{{ formatYuan(order.totalAmount) }}</dd>
          </div>
          <div v-if="order.couponAmount > 0" class="row">
            <dt>优惠券</dt>
            <dd class="tnum minus">-¥{{ formatYuan(order.couponAmount) }}</dd>
          </div>
          <div v-if="order.discountAmount - order.couponAmount > 0" class="row">
            <dt>活动优惠</dt>
            <dd class="tnum minus">-¥{{ formatYuan(order.discountAmount - order.couponAmount) }}</dd>
          </div>
          <div class="row">
            <dt>运费</dt>
            <dd class="tnum">¥{{ formatYuan(order.freightAmount) }}</dd>
          </div>
          <!-- 运费的计费依据要在下单后仍可追溯（docs/06 §12） -->
          <div v-for="n in order.freightDetail.notices ?? []" :key="n" class="notice">{{ n }}</div>
          <div class="row total">
            <dt>{{ order.paidAmount > 0 ? '实付' : '应付' }}</dt>
            <dd class="tnum">
              ¥{{ formatYuan(order.paidAmount > 0 ? order.paidAmount : order.payableAmount) }}
            </dd>
          </div>
        </dl>

        <dl class="breakdown meta">
          <div class="row">
            <dt>订单编号</dt>
            <dd class="tnum">{{ order.orderMainNo }}</dd>
          </div>
          <div class="row">
            <dt>下单时间</dt>
            <dd>{{ formatDateTime(order.createTime) }}</dd>
          </div>
          <div v-if="order.payTime" class="row">
            <dt>付款时间</dt>
            <dd>{{ formatDateTime(order.payTime) }}</dd>
          </div>
          <div v-if="order.finishTime" class="row">
            <dt>完成时间</dt>
            <dd>{{ formatDateTime(order.finishTime) }}</dd>
          </div>
        </dl>
      </section>

      <footer class="action-bar">
        <el-button @click="router.push({ name: 'orders' })">返回列表</el-button>
        <el-button v-if="order.canCancel" :loading="acting" @click="onCancel">取消订单</el-button>
        <el-button
          v-if="order.canPay && !expired"
          type="primary"
          :loading="acting"
          @click="onPay"
        >
          去支付 ¥{{ formatYuan(order.payableAmount) }}
        </el-button>
        <el-button v-else-if="expired && order.status === ORDER_WAIT_PAY" type="info" disabled>
          支付已超时
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

/* ---------- 状态条 ---------- */

.hero {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-5) var(--space-5);
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

/* 支付状态与履约状态正交，用一个小标签并排显示 */
.hero-pay {
  padding: 2px var(--space-3);
  border-radius: var(--radius-pill);
  background: var(--color-bg-surface);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.hero-sub {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.countdown {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-warning);
}

/* ---------- 面板 ---------- */

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

.sub-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.shop-name {
  font-weight: var(--weight-semibold);
}

.sub-status {
  padding: 2px var(--space-3);
  border-radius: var(--radius-pill);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
}

.sub-no {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  font-weight: var(--weight-normal);
}

.tone-warn {
  background: var(--color-warning-soft);
  color: var(--color-warning);
}

.tone-accent {
  background: var(--color-accent-soft);
  color: var(--color-accent);
}

.tone-success {
  background: var(--color-success-soft);
  color: var(--color-success);
}

.tone-muted {
  background: var(--color-bg-subtle);
  color: var(--color-text-tertiary);
}

/* ---------- 收货信息 ---------- */

.receiver {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.recv-line {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}

.recv-name {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.recv-phone {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.recv-addr {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: var(--leading-snug);
}

.recv-remark {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

/* ---------- 商品行 ---------- */

.items {
  list-style: none;
  margin: 0;
  padding: 0;
}

.item {
  display: grid;
  grid-template-columns: 72px minmax(160px, 1fr) 120px 130px;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
  cursor: pointer;
}

.item:last-child {
  border-bottom: none;
}

.cover {
  width: 72px;
  height: 72px;
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

.price-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.unit {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.qty {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.amount-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}

.was {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
  text-decoration: line-through;
}

.final {
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

/* ---------- 物流 ---------- */

.logistics {
  padding: var(--space-3) var(--space-4);
  border-top: 1px dashed var(--color-border);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.logi-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  font-size: var(--text-sm);
}

.logi-label {
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
  flex: 0 0 auto;
}

.logi-value {
  color: var(--color-text);
}

.logi-time {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

/* ---------- 子单汇总 ---------- */

.sub-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
}

.sub-amount {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.sub-amount-item.strong {
  color: var(--color-text);
  font-weight: var(--weight-medium);
}

.minus {
  color: var(--color-success);
}

/* ---------- 金额明细 ---------- */

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

.notice {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
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
    grid-template-columns: 60px 1fr auto;
    grid-template-areas:
      "cover info amount"
      "cover price amount";
    gap: var(--space-3);
  }

  .cover {
    width: 60px;
    height: 60px;
    grid-area: cover;
  }

  .info {
    grid-area: info;
  }

  .price-cell {
    grid-area: price;
    align-items: flex-start;
    flex-direction: row;
    gap: var(--space-2);
  }

  .amount-cell {
    grid-area: amount;
  }

  .sub-foot {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
