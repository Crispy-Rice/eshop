<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { isBizError } from '@/api/errors'
import { fetchSpu, type SkuDetail, type SpuDetail } from '@/api/product'
import { formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const route = useRoute()
const router = useRouter()

const spu = ref<SpuDetail | null>(null)
const loading = ref(false)
const notFound = ref(false)
const quantity = ref(1)
/** groupId → valueId */
const selected = ref<Record<string, string>>({})

const spuId = computed(() => String(route.params.spuId))

/**
 * 某个规格值当前是否可选。
 *
 * 判断依据：是否存在一个 SKU，它包含这个值，**并且**包含其它规格组已选中的值。
 * 只看"其它组"是刻意的——同一组里两个值不可能同时出现在一个 SKU 上。
 */
function isValueAvailable(groupId: string, valueId: string): boolean {
  const skus = spu.value?.skus ?? []
  return skus.some((sku) => {
    if (!sku.specValueIds.includes(valueId)) return false
    return Object.entries(selected.value).every(
      ([gid, vid]) => gid === groupId || sku.specValueIds.includes(vid),
    )
  })
}

/** 选满所有规格组后命中的 SKU */
const currentSku = computed<SkuDetail | null>(() => {
  const groups = spu.value?.specGroups ?? []
  // filter(Boolean) 不会收窄类型，用类型谓词让 TS 知道这里已经没有 undefined
  const chosen = groups
    .map((g) => selected.value[g.id])
    .filter((v): v is string => Boolean(v))
  if (chosen.length !== groups.length || groups.length === 0) return null
  return spu.value?.skus.find((sku) => chosen.every((v) => sku.specValueIds.includes(v))) ?? null
})

const allSelected = computed(() => {
  const groups = spu.value?.specGroups ?? []
  return groups.length > 0 && groups.every((g) => selected.value[g.id])
})

/**
 * 选完之后如果某个规格组只剩一个可选值，自动补上。
 * 比如只有"黑色 256G"可选时，用户点一个颜色就应该直接把容量也带上。
 */
function autoSelectSingleOptions(): void {
  let changed = true
  let guard = 0
  while (changed && guard++ < 10) {
    changed = false
    for (const group of spu.value?.specGroups ?? []) {
      if (selected.value[group.id]) continue
      const available = group.values.filter((v) => isValueAvailable(group.id, v.id))
      const only = available[0]
      if (available.length === 1 && only) {
        selected.value[group.id] = only.id
        changed = true
      }
    }
  }
}

function pickValue(groupId: string, valueId: string): void {
  if (!isValueAvailable(groupId, valueId)) return

  if (selected.value[groupId] === valueId) {
    delete selected.value[groupId]
  } else {
    selected.value[groupId] = valueId
    autoSelectSingleOptions()
  }
}

const displayPrice = computed(() => {
  if (currentSku.value) return `¥${formatYuan(currentSku.value.price)}`
  if (!spu.value) return ''
  return spu.value.priceMin === spu.value.priceMax
    ? `¥${formatYuan(spu.value.priceMin)}`
    : `¥${formatYuan(spu.value.priceMin)} ~ ¥${formatYuan(spu.value.priceMax)}`
})

const mainImage = computed(() => currentSku.value?.coverImage || spu.value?.mainImage || '')

function onAddToCart(): void {
  if (!currentSku.value) {
    ElMessage.warning('请先选择完整规格')
    return
  }
  // 购物车模块（cart）还没实现，这里先如实提示，不做假的成功反馈
  ElMessage.info('购物车功能还在开发中，当前可以先浏览商品与规格选择')
}

async function load(): Promise<void> {
  loading.value = true
  notFound.value = false
  selected.value = {}
  quantity.value = 1
  try {
    spu.value = await fetchSpu(spuId.value)
    autoSelectSingleOptions()
  } catch (e) {
    spu.value = null
    if (isBizError(e) && e.code === 'NOT_FOUND') {
      notFound.value = true
    } else {
      ElMessage.error(isBizError(e) ? e.message : '加载失败')
    }
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(spuId, load)
</script>

<template>
  <div v-loading="loading">
    <el-result
      v-if="notFound"
      icon="warning"
      title="商品不存在或已下架"
      sub-title="它可能已经被下架，或者链接不对"
    >
      <template #extra>
        <el-button type="primary" @click="router.push({ name: 'home' })">回到商品列表</el-button>
      </template>
    </el-result>

    <template v-else-if="spu">
      <el-breadcrumb class="crumb">
        <el-breadcrumb-item :to="{ name: 'home' }">全部商品</el-breadcrumb-item>
        <el-breadcrumb-item>{{ spu.title }}</el-breadcrumb-item>
      </el-breadcrumb>

      <el-card shadow="never">
        <div class="detail">
          <div class="gallery">
            <img :src="mainImage" :alt="spu.title" class="main-image" @error="onImageError" />
          </div>

          <div class="info">
            <h1 class="title">{{ spu.title }}</h1>
            <p v-if="spu.subTitle" class="subtitle">{{ spu.subTitle }}</p>

            <div class="price-box">
              <span class="price">{{ displayPrice }}</span>
              <span class="sold">已售 {{ spu.totalSold }}</span>
            </div>

            <div v-for="group in spu.specGroups" :key="group.id" class="spec-group">
              <div class="spec-label">{{ group.name }}</div>
              <div class="spec-values">
                <button
                  v-for="value in group.values"
                  :key="value.id"
                  type="button"
                  class="spec-value"
                  :class="{
                    active: selected[group.id] === value.id,
                    disabled: !isValueAvailable(group.id, value.id),
                  }"
                  :disabled="!isValueAvailable(group.id, value.id)"
                  :title="isValueAvailable(group.id, value.id) ? value.value : '该规格组合暂不可选'"
                  @click="pickValue(group.id, value.id)"
                >
                  <img v-if="value.image" :src="value.image" :alt="value.value" class="swatch" />
                  {{ value.value }}
                </button>
              </div>
            </div>

            <div class="spec-group">
              <div class="spec-label">数量</div>
              <el-input-number v-model="quantity" :min="1" :max="200" />
            </div>

            <div class="sku-line">
              <template v-if="currentSku">
                已选：{{ currentSku.specText }}（{{ currentSku.skuCode }}）
              </template>
              <template v-else-if="allSelected">该规格组合暂不可售，请换一个组合</template>
              <template v-else>请选择 {{ spu.specGroups.map((g) => g.name).join(' / ') }}</template>
            </div>

            <el-button type="primary" size="large" class="buy" @click="onAddToCart">
              加入购物车
            </el-button>
          </div>
        </div>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.crumb {
  margin-bottom: 16px;
}

.detail {
  display: flex;
  gap: 32px;
  flex-wrap: wrap;
}

.gallery {
  flex: 0 0 360px;
  max-width: 100%;
}

.main-image {
  width: 360px;
  max-width: 100%;
  height: 360px;
  object-fit: cover;
  border-radius: 4px;
  background: #f5f7fa;
  display: block;
}

.info {
  flex: 1;
  min-width: 320px;
}

.title {
  margin: 0;
  font-size: 22px;
  line-height: 1.4;
}

.subtitle {
  margin: 8px 0 0;
  color: #909399;
  font-size: 14px;
}

.price-box {
  margin: 16px 0;
  padding: 12px 16px;
  background: #fef0f0;
  border-radius: 4px;
  display: flex;
  align-items: baseline;
  gap: 16px;
}

.price {
  color: #d93025;
  font-size: 26px;
  font-weight: 600;
}

.sold {
  color: #909399;
  font-size: 13px;
}

.spec-group {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 16px;
}

.spec-label {
  flex: 0 0 56px;
  color: #909399;
  font-size: 14px;
  line-height: 32px;
}

.spec-values {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.spec-value {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  font-size: 14px;
  color: #303133;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;
}

.spec-value:hover:not(.disabled) {
  border-color: #d93025;
  color: #d93025;
}

.spec-value.active {
  border-color: #d93025;
  color: #d93025;
  background: #fef0f0;
  font-weight: 500;
}

.spec-value.disabled {
  color: #c0c4cc;
  background: #f5f7fa;
  border-style: dashed;
  cursor: not-allowed;
  text-decoration: line-through;
}

.swatch {
  width: 18px;
  height: 18px;
  object-fit: cover;
  border-radius: 2px;
}

.sku-line {
  margin: 20px 0 12px;
  font-size: 13px;
  color: #909399;
  min-height: 20px;
}

.buy {
  width: 200px;
}
</style>
