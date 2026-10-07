<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { isBizError } from '@/api/errors'
import { post } from '@/api/http'
import {
  deleteSpu,
  listMySpus,
  offShelf,
  onShelf,
  submitForAudit,
  SPU_STATUS_TYPE,
  type SpuCard,
} from '@/api/product'
import { useAuthStore } from '@/stores/auth'
import { formatPriceRange } from '@/utils/money'
import { onImageError, thumbFallback, thumbSrc } from '@/utils/placeholder'

const auth = useAuthStore()
const router = useRouter()

const items = ref<SpuCard[]>([])
const loading = ref(false)
const statusFilter = ref<number | undefined>(undefined)
const shopLoading = ref(false)

/**
 * 平台账号（admin / finance）不开店。
 *
 * 后端 ``create_shop`` 会直接 403，所以这不只是"藏一个按钮"：给平台账号看
 * "你还没有店铺"本身就是错的 —— 商家菜单按店铺归属分，运营本来就不该有店铺。
 */
const ROLE_TEXT: Record<string, string> = { admin: '平台运营', finance: '财务' }

const isPlatformRole = computed(
  () => auth.user?.role === 'admin' || auth.user?.role === 'finance',
)

const noShopTitle = computed(() => (isPlatformRole.value ? '平台账号' : '你还没有店铺'))

const noShopHint = computed(() =>
  isPlatformRole.value
    ? `当前是${ROLE_TEXT[auth.user?.role ?? ''] ?? '平台'}账号，不需要开店。`
    : '上架商品前需要先开通店铺。',
)

const statusTabs = [
  { label: '全部', value: undefined },
  { label: '草稿', value: 1 },
  // 已驳回单独一档：它和草稿是两件事 —— 一个是"我还没写完"，
  // 一个是"平台打回来了，等我改"。混在一起商家不知道先动哪个。
  { label: '已驳回', value: 6 },
  { label: '待审核', value: 5 },
  { label: '已上架', value: 2 },
  { label: '已下架', value: 3 },
]

async function load(): Promise<void> {
  if (shopLoading.value) return
  loading.value = true
  try {
    const result = await listMySpus({ status: statusFilter.value, limit: 60 })
    items.value = result.items
  } catch (e) {
    // 没开店时后端返回 403，这里不当作错误弹窗，页面会显示引导
    if (isBizError(e) && e.code === 'FORBIDDEN') {
      items.value = []
    } else {
      ElMessage.error(isBizError(e) ? e.message : '加载失败')
    }
  } finally {
    loading.value = false
  }
}

async function openShop(): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt('给你的店铺起个名字', '开通店铺', {
      inputPattern: /^.{2,64}$/,
      inputErrorMessage: '店铺名 2~64 个字符',
      confirmButtonText: '开通',
    })
    shopLoading.value = true
    await post('/merchant/shop', { name: value })
    await auth.restore()
    ElMessage.success('店铺已开通')
    await load()
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(isBizError(e) ? e.message : '开通失败')
  } finally {
    shopLoading.value = false
  }
}

async function act(item: SpuCard, action: 'submit' | 'on' | 'off'): Promise<void> {
  const labels = { submit: '提交审核', on: '上架', off: '下架' }
  try {
    await ElMessageBox.confirm(`确定要${labels[action]}「${item.title}」吗？`, '确认', {
      type: 'warning',
    })
  } catch {
    return
  }

  try {
    if (action === 'submit') await submitForAudit(item.id)
    else if (action === 'on') await onShelf(item.id)
    else await offShelf(item.id)
    ElMessage.success(`${labels[action]}成功`)
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : `${labels[action]}失败`)
  }
}

/**
 * 删除商品（软删）。
 *
 * ★ 确认框里必须说清"历史订单不受影响" —— 商家一听"删除"就担心已卖出去的订单
 *   会不会跟着没了，不说清楚他不敢点。实际情况是订单读的是下单时的快照，一点影响都没有。
 */
