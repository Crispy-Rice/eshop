<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  MERCHANT_TABS,
  fetchMerchantOrders,
  orderTagType,
  shipOrder,
  type MerchantOrder,
} from '@/api/trade'
import { formatDateTime, formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const tab = ref(0)
const orders = ref<MerchantOrder[]>([])
const cursor = ref<string | null>(null)
const hasMore = ref(false)
const loading = ref(false)
const loadingMore = ref(false)

/** 发货弹窗 */
const shipVisible = ref(false)
const shipping = ref(false)
const target = ref<MerchantOrder | null>(null)
const form = reactive({ expressCompany: '', expressNo: '' })

const CARRIERS = ['顺丰速运', '中通快递', '圆通速递', '韵达快递', '京东物流', '邮政EMS']

async function load(reset = true): Promise<void> {
  if (reset) {
    loading.value = true
    cursor.value = null
  } else {
    loadingMore.value = true
  }
  try {
    const page = await fetchMerchantOrders({
      status: MERCHANT_TABS[tab.value]?.status,
      cursor: reset ? undefined : (cursor.value ?? undefined),
      limit: 20,
    })
    orders.value = reset ? page.items : [...orders.value, ...page.items]
    cursor.value = page.nextCursor
    hasMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载订单失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchTab(index: number): Promise<void> {
  if (tab.value === index) return
  tab.value = index
  await load(true)
}

function openShip(order: MerchantOrder): void {
  target.value = order
  form.expressCompany = ''
  form.expressNo = ''
  shipVisible.value = true
}

async function submitShip(): Promise<void> {
  if (target.value === null) return
  if (!form.expressCompany || !form.expressNo.trim()) {
    ElMessage.warning('请填写快递公司与快递单号')
    return
  }
  shipping.value = true
  try {
    await shipOrder(target.value.orderSubNo, {
      expressCompany: form.expressCompany,
      expressNo: form.expressNo.trim(),
    })
    ElMessage.success('发货成功')
    shipVisible.value = false
    await load(true)
  } catch (e) {
    // 重复发货会被状态机拒绝（422）—— 如实告诉商家单号没被改掉
    ElMessage.error(isBizError(e) ? e.message : '发货失败')
  } finally {
    shipping.value = false
  }
}

onMounted(() => {
  void load(true)
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">订单管理</h2>
      <span class="hint">只显示本店铺的子单；跨店订单在买家侧是一张母单</span>
      <div class="spacer" />
      <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
    </div>

    <el-radio-group :model-value="tab" size="small" @change="switchTab($event as number)">
      <el-radio-button v-for="(t, i) in MERCHANT_TABS" :key="t.label" :value="i">
        {{ t.label }}
      </el-radio-button>
    </el-radio-group>

    <div class="panel">
      <el-empty v-if="!loading && orders.length === 0" description="没有订单" />

      <el-table v-else v-loading="loading" :data="orders">
        <el-table-column label="商品" min-width="280">
          <template #default="{ row }">
            <div class="goods">
              <img
                v-for="(img, i) in row.previewImages"
                :key="i"
                :src="img"
                :alt="row.previewTitles[i] ?? '商品'"
                class="goods-img"
                @error="onImageError"
              />
              <div class="goods-text">
                <div class="goods-title">{{ row.previewTitles.join(' / ') }}</div>
                <div class="goods-sub">
                  共 {{ row.totalNum }} 件 · {{ row.itemKindCount }} 种
                </div>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="订单号" width="200">
          <template #default="{ row }">
            <div class="cell-mono tnum">{{ row.orderSubNo }}</div>
            <div class="cell-sub tnum">{{ row.orderMainNo }}</div>
          </template>
        </el-table-column>

        <el-table-column label="买家 / 收货" min-width="200">
          <template #default="{ row }">
            <div class="cell-title">{{ row.buyerName }}</div>
            <div class="cell-sub tnum">{{ row.buyerPhone }}</div>
            <div class="cell-sub addr">{{ row.receiverFull }}</div>
          </template>
        </el-table-column>

        <el-table-column label="金额" width="140" align="right">
          <template #default="{ row }">
            <div class="money tnum">¥{{ formatYuan(row.payableAmount) }}</div>
            <div class="cell-sub tnum">
              商品 ¥{{ formatYuan(row.totalAmount) }}
              <template v-if="row.freightAmount > 0">
                + 运费 ¥{{ formatYuan(row.freightAmount) }}
              </template>
            </div>
            <div v-if="row.discountAmount > 0" class="cell-sub minus tnum">
              已优惠 ¥{{ formatYuan(row.discountAmount) }}
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="orderTagType(row.status)" size="small" effect="light">
              {{ row.statusText }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="下单时间" width="140">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatDateTime(row.createTime) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="物流" width="180">
          <template #default="{ row }">
            <template v-if="row.deliveries.length">
              <div v-for="d in row.deliveries" :key="d.deliveryNo" class="cell-sub">
                {{ d.expressCompany }}
                <span class="tnum">{{ d.expressNo }}</span>
              </div>
            </template>
            <span v-else class="cell-sub">—</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="100" align="right" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.canShip" type="primary" size="small" @click="openShip(row)">
              发货
            </el-button>
            <span v-else class="cell-sub">—</span>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="hasMore" class="more">
        <el-button size="small" :loading="loadingMore" @click="load(false)">加载更多</el-button>
      </div>
    </div>

    <el-dialog v-model="shipVisible" title="发货" width="440px">
      <div v-if="target" class="ship-target">
        <div class="ship-line">
          <span class="ship-label">订单号</span>
          <span class="tnum">{{ target.orderSubNo }}</span>
        </div>
        <div class="ship-line">
          <span class="ship-label">收货人</span>
          <span>{{ target.buyerName }} {{ target.buyerPhone }}</span>
        </div>
        <div class="ship-line">
          <span class="ship-label">地址</span>
          <span class="ship-addr">{{ target.receiverFull }}</span>
        </div>
      </div>

      <el-form label-width="80px" class="ship-form">
        <el-form-item label="快递公司">
          <el-select v-model="form.expressCompany" placeholder="选择或输入" filterable allow-create>
            <el-option v-for="c in CARRIERS" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="快递单号">
          <el-input v-model="form.expressNo" placeholder="如 SF1234567890" maxlength="64" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="shipVisible = false">取消</el-button>
        <el-button type="primary" :loading="shipping" @click="submitShip">确认发货</el-button>
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

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

/* ---------- 表格单元格 ---------- */

.goods {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.goods-img {
  width: 40px;
  height: 40px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--media-bg);
  flex: 0 0 auto;
}

.goods-text {
  min-width: 0;
}

.goods-title {
  font-size: var(--text-sm);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.goods-sub,
.cell-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.cell-title {
  font-size: var(--text-sm);
  color: var(--color-text);
}

.cell-mono {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.addr {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}

.money {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

.minus {
  color: var(--color-success);
}

.more {
  display: flex;
  justify-content: center;
  padding: var(--space-3);
}

/* ---------- 发货弹窗 ---------- */

.ship-target {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.ship-line {
  display: flex;
  gap: var(--space-3);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.ship-label {
  flex: 0 0 48px;
  color: var(--color-text-tertiary);
}

.ship-addr {
  line-height: var(--leading-snug);
}

.ship-form :deep(.el-select) {
  width: 100%;
}
</style>
