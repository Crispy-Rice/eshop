<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchHealth, fetchReady } from '@/api/system'
import { isBizError } from '@/api/errors'

const health = ref('检查中…')
const ready = ref<Record<string, string>>({})
const loading = ref(false)

async function check(): Promise<void> {
  loading.value = true
  health.value = '检查中…'
  ready.value = {}

  try {
    const res = await fetchHealth()
    health.value = res.status === 'ok' ? '正常' : res.status
  } catch (e) {
    health.value = isBizError(e) ? e.message : '无法连接后端'
  }

  try {
    const res = await fetchReady()
    ready.value = res.checks ?? {}
  } catch (e) {
    health.value = isBizError(e) ? e.message : health.value
  } finally {
    loading.value = false
  }
}

onMounted(check)
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-header">
        <span>系统状态</span>
        <el-button :loading="loading" size="small" @click="check">重新检查</el-button>
      </div>
    </template>

    <el-descriptions :column="1" border>
      <el-descriptions-item label="后端进程">
        <el-tag :type="health === '正常' ? 'success' : 'danger'">{{ health }}</el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="PostgreSQL">
        <el-tag :type="ready.postgres === 'ok' ? 'success' : 'danger'">
          {{ ready.postgres ?? '未探测' }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="Redis">
        <el-tag :type="ready.redis === 'ok' ? 'success' : 'danger'">
          {{ ready.redis ?? '未探测' }}
        </el-tag>
      </el-descriptions-item>
    </el-descriptions>

    <p class="hint">三项都是「正常 / ok」说明前端代理、FastAPI、数据库、缓存都已打通。</p>
  </el-card>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.hint {
  margin: var(--space-4) 0 0;
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}
</style>
