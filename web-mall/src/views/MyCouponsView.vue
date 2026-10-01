<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import { CODE_UNUSED, fetchMyCoupons, type MyCoupon } from '@/api/promotion'
import { useAuthStore } from '@/stores/auth'
import { formatYuan } from '@/utils/money'

const auth = useAuthStore()
const router = useRouter()

const coupons = ref<MyCoupon[]>([])
const loading = ref(false)
const tab = ref<'unused' | 'used' | 'expired'>('unused')

const tabs = [
  { key: 'unused' as const, label: '可使用' },
  { key: 'used' as const, label: '已使用' },
  { key: 'expired' as const, label: '已过期' },
]

/** 按当前 tab 过滤。过期判定用 `expired`（实时算的），不用库里的 status */
const shown = computed(() => {
  return coupons.value.filter((c) => {
    if (tab.value === 'used') return c.status === 3
    if (tab.value === 'expired') return c.expired || c.status === 4
    return c.status === CODE_UNUSED && !c.expired
  })
})

async function load(): Promise<void> {
  loading.value = true
  try {
    coupons.value = await fetchMyCoupons()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载我的券失败')
  } finally {
    loading.value = false
  }
}

function faceValue(c: MyCoupon): string {
  if (c.couponType === 2) {
    return `${(c.discountValue / 1000).toFixed(1).replace(/\.0$/, '')} 折`
  }
  return `¥${formatYuan(c.discountValue)}`
}

function thresholdText(c: MyCoupon): string {
  return c.threshold <= 0 ? '无门槛' : `满 ¥${formatYuan(c.threshold)} 可用`
}

/** 可用状态用更强的视觉，已用/过期整块降低不透明度 */
function isDim(c: MyCoupon): boolean {
  return c.expired || c.status === 3 || c.status === 4
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: '/my/coupons' } })
    return
  }
  await load()
})
</script>

<template>
  <div class="page">
    <header class="head">
      <h1 class="title">我的优惠券</h1>
      <div class="spacer" />
      <RouterLink to="/coupons">
        <el-button size="small" type="primary">去领券</el-button>
      </RouterLink>
    </header>

    <div class="tabs">
      <button
        v-for="t in tabs"
        :key="t.key"
        type="button"
        class="tab"
        :class="{ active: tab === t.key }"
        @click="tab = t.key"
      >
        {{ t.label }}
      </button>
    </div>

    <div v-loading="loading" class="grid">
      <el-empty v-if="!loading && shown.length === 0" :description="`没有${tabs.find((t) => t.key === tab)?.label}的券`" />

      <article v-for="c in shown" :key="c.id" class="coupon" :class="{ dim: isDim(c) }">
        <div class="face">
          <span class="face-value tnum">{{ faceValue(c) }}</span>
          <span class="face-threshold">{{ thresholdText(c) }}</span>
        </div>

        <div class="body">
          <h3 class="name">{{ c.name }}</h3>
          <p class="meta">
            有效期 {{ new Date(c.validStart).toLocaleDateString('zh-CN') }} ~
            {{ new Date(c.validEnd).toLocaleDateString('zh-CN') }}
          </p>
          <p class="code tnum">券码 {{ c.code }}</p>
        </div>

        <div class="action">
          <el-tag
            :type="c.status === CODE_UNUSED && !c.expired ? 'success' : 'info'"
            size="small"
            disable-transitions
          >
            {{ c.expired && c.status === CODE_UNUSED ? '已过期' : c.statusText }}
          </el-tag>
        </div>
      </article>
    </div>
  </div>
</template>

<style scoped>
/* 只用语义 token，见 docs/17-frontend-design-system.md */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.title {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.spacer {
  flex: 1;
}

/* 分段控件：当前标签用强调色下划线，比实心块更轻（后台也用的这套语言） */
.tabs {
  display: flex;
  gap: var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.tab {
  position: relative;
  padding: var(--space-3) 0;
  border: none;
  background: none;
  font-size: var(--text-base);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease-out);
}

.tab:hover {
  color: var(--color-text);
}

.tab.active {
  color: var(--color-text);
  font-weight: var(--weight-medium);
}

.tab.active::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 2px;
  background: var(--color-accent);
  border-radius: var(--radius-pill);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--space-4);
  min-height: 120px;
}

.coupon {
  display: grid;
  grid-template-columns: 110px 1fr auto;
  align-items: center;
  background: var(--card-bg);
  border: var(--card-border);
  border-radius: var(--card-radius);
  overflow: hidden;
}

/* 已用/过期：整体降透明度，一眼区分 */
.coupon.dim {
  opacity: 0.6;
}

.face {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-1);
  padding: var(--space-4) var(--space-2);
  background: var(--color-accent-soft);
  border-right: 1px dashed var(--color-border-strong);
  align-self: stretch;
}

.face-value {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-accent);
  line-height: var(--leading-tight);
}

.face-threshold {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.body {
  padding: var(--space-3) var(--space-4);
  min-width: 0;
}

.name {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.meta {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.code {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.action {
  padding-right: var(--space-4);
}

@media (max-width: 900px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
