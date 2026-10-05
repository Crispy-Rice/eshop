<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  adjustStock,
  createWarehouse,
  listStock,
  listWarehouses,
  type StockItem,
  type Warehouse,
} from '@/api/inventory'

const warehouses = ref<Warehouse[]>([])
const items = ref<StockItem[]>([])
const loading = ref(false)
const warehouseFilter = ref<string>('')
const nextCursor = ref<string | null>(null)
const hasMore = ref(false)

const adjustVisible = ref(false)
const adjusting = ref(false)
const editing = ref<StockItem | null>(null)

const form = reactive({
  /** 正数入库、负数出库 */
  delta: 0,
  remark: '',
})

const currentWarehouse = computed(() =>
  warehouses.value.find((w) => w.id === warehouseFilter.value),
)

/**
 * 幂等键。
 *
 * ★ 必须用 `crypto.randomUUID`，它在**非安全上下文**（HTTP + IP 直连）里
 *   不存在，那时回落到 `getRandomValues` 拼一个（docs/10 §84）。
 *   同一个键只在"这一笔调整"里有效——重试要沿用同一个键，新调整要用新键。
 */
function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID().replace(/-/g, '').slice(0, 32)
  }
  const bytes = new Uint8Array(16)
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    crypto.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256)
  }
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

async function loadWarehouses(): Promise<void> {
  try {
    warehouses.value = await listWarehouses()
    if (!warehouseFilter.value && warehouses.value.length > 0) {
      // 默认选中默认仓
      const preferred = warehouses.value.find((w) => w.isDefault) ?? warehouses.value[0]
      warehouseFilter.value = preferred?.id ?? ''
    }
  } catch (e) {
    if (isBizError(e) && e.code === 'FORBIDDEN') {
      warehouses.value = []
    } else {
      ElMessage.error(isBizError(e) ? e.message : '加载仓库失败')
    }
  }
}

async function load(reset = true): Promise<void> {
  loading.value = true
  try {
    const result = await listStock({
      warehouseId: warehouseFilter.value || undefined,
      cursor: reset ? null : nextCursor.value,
      limit: 20,
    })
    items.value = reset ? result.items : [...items.value, ...result.items]
    nextCursor.value = result.nextCursor
    hasMore.value = result.hasMore
  } catch (e) {
    if (isBizError(e) && e.code === 'FORBIDDEN') {
      items.value = []
    } else {
      ElMessage.error(isBizError(e) ? e.message : '加载库存失败')
    }
  } finally {
    loading.value = false
  }
}

function openAdjust(row: StockItem): void {
  editing.value = row
  form.delta = 0
  form.remark = ''
  adjustVisible.value = true
}

async function submitAdjust(): Promise<void> {
  if (!editing.value) return
  if (form.delta === 0) {
    ElMessage.warning('调整数量不能为 0')
    return
  }

  adjusting.value = true
  try {
    const result = await adjustStock(
      {
        skuId: editing.value.skuId,
        warehouseId: editing.value.warehouseId,
        delta: form.delta,
        remark: form.remark || undefined,
      },
      newIdempotencyKey(),
    )
    ElMessage.success(`已调整：${result.beforeQty} → ${result.afterQty}`)
    adjustVisible.value = false
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '调整失败')
  } finally {
    adjusting.value = false
  }
}

/** 后台仓是每店铺一个默认仓，第一次进来时列表为空，引导建仓 */
async function ensureWarehouse(): Promise<void> {
  if (warehouses.value.length > 0) return
  try {
    const wh = await createWarehouse('默认仓库')
    warehouses.value = [wh]
    warehouseFilter.value = wh.id
  } catch (e) {
    if (!isBizError(e) || e.code !== 'FORBIDDEN') {
      ElMessage.error(isBizError(e) ? e.message : '创建仓库失败')
    }
  }
}

