<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchActiveActivities, type ActiveActivity } from '@/api/site'

/**
 * 顶部活动公告。
 *
 * 数据是 `GET /api/promotions/active` —— **真实进行中的平台级活动**，
 * 所以这条带说的每一句都是真的。它以前不是这样：文案硬编码在皮肤定义里
 * （"跨店每满 300 减 50"「定金膨胀 · 尾款立减」这类**优惠承诺**），
 * 用户切一下皮肤就看到并不存在的活动。
 *
 * ★ 没有进行中的活动就**整条不渲染**，而不是显示一句"暂无活动"的空话。
 * ★ 结构与 token 一行没动：换皮照旧（`--promo-*` 跟着皮肤走，大促皮肤下是
 *   渐变横幅，中性皮肤下是一条安静的灰带）。变的只是"文案从哪来"。
 */
const activities = ref<ActiveActivity[]>([])

/** **真实**总数，不是 `activities.length` —— 列表有上限，见 `fetchActiveActivities`。 */
const total = ref(0)

/** 最快结束的那个打头 —— 公告先说要紧的 */
const primary = computed(() => activities.value[0] ?? null)

/** "10-07"。只到日：公告用不上具体到分钟。 */
function formatDay(iso: string): string {
  const d = new Date(iso)
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')}`
}

const endsAt = computed(() => (primary.value ? `${formatDay(primary.value.endAt)} 结束` : ''))

/** 还有别的活动在跑时补一句，免得用户以为只有一个 */
const more = computed(() => (total.value > 1 ? `还有 ${total.value - 1} 个活动进行中` : ''))

/**
 * 悬停提示：把**这一次能列出来的**活动都写清楚（名字 + 结束日）。
 *
 * ★ 为什么要有它：公告带只放得下一条标题，其余的活动原本只是一个数字，
 *   用户看不到是什么。这条 title 用零布局改动的代价把名字补上。
 * ★ 列表被上限截断时补一句"等 N 个"—— 连总数都说不全就又开始撒谎了。
 */
const tooltip = computed(() => {
  if (!activities.value.length) return ''
  const listed = activities.value.map((a) => `${a.name}（${formatDay(a.endAt)} 结束）`)
  const rest = total.value - activities.value.length
  return `进行中的平台活动：${listed.join('、')}${rest > 0 ? ` 等 ${total.value} 个` : ''}`
})

onMounted(async () => {
  try {
    const data = await fetchActiveActivities()
    activities.value = data.items
    total.value = data.total
  } catch {
    // 公告拉不到就当没有：不值得为一条横幅弹错误提示，更不该挡住页面
    activities.value = []
    total.value = 0
  }
})
</script>

<template>
  <section v-if="primary" class="promo is-festive" :title="tooltip">
    <div class="promo-inner">
      <span class="promo-badge">活动进行中</span>
      <h2 class="promo-headline">{{ primary.name }}</h2>
      <p class="promo-slogan">
        {{ endsAt }}<template v-if="more"> · {{ more }}</template>
      </p>
    </div>
  </section>
</template>

<style scoped>
.promo {
  background: var(--promo-hero-bg);
  color: var(--promo-hero-text);
  border-bottom: 1px solid var(--color-border);
}

/* 中性主题是"静灰带"，不需要分隔线压得太重 */
.promo.is-festive {
  border-bottom-color: transparent;
}

.promo-inner {
  max-width: var(--layout-max);
  margin: 0 auto;
  padding: var(--space-4) var(--layout-gutter);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.promo-badge {
  flex: 0 0 auto;
  padding: 2px var(--space-2);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  letter-spacing: 0.02em;
  border-radius: var(--radius-sm);
  background: var(--promo-badge-bg);
  color: var(--promo-badge-text);
}

.promo-headline {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
}

.promo-slogan {
  font-size: var(--text-sm);
  color: var(--promo-hero-text-soft);
}
</style>
