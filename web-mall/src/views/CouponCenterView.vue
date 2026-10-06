<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  fetchAvailableCoupons,
  receiveCoupon,
  type ReceivableCoupon,
} from '@/api/promotion'
import { useAuthStore } from '@/stores/auth'
import { newIdempotencyKey } from '@/utils/idempotency'
import { formatYuan } from '@/utils/money'

const auth = useAuthStore()
const router = useRouter()

const coupons = ref<ReceivableCoupon[]>([])
const loading = ref(false)
/** 正在领取的模板 id，避免连点 */
const receiving = ref<string | null>(null)

async function load(): Promise<void> {
  loading.value = true
  try {
    coupons.value = await fetchAvailableCoupons()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载券中心失败')
  } finally {
    loading.value = false
  }
}

async function onReceive(item: ReceivableCoupon): Promise<void> {
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: '/coupons' } })
    return
  }

  const key = newIdempotencyKey()
  receiving.value = item.template.id
  try {
    await receiveCoupon(item.template.id, key)
    ElMessage.success(`已领取「${item.template.name}」`)
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '领取失败')
    await load()
  } finally {
    receiving.value = null
  }
}

/** 面额文案：满减/无门槛显示金额，折扣显示几折 */
function faceValue(c: ReceivableCoupon): string {
  const t = c.template
  if (t.type === 2) {
    const zhe = (t.discountValue / 1000).toFixed(1).replace(/\.0$/, '')
    return `${zhe} 折`
  }
  return `¥${formatYuan(t.discountValue)}`
}

function thresholdText(c: ReceivableCoupon): string {
  const t = c.template
  if (t.threshold <= 0) return '无门槛'
  return `满 ¥${formatYuan(t.threshold)} 可用`
}

/** 已经领过的券，按钮要置灰并说清原因，不能让它看起来还能点 */
function buttonText(c: ReceivableCoupon): string {
  if (!c.canReceive) {
    if (c.received >= c.template.perUserLimit) return '已领取'
    return '已抢光'
  }
  return '立即领取'
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="head">
      <h1 class="title">领券中心</h1>
      <p class="sub">领了之后在结算页使用，可叠加的券会自动择优</p>
      <div class="spacer" />
      <RouterLink to="/my/coupons">
        <el-button size="small">我的券</el-button>
      </RouterLink>
    </header>

    <div v-loading="loading" class="grid">
      <el-empty v-if="!loading && coupons.length === 0" description="暂时没有可领取的券" />

      <article v-for="c in coupons" :key="c.template.id" class="coupon">
        <div class="face">
          <span class="face-value tnum">{{ faceValue(c) }}</span>
          <span class="face-threshold">{{ thresholdText(c) }}</span>
        </div>

        <div class="body">
          <h3 class="name">{{ c.template.name }}</h3>
          <p class="meta">
            <span v-if="c.template.validDays">领取后 {{ c.template.validDays }} 天内有效</span>
            <span v-else-if="c.template.validEnd">
              有效期至 {{ new Date(c.template.validEnd).toLocaleDateString('zh-CN') }}
            </span>
          </p>
          <p class="meta">
            <span v-if="c.template.maxDiscount > 0">
              最高减 ¥{{ formatYuan(c.template.maxDiscount) }} ·
            </span>
            <!-- 限领多张时要说清"已领几张"，否则领完一张按钮还是「立即领取」，
                 用户会以为没领上（其实是还能再领） -->
            <span v-if="c.template.perUserLimit > 1">
              每人限领 {{ c.template.perUserLimit }} 张（已领 {{ c.received }}） ·
            </span>
            <span>剩余 {{ c.remain }} 张</span>
          </p>
        </div>

        <div class="action">
          <el-button
            type="primary"
            size="small"
            :disabled="!c.canReceive"
            :loading="receiving === c.template.id"
            @click="onReceive(c)"
          >
            {{ buttonText(c) }}
          </el-button>
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
  align-items: baseline;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.title {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.sub {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.spacer {
  flex: 1;
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
  transition: border-color var(--dur) var(--ease-out);
}

.coupon:hover {
  border-color: var(--color-border-strong);
}

/* 券的面额区：用强调色的柔和底，换肤时跟着变 */
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

.action {
  padding-right: var(--space-4);
}

@media (max-width: 900px) {
  .grid {
    grid-template-columns: 1fr;
  }

  .coupon {
    grid-template-columns: 90px 1fr;
    grid-template-areas:
      "face body"
      "face action";
  }

  .face {
    grid-area: face;
  }

  .body {
    grid-area: body;
  }

  .action {
    grid-area: action;
    padding: 0 var(--space-4) var(--space-3);
  }
}
</style>
