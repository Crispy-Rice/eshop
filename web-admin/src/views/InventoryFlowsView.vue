<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import { listFlows, type StockFlow } from '@/api/inventory'

const items = ref<StockFlow[]>([])
const loading = ref(false)
const nextCursor = ref<string | null>(null)
const hasMore = ref(false)

async function load(reset = true): Promise<void> {
  loading.value = true
  try {
    const result = await listFlows({ cursor: reset ? null : nextCursor.value })
    items.value = reset ? result.items : [...items.value, ...result.items]
    nextCursor.value = result.nextCursor
    hasMore.value = result.hasMore
  } catch (e) {
    if (isBizError(e) && e.code === 'FORBIDDEN') {
      items.value = []
    } else {
      ElMessage.error(isBizError(e) ? e.message : '加载流水失败')
    }
  } finally {
    loading.value = false
  }
}

/** 变动量正数=入库、负数=出库，用颜色把方向标出来 */
function numClass(num: number): string {
  if (num > 0) return 'num num-in'
  if (num < 0) return 'num num-out'
  return 'num'
}

onMounted(() => void load(true))
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">库存流水</h2>
      <span class="hint">每笔库存变更都会留痕，是排查差异的唯一依据</span>
      <div class="spacer" />
      <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
      <RouterLink to="/inventory">
        <el-button size="small">返回库存</el-button>
      </RouterLink>
    </div>

    <div class="panel">
      <el-empty v-if="!loading && items.length === 0" description="还没有库存流水" />

      <el-table v-else v-loading="loading" :data="items">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">
            <span class="tnum time">{{ new Date(row.createdAt).toLocaleString('zh-CN') }}</span>
          </template>
        </el-table-column>

        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag size="small" disable-transitions>{{ row.changeTypeText }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="变动" width="90" align="right">
          <template #default="{ row }">
            <span :class="numClass(row.num)">
              {{ row.num > 0 ? `+${row.num}` : row.num }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="变更前" width="90" align="right">
          <template #default="{ row }"><span class="tnum">{{ row.beforeQty }}</span></template>
        </el-table-column>

        <el-table-column label="变更后" width="90" align="right">
          <template #default="{ row }"><span class="tnum">{{ row.afterQty }}</span></template>
        </el-table-column>

        <el-table-column label="关联单据" width="180">
          <template #default="{ row }">
            <span class="tnum muted">{{ row.orderNo || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作人" width="150">
          <template #default="{ row }">
            <span class="muted">{{ row.operator || '系统' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="备注" min-width="160">
          <template #default="{ row }">
            <span class="muted">{{ row.remark || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="hasMore" class="more">
        <el-button size="small" :loading="loading" @click="load(false)">加载更多</el-button>
      </div>
    </div>
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
  align-items: baseline;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.hint {
  font-size: var(--text-sm);
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
  padding: var(--space-1) 0;
}

.panel :deep(.el-empty) {
  padding: var(--space-8) 0;
}

.time,
.muted {
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}

/* 入库用成功色、出库用危险色，一眼看清方向 */
.num {
  font-variant-numeric: tabular-nums;
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.num-in {
  color: var(--color-success);
}

.num-out {
  color: var(--color-danger);
}

.more {
  padding: var(--space-4) 0;
  text-align: center;
}
</style>