async function remove(item: SpuCard): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除「${item.title}」吗？商城将不再展示它；已产生的历史订单不受影响。`,
      '删除商品',
      { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return // 用户取消
  }

  try {
    await deleteSpu(item.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    // 在售 / 审核中会被后端拒绝。原样弹文案 —— "商品在售，请先下架再删除"
    // 比"删除失败"有用得多，商家看到就知道下一步干什么
    ElMessage.error(isBizError(e) ? e.message : '删除失败')
  }
}

onMounted(() => {
  void auth.restore().then(load)
})
</script>

<template>
  <div class="page">
    <el-alert
      v-if="auth.user && !auth.user.shopId"
      type="info"
      :closable="false"
      class="shop-alert"
      show-icon
    >
      <template #title>{{ noShopTitle }}</template>
      <div class="alert-body">
        <span>{{ noShopHint }}</span>
        <el-button
          v-if="!isPlatformRole"
          type="primary"
          size="small"
          :loading="shopLoading"
          @click="openShop"
        >
          立即开通
        </el-button>
      </div>
    </el-alert>

    <!-- 工具栏：左侧分段筛选状态，右侧主操作。动作永远在右手边。 -->
    <div class="toolbar">
      <el-radio-group v-model="statusFilter" size="small" @change="load">
        <el-radio-button v-for="tab in statusTabs" :key="String(tab.value)" :value="tab.value">
          {{ tab.label }}
        </el-radio-button>
      </el-radio-group>

      <div class="spacer" />

      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <el-button
        type="primary"
        size="small"
        :disabled="!auth.user?.shopId"
        @click="router.push('/products/new')"
      >
        发布商品
      </el-button>
    </div>

    <div class="panel">
      <el-empty v-if="!loading && items.length === 0" description="还没有商品" />

      <el-table v-else v-loading="loading" :data="items">
        <el-table-column label="商品" min-width="280">
          <template #default="{ row }">
            <div class="cell-product">
              <img
                :src="thumbSrc(row.mainImage, row.mainImageMid, row.title)"
                :data-fallback-src="thumbFallback(row.mainImage, row.mainImageMid)"
                class="thumb"
                :alt="row.title"
                @error="onImageError"
              />
              <div class="cell-text">
                <div class="cell-title">{{ row.title }}</div>
                <div class="cell-sub tnum">ID {{ row.id }}</div>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="价格区间" width="180">
          <template #default="{ row }">
            <span class="tnum price">{{ formatPriceRange(row.priceMin, row.priceMax) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="销量" width="90" align="right">
          <template #default="{ row }">
            <span class="tnum">{{ row.totalSold }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="SPU_STATUS_TYPE[row.status] ?? 'info'" size="small" disable-transitions>
              {{ row.statusText }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="250" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/products/${row.id}`)">编辑</el-button>
            <el-button v-if="row.status === 1" link type="primary" @click="act(row, 'submit')">
              提交审核
            </el-button>
            <el-button v-if="row.status === 3" link type="success" @click="act(row, 'on')">
              上架
            </el-button>
            <el-button v-if="row.status === 2" link type="warning" @click="act(row, 'off')">
              下架
            </el-button>
            <!-- 在售（2）要先下架、审核中（5）要等结果，后端也会挡，这里先不给点 -->
            <el-button
              v-if="row.status !== 2 && row.status !== 5"
              link
              type="danger"
              @click="remove(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.shop-alert {
  margin: 0;
}

.alert-body {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-top: var(--space-1);
}

/* --------------------------------------------------------------------------
 * 工具栏
 * ------------------------------------------------------------------------*/

.toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.spacer {
  flex: 1;
}

/* --------------------------------------------------------------------------
 * 表格容器
 *
 * 用底色的层次（页面 n-100 / 面板 n-0）来分区，而不是到处加边框和阴影，
 * 这样一屏里的"块"少、视觉噪声低，符合"极简高效"。
 * ------------------------------------------------------------------------*/

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

/* --------------------------------------------------------------------------
 * 商品单元格
 * ------------------------------------------------------------------------*/

.cell-product {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.thumb {
  width: 40px;
  height: 40px;
  flex: 0 0 auto;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-bg-inset);
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
  color: var(--color-text-placeholder);
}

.price {
  color: var(--color-price);
  font-weight: var(--weight-medium);
}
</style>