onMounted(async () => {
  await loadWarehouses()
  await ensureWarehouse()
  await load(true)
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <span class="field-label">仓库</span>
      <el-select
        v-model="warehouseFilter"
        placeholder="选择仓库"
        class="w-200"
        @change="load(true)"
      >
        <el-option v-for="w in warehouses" :key="w.id" :label="w.name" :value="w.id">
          <span>{{ w.name }}</span>
          <span v-if="w.isDefault" class="tag-default">默认</span>
        </el-option>
      </el-select>

      <span v-if="currentWarehouse" class="hint">
        手工调整会写入流水并记录操作人，可在「库存流水」页查看
      </span>

      <div class="spacer" />

      <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
      <RouterLink to="/inventory/flows">
        <el-button size="small">库存流水</el-button>
      </RouterLink>
    </div>

    <div class="panel">
      <el-empty v-if="!loading && items.length === 0" description="这个仓库还没有库存记录">
        <template #description>
          <p>这个仓库还没有库存记录。</p>
          <p class="empty-hint">先在「我的商品」里发布商品，再回到这里设置初始库存。</p>
        </template>
      </el-empty>

      <el-table v-else v-loading="loading" :data="items">
        <el-table-column label="商品" min-width="260">
          <template #default="{ row }">
            <div class="cell-product">
              <div class="cell-text">
                <div class="cell-title">{{ row.spuTitle }}</div>
                <div class="cell-sub">{{ row.specText || '—' }}</div>
                <div class="cell-code tnum">
                  <template v-if="row.skuCode">{{ row.skuCode }}</template>
                  <!-- 编码是选填的。没填时只有"取不到商品"那种异常行才退回 skuId 当线索 ——
                       正常行显示一串 19 位雪花 ID 只是噪音 -->
                  <template v-else-if="!row.spuTitle">{{ row.skuId }}</template>
                  <template v-else>—</template>
                </div>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="可售" width="90" align="right">
          <template #default="{ row }">
            <span class="tnum strong">{{ row.available }}</span>
          </template>
        </el-table-column>

        <el-table-column label="预占" width="80" align="right">
          <template #default="{ row }">
            <span class="tnum">{{ row.locked }}</span>
          </template>
        </el-table-column>

        <el-table-column label="冻结" width="80" align="right">
          <template #default="{ row }">
            <span class="tnum">{{ row.frozen }}</span>
          </template>
        </el-table-column>

        <el-table-column label="总量" width="80" align="right">
          <template #default="{ row }">
            <span class="tnum">{{ row.total }}</span>
          </template>
        </el-table-column>

        <el-table-column label="仓库" width="120">
          <template #default="{ row }">{{ row.warehouseName }}</template>
        </el-table-column>

        <el-table-column label="操作" width="90" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openAdjust(row)">调整</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="hasMore" class="more">
        <el-button size="small" :loading="loading" @click="load(false)">加载更多</el-button>
      </div>
    </div>

    <el-dialog v-model="adjustVisible" title="调整库存" width="440px">
      <div v-if="editing" class="dialog-body">
        <div class="dialog-product">
          <div class="cell-title">{{ editing.spuTitle }}</div>
          <div class="cell-sub">{{ editing.specText || '—' }}</div>
        </div>

        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="当前可售">
            <span class="tnum">{{ editing.available }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="总量">
            <span class="tnum">{{ editing.total }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <el-form label-width="80px" class="dialog-form">
          <el-form-item label="调整数量">
            <el-input-number v-model="form.delta" :controls="false" class="w-full" />
            <div class="field-hint">正数入库、负数出库</div>
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="form.remark" maxlength="255" placeholder="选填，会记入流水" />
          </el-form-item>
        </el-form>
      </div>

      <template #footer>
        <el-button @click="adjustVisible = false">取消</el-button>
        <el-button type="primary" :loading="adjusting" @click="submitAdjust">确认调整</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 样式全部走语义 token，见 docs/17-frontend-design-system.md */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.field-label {
  font-size: var(--text-base);
  color: var(--color-text-tertiary);
}

.spacer {
  flex: 1;
}

.hint {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.tag-default {
  margin-left: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.w-200 {
  width: 200px;
}

.w-full {
  width: 100%;
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  padding: var(--space-1) 0;
}

.panel :deep(.el-empty) {
  padding: var(--space-8) 0;
}

.empty-hint {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text-placeholder);
}

.cell-text {
  min-width: 0;
}

.cell-title {
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
}

.cell-sub {
  margin-top: 2px;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.cell-code {
  margin-top: 2px;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.strong {
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.more {
  padding: var(--space-4) 0;
  text-align: center;
}

.dialog-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.dialog-product {
  padding: var(--space-3);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
}

.dialog-form {
  margin-top: var(--space-1);
}

.field-hint {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}
</style>
