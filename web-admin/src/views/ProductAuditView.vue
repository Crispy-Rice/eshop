<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import { fetchShops } from '@/api/auth'
import { isBizError } from '@/api/errors'
import {
  SPU_STATUS_TEXT,
  SPU_STATUS_TYPE,
  auditSpu,
  listAdminSpus,
  type SpuCard,
} from '@/api/product'
import { formatPriceRange } from '@/utils/money'
import { onImageError, thumbFallback, thumbSrc } from '@/utils/placeholder'

/**
 * 平台审核只列这三档。
 *
 * ★ 刻意不做「全部」：草稿（status=1）是商家自己的半成品，不是平台的审核对象 ——
 *   放进列表既没意义，也会让"待审核"这个真正要处理的队列被稀释。
 */
const statusTabs = [
  { label: '待审核', value: 5 },
  { label: '已上架', value: 2 },
  { label: '已下架', value: 3 },
]

const statusFilter = ref<number>(5)
const keyword = ref('')
const items = ref<SpuCard[]>([])
const loading = ref(false)
/** 正在处理的行 id，用来单独禁用那一行的按钮，避免连点审两次 */
const actingId = ref<string | null>(null)

/** shopId → 店铺名。审核队列要知道"这是谁提交的" */
const shopNames = ref<Record<string, string>>({})

/**
 * 解析这批商品所属的店铺名。
 *
 * ★ 一次批量请求，不要每行各发一个 —— 一页几十件往往只来自一两家店。
 *   店名是附加信息：拉不到只是那一列空着，不弹错、也不拖住列表。
 */
async function loadShopNames(list: SpuCard[]): Promise<void> {
  const missing = [...new Set(list.map((item) => item.shopId))].filter(
    (id) => shopNames.value[id] === undefined,
  )
  if (missing.length === 0) return
  try {
    for (const shop of await fetchShops(missing)) {
      shopNames.value[shop.id] = shop.name
    }
  } catch {
    // 静默降级
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listAdminSpus({
      status: statusFilter.value,
      keyword: keyword.value.trim() || undefined,
      limit: 60,
    })
    items.value = result.items
    void loadShopNames(result.items)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载商品失败')
  } finally {
    loading.value = false
  }
}

// ============================================================
// 通过 / 驳回
// ============================================================
async function approve(item: SpuCard): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定让「${item.title}」通过审核并上架吗？`, '通过审核', {
      type: 'warning',
    })
  } catch {
    return // 用户取消
  }

  actingId.value = item.id
  try {
    await auditSpu(item.id, true)
    ElMessage.success('已通过，商品已上架')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  } finally {
    actingId.value = null
  }
}

const rejectVisible = ref(false)
const rejectTarget = ref<SpuCard | null>(null)
const rejectFormRef = ref<FormInstance>()
const rejectForm = ref({ remark: '' })

/**
 * ★ 驳回理由**必填**。
 *
 * 理由会存进 `spu.audit_remark`，商家在自己的商品详情里看到它 ——
 * 这是商家唯一能知道"该改什么"的地方。留空等于让商品悄悄回到草稿，
 * 正是这次要修掉的那个毛病。
 */
const rejectRules: FormRules = {
  remark: [
    { required: true, message: '请填写驳回理由，商家要靠它知道改什么', trigger: 'blur' },
    { max: 255, message: '最多 255 个字符', trigger: 'blur' },
  ],
}

function openReject(item: SpuCard): void {
  rejectTarget.value = item
  rejectForm.value.remark = ''
  rejectVisible.value = true
  void nextTick(() => rejectFormRef.value?.clearValidate())
}

async function submitReject(): Promise<void> {
  const valid = await rejectFormRef.value?.validate().catch(() => false)
  if (!valid || rejectTarget.value === null) return

  actingId.value = rejectTarget.value.id
  try {
    await auditSpu(rejectTarget.value.id, false, rejectForm.value.remark.trim())
    ElMessage.success('已驳回，商品退回商家草稿箱')
    rejectVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  } finally {
    actingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">商品审核</h2>
      <span class="hint">商家提交审核的商品都在这里；驳回时写的理由会显示给商家</span>
    </div>

    <div class="toolbar">
      <el-radio-group v-model="statusFilter" size="small" @change="load">
        <el-radio-button v-for="tab in statusTabs" :key="tab.value" :value="tab.value">
          {{ tab.label }}
        </el-radio-button>
      </el-radio-group>

      <el-input
        v-model="keyword"
        class="f-keyword"
        size="small"
        placeholder="按商品标题搜索"
        clearable
        @keyup.enter="load"
      />

      <div class="spacer" />

      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </div>

    <div v-loading="loading" class="panel">
      <el-empty
        v-if="!loading && items.length === 0"
        :description="statusFilter === 5 ? '没有待审核的商品' : '没有符合条件的商品'"
      />

      <el-table v-else :data="items">
        <el-table-column label="商品" min-width="320">
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

        <el-table-column label="店铺" min-width="160">
          <template #default="{ row }">
            <span v-if="shopNames[row.shopId]">{{ shopNames[row.shopId] }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="价格区间" width="180">
          <template #default="{ row }">
            <span class="tnum price">{{ formatPriceRange(row.priceMin, row.priceMax) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="SPU_STATUS_TYPE[row.status]" size="small" effect="plain" disable-transitions>
              {{ SPU_STATUS_TEXT[row.status] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="150" align="right">
          <template #default="{ row }">
            <!-- 只有待审核的能审。已上架/已下架的没有可执行的动作 -->
            <template v-if="row.status === 5">
              <el-button
                link
                type="primary"
                size="small"
                :loading="actingId === row.id"
                @click="approve(row)"
              >
                通过
              </el-button>
              <el-button link type="danger" size="small" @click="openReject(row)">驳回</el-button>
            </template>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 驳回 -->
    <el-dialog v-model="rejectVisible" title="驳回商品" width="480px" :close-on-click-modal="false">
      <p class="dialog-note">「{{ rejectTarget?.title }}」将退回商家的草稿箱，理由会显示在商家的商品详情里。</p>
      <el-form ref="rejectFormRef" :model="rejectForm" :rules="rejectRules" label-position="top">
        <el-form-item label="驳回理由" prop="remark">
          <el-input
            v-model="rejectForm.remark"
            type="textarea"
            :rows="3"
            maxlength="255"
            show-word-limit
            placeholder="例如：主图太模糊，请换一张清晰的"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="rejectVisible = false">取消</el-button>
        <el-button type="primary" :loading="actingId !== null" @click="submitReject">确认驳回</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.toolbar {
  display: flex;
  align-items: center;
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

.f-keyword {
  width: 220px;
}

.spacer {
  flex: 1;
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.panel :deep(.el-empty) {
  padding: var(--space-8) 0;
}

.cell-product {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.thumb {
  width: 44px;
  height: 44px;
  border-radius: var(--radius-sm);
  object-fit: cover;
  background: var(--media-bg);
  flex: 0 0 auto;
}

.cell-title {
  font-size: var(--text-base);
  color: var(--color-text);
}

.cell-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.price {
  color: var(--color-price);
}

/* 没有可展示的值时用破折号，不要留白 —— 空白看起来像加载失败 */
.muted {
  color: var(--color-text-placeholder);
}

.dialog-note {
  margin: 0 0 var(--space-4);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}
</style>
