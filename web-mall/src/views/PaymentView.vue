<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import { PAY_CLOSED, PAY_SUCCESS, fetchPayment, mockPayCallback, type Payment } from '@/api/trade'
import { formatYuan } from '@/utils/money'

const route = useRoute()
const router = useRouter()

const payment = ref<Payment | null>(null)
const loading = ref(false)
const paying = ref(false)

const payNo = computed(() => String(route.params.payNo ?? ''))
const done = computed(() => payment.value?.status === PAY_SUCCESS)
const closed = computed(() => payment.value?.status === PAY_CLOSED)

async function load(): Promise<void> {
  loading.value = true
  try {
    payment.value = await fetchPayment(payNo.value)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载支付单失败')
  } finally {
    loading.value = false
  }
}

/**
 * 「确认支付」。
 *
 * ★ 这一个按钮同时扮演两个角色：**用户在收银台付款** + **渠道回调我们**。
 *   真实接渠道时前者发生在微信/支付宝的页面上，后者由渠道服务器 POST 到
 *   `/api/payments/{payNo}/mock-callback`（docs/09 §4）。
 *   两条路径调的是同一个回调接口，所以模拟的语义和真实是一致的。
 *
 *   回调是**幂等**的：连点、超时重发都不会重复推进订单。
 */
async function onPay(): Promise<void> {
  paying.value = true
  try {
    payment.value = await mockPayCallback(payNo.value)
    ElMessage.success('支付成功')
    goOrder()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '支付失败')
    await load()
  } finally {
    paying.value = false
  }
}

function goOrder(): void {
  const mainNo = payment.value?.orderMainNo
  if (mainNo) {
    void router.replace({ name: 'order-detail', params: { orderMainNo: mainNo } })
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div v-loading="loading" class="page">
    <el-empty v-if="!loading && payment === null" description="支付单不存在">
      <el-button type="primary" @click="router.push({ name: 'orders' })">返回订单列表</el-button>
    </el-empty>

    <template v-else-if="payment">
      <section class="card">
        <header class="card-head">
          <span class="method-dot" />
          <span class="method">模拟收银台</span>
          <span class="channel tnum">{{ payment.channel }}</span>
        </header>

        <div class="amount-block">
          <p class="amount-label">应付金额</p>
          <p class="amount tnum">
            <span class="symbol">¥</span>{{ formatYuan(payment.amount) }}
          </p>
          <p class="order-no tnum">订单号 {{ payment.orderMainNo }}</p>
        </div>

        <div class="rows">
          <div class="row">
            <span>支付单号</span>
            <span class="tnum">{{ payment.payNo }}</span>
          </div>
          <div class="row">
            <span>状态</span>
            <span class="v-status" :class="{ ok: done, over: closed }">{{ payment.statusText }}</span>
          </div>
          <div v-if="payment.payTime" class="row">
            <span>支付时间</span>
            <span class="tnum">{{ payment.payTime }}</span>
          </div>
        </div>

        <div class="tip">
          这是演示环境：点「确认支付」会直接触发模拟渠道的回调。 回调是幂等的，重复点击不会重复扣款。
        </div>

        <footer class="actions">
          <template v-if="done">
            <el-result icon="success" title="支付成功" sub-title="订单已进入待发货状态" />
            <el-button type="primary" @click="goOrder">查看订单</el-button>
          </template>
          <template v-else-if="closed">
            <el-alert type="info" :closable="false" title="支付单已关闭" show-icon />
            <el-button @click="goOrder">查看订单</el-button>
          </template>
          <template v-else>
            <el-button :disabled="paying" @click="router.back()">返回</el-button>
            <el-button type="primary" size="large" :loading="paying" @click="onPay">
              确认支付 ¥{{ formatYuan(payment.amount) }}
            </el-button>
          </template>
        </footer>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  justify-content: center;
  padding-top: var(--space-6);
}

.card {
  width: 100%;
  max-width: 520px;
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.card-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
}

.method-dot {
  width: 10px;
  height: 10px;
  border-radius: var(--radius-pill);
  background: var(--color-accent);
}

.method {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.channel {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.amount-block {
  padding: var(--space-8) var(--space-4) var(--space-6);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
}

.amount-label {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

/* 金额是这一页唯一的主角，字号要压过其他一切 */
.amount {
  font-size: 40px;
  line-height: 1.1;
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

.symbol {
  font-size: var(--text-xl);
  margin-right: 2px;
}

.order-no {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.rows {
  padding: 0 var(--space-4) var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.row {
  display: flex;
  justify-content: space-between;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.v-status {
  font-weight: var(--weight-medium);
}

.v-status.ok {
  color: var(--color-success);
}

.v-status.over {
  color: var(--color-text-tertiary);
}

.tip {
  margin: 0 var(--space-4) var(--space-4);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-warning-soft);
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  line-height: var(--leading-snug);
}

.actions {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  border-top: 1px solid var(--color-border);
}

.actions :deep(.el-result) {
  padding: var(--space-2) 0;
}
</style>
