<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchHealth, fetchReady } from '@/api/system'
import { isBizError } from '@/api/errors'

/**
 * 骨架自检页：验证「浏览器 → Vite 代理 → FastAPI → PostgreSQL/Redis」整条链路。
 * 业务页面做好之后这个页面可以移除。
 */
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
        <span>后台 · 前后端连通性自检</span>
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

    <p class="hint">
      后台前端跑在 5174 端口，商城前端跑在 5173 端口，两者共用同一个后端。
    </p>
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
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}
</style>
