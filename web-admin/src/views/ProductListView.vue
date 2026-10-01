<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { isBizError } from '@/api/errors'
import { post } from '@/api/http'
import {
  listMySpus,
  offShelf,
  onShelf,
  submitForAudit,
  SPU_STATUS_TEXT,
  SPU_STATUS_TYPE,
  type SpuCard,
} from '@/api/product'
import { useAuthStore } from '@/stores/auth'
import { formatPriceRange } from '@/utils/money'
import { placeholderImage } from '@/utils/placeholder'

const auth = useAuthStore()
const router = useRouter()

const items = ref<SpuCard[]>([])
const loading = ref(false)
const statusFilter = ref<number | undefined>(undefined)
const shopLoading = ref(false)

const statusTabs = [
  { label: '全部', value: undefined },
  { label: '草稿', value: 1 },
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

onMounted(() => {
  void auth.restore().then(load)
})
</script>

<template>
  <div>
    <el-alert
      v-if="auth.user && !auth.user.shopId"
      type="info"
      :closable="false"
      class="shop-alert"
      title="你还没有店铺"
      description="上架商品前需要先开通店铺。"
    >
      <template #default>
        <div class="alert-body">
          <span>上架商品前需要先开通店铺。</span>
          <el-button type="primary" size="small" :loading="shopLoading" @click="openShop">
            立即开通
          </el-button>
        </div>
      </template>
    </el-alert>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <el-radio-group v-model="statusFilter" @change="load">
            <el-radio-button v-for="tab in statusTabs" :key="String(tab.value)" :value="tab.value">
              {{ tab.label }}
            </el-radio-button>
          </el-radio-group>
          <div class="actions">
            <el-button :loading="loading" @click="load">刷新</el-button>
            <el-button type="primary" :disabled="!auth.user?.shopId" @click="router.push('/products/new')">
              发布商品
            </el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="!loading && items.length === 0" description="还没有商品" />

      <el-table v-else v-loading="loading" :data="items">
        <el-table-column label="商品" min-width="280">
          <template #default="{ row }">
            <div class="cell-product">
              <img :src="row.mainImage || placeholderImage(row.title)" class="thumb" :alt="row.title" />
              <div>
                <div class="title">{{ row.title }}</div>
                <div class="sub">ID {{ row.id }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="价格区间" width="180">
          <template #default="{ row }">{{ formatPriceRange(row.priceMin, row.priceMax) }}</template>
        </el-table-column>
        <el-table-column label="销量" width="90">
          <template #default="{ row }">{{ row.totalSold }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="SPU_STATUS_TYPE[row.status] ?? 'info'" size="small">
              {{ SPU_STATUS_TEXT[row.status] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/products/${row.id}`)">编辑</el-button>
            <el-button v-if="row.status === 1" link type="primary" @click="act(row, 'submit')">
              提交审核
            </el-button>
            <el-button v-if="row.status === 3" link type="success" @click="act(row, 'on')">上架</el-button>
            <el-button v-if="row.status === 2" link type="warning" @click="act(row, 'off')">下架</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.shop-alert {
  margin-bottom: 16px;
}

.alert-body {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 4px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.actions {
  display: flex;
  gap: 8px;
}

.cell-product {
  display: flex;
  align-items: center;
  gap: 12px;
}

.thumb {
  width: 48px;
  height: 48px;
  object-fit: cover;
  border-radius: 4px;
  background: #f5f7fa;
  flex: 0 0 auto;
}

.title {
  font-size: 14px;
  line-height: 1.4;
}

.sub {
  font-size: 12px;
  color: #a8abb2;
  margin-top: 2px;
}
</style>
