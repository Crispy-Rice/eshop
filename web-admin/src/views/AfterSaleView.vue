<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  MERCHANT_TABS,
  approveRefund,
  fetchMerchantRefund,
  fetchMerchantRefunds,
  receiveRefund,
  refundTagType,
  rejectRefund,
  submitQuality,
  type Refund,
  type RefundListItem,
} from '@/api/aftersale'
import { uploadImage } from '@/api/files'
import { isBizError } from '@/api/errors'
import { formatDateTime, formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const tab = ref(0)
const items = ref<RefundListItem[]>([])
const cursor = ref<string | null>(null)
const hasMore = ref(false)
const loading = ref(false)
const loadingMore = ref(false)
const acting = ref(false)

/**
 * 三个操作弹窗只用到这三个字段，列表项与详情对象都满足 —— 所以把类型收窄到这里，
 * 抽屉里就能直接拿 `detail` 调同一批操作，不用再拼一个假列表项。
 */
type ActionTarget = Pick<
  RefundListItem,
  'refundNo' | 'refundType' | 'refundTypeText' | 'totalRefund'
>

/** 三个弹窗：同意 / 拒绝 / 质检。共用 target */
const target = ref<ActionTarget | null>(null)
const approveVisible = ref(false)
const rejectVisible = ref(false)
const qualityVisible = ref(false)

const forms = reactive({
  approveRemark: '',
  rejectReason: '',
  passed: true,
  qualityRemark: '',
})

async function load(reset = true): Promise<void> {
  if (reset) {
    loading.value = true
    cursor.value = null
  } else {
    loadingMore.value = true
  }
  try {
    const current = MERCHANT_TABS[tab.value]
    const page = await fetchMerchantRefunds({
      status: current?.status,
      pendingOnly: current?.pendingOnly,
      cursor: reset ? undefined : (cursor.value ?? undefined),
      limit: 20,
    })
    items.value = reset ? page.items : [...items.value, ...page.items]
    cursor.value = page.nextCursor
    hasMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载售后列表失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchTab(index: number): Promise<void> {
  if (tab.value === index) return
  tab.value = index
  await load(true)
}

function openApprove(row: ActionTarget): void {
  target.value = row
  forms.approveRemark = ''
  approveVisible.value = true
}

function openReject(row: ActionTarget): void {
  target.value = row
  forms.rejectReason = ''
  rejectVisible.value = true
}

function openQuality(row: ActionTarget): void {
  target.value = row
  forms.passed = true
  forms.qualityRemark = ''
  qualityPaths.value = []
  qualityPreviews.value = []
  qualityVisible.value = true
}

async function submitApprove(): Promise<void> {
  if (!target.value) return
  acting.value = true
  try {
    await approveRefund(target.value.refundNo, forms.approveRemark || undefined)
    ElMessage.success('已同意售后')
    approveVisible.value = false
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  } finally {
    acting.value = false
  }
}

async function submitReject(): Promise<void> {
  if (!target.value) return
  if (forms.rejectReason.trim().length < 2) {
    ElMessage.warning('请填写拒绝理由，买家能看到')
    return
  }
  acting.value = true
  try {
    await rejectRefund(target.value.refundNo, forms.rejectReason.trim())
    ElMessage.success('已拒绝售后')
    rejectVisible.value = false
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  } finally {
    acting.value = false
  }
}

async function onReceive(row: ActionTarget): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '确认已收到买家寄回的商品？确认后需要提交质检结果。',
      '确认收货',
      { confirmButtonText: '确认收到', cancelButtonText: '还没收到' },
    )
  } catch {
    return
  }
  acting.value = true
  try {
    await receiveRefund(row.refundNo)
    ElMessage.success('已签收，请提交质检结果')
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  } finally {
    acting.value = false
  }
}

async function submitQualityForm(): Promise<void> {
  if (!target.value) return
  acting.value = true
  try {
    await submitQuality(target.value.refundNo, {
      passed: forms.passed,
      remark: forms.qualityRemark || undefined,
      images: qualityPaths.value.length ? qualityPaths.value : undefined,
    })
    ElMessage.success(forms.passed ? '质检合格，退款已发起' : '已记录质检不通过')
    qualityVisible.value = false
    await load(true)
    // 抽屉开着的话同步刷新，否则商家会看到过期的状态
    if (detailVisible.value) await reloadDetail()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  } finally {
    acting.value = false
  }
}

function deadlineText(row: RefundListItem): string {
  if (!row.deadline) return '—'
  return formatDateTime(row.deadline)
}

// ============================================================
// 详情抽屉
//
// 列表项是**精简对象**：没有凭证图、退货物流、质检结果、时间线。
// 点开时按 refundNo 拉详情 —— 这个接口一直存在，只是从没被前端调用过。
// ============================================================
const detailVisible = ref(false)
const detail = ref<Refund | null>(null)
const detailLoading = ref(false)

/** 凭证图入库存的是**相对路径**（aftersale/{uid}/x.webp），渲染要拼 /media/ */
function mediaUrl(path: string): string {
  if (path.startsWith('/') || path.startsWith('http') || path.startsWith('data:')) return path
  return `/media/${path}`
}

async function openDetail(row: RefundListItem): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await fetchMerchantRefund(row.refundNo)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载售后详情失败')
  } finally {
    detailLoading.value = false
  }
}

async function reloadDetail(): Promise<void> {
  if (!detail.value) return
  detail.value = await fetchMerchantRefund(detail.value.refundNo)
}

/** 时间线只放有值的节点，未走过的环节不显示空行 */
function timelineOf(r: Refund): { label: string; at: string }[] {
  const nodes: [string, string | null][] = [
    ['提交申请', r.applyTime],
    ['商家处理', r.merchantHandleTime],
    ['买家寄回', r.returnTime],
    ['商家签收', r.receiveTime],
    ['质检完成', r.qualityTime],
    ['退款成功', r.refundTime],
    ['售后关闭', r.closeTime],
  ]
  return nodes.filter((n): n is [string, string] => Boolean(n[1])).map(([label, at]) => ({ label, at }))
}

// ---------- 质检留证图 ----------
// 后端的 RefundQualityRequest.images 支持 ≤9 张，之前前端一个都没传
const qualityUploading = ref(false)
const qualityPaths = ref<string[]>([])
const qualityPreviews = ref<string[]>([])

async function onQualityPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 清掉才能重复选同一个文件
  if (!file) return
  if (qualityPreviews.value.length >= 9) {
    ElMessage.warning('最多 9 张')
    return
  }
  qualityUploading.value = true
  try {
    const img = await uploadImage(file, 'aftersale')
    qualityPaths.value.push(img.path) // 入库用相对 path，不是 url
    qualityPreviews.value.push(img.thumbUrl)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '上传失败')
  } finally {
    qualityUploading.value = false
  }
}

function removeQualityImage(index: number): void {
  qualityPaths.value.splice(index, 1)
  qualityPreviews.value.splice(index, 1)
}

onMounted(() => {
  void load(true)
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">退款/售后</h2>
      <span class="hint">只显示本店铺的售后单；库存回补发生在「质检合格」那一刻</span>
      <div class="spacer" />
      <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
    </div>

    <el-radio-group :model-value="tab" size="small" @change="switchTab($event as number)">
      <el-radio-button v-for="(t, i) in MERCHANT_TABS" :key="t.label" :value="i">
        {{ t.label }}
      </el-radio-button>
    </el-radio-group>

    <div class="panel">
      <el-empty v-if="!loading && items.length === 0" description="暂无售后单" />

      <el-table v-else v-loading="loading" :data="items">
        <el-table-column label="商品" min-width="240">
          <template #default="{ row }">
            <div class="goods">
              <img
                :src="row.previewImage"
                :alt="row.previewTitle"
                class="goods-img"
                @error="onImageError"
              />
              <div class="goods-text">
                <div class="goods-title">{{ row.previewTitle }}</div>
                <div class="goods-sub">共 {{ row.itemCount }} 件</div>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="售后单号" width="190">
          <template #default="{ row }">
            <div class="cell-mono tnum">{{ row.refundNo }}</div>
            <div class="cell-sub tnum">{{ row.orderSubNo }}</div>
          </template>
        </el-table-column>

        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <span class="cell-sub">{{ row.refundTypeText }}</span>
          </template>
        </el-table-column>

        <el-table-column label="退款金额" width="120" align="right">
          <template #default="{ row }">
            <div class="money tnum">¥{{ formatYuan(row.totalRefund) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="refundTagType(row.status)" size="small" effect="light">
              {{ row.statusText }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="申请时间" width="140">
          <template #default="{ row }">
            <span class="cell-sub">{{ formatDateTime(row.applyTime) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="处理时限" width="140">
          <template #default="{ row }">
            <span class="cell-sub">{{ deadlineText(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="230" align="right" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openDetail(row)">详情</el-button>
            <el-button
              v-if="row.canApprove"
              type="primary"
              size="small"
              @click="openApprove(row)"
            >
              同意
            </el-button>
            <el-button v-if="row.canApprove" size="small" @click="openReject(row)">拒绝</el-button>
            <el-button
              v-if="row.canReceive"
              type="primary"
              size="small"
              :loading="acting"
              @click="onReceive(row)"
            >
              确认收货
            </el-button>
            <el-button
              v-if="row.canQuality"
              type="primary"
              size="small"
              @click="openQuality(row)"
            >
              提交质检
            </el-button>
            <span v-if="!row.canApprove && !row.canReceive && !row.canQuality" class="cell-sub">
              等待中
            </span>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="hasMore" class="more">
        <el-button size="small" :loading="loadingMore" @click="load(false)">加载更多</el-button>
      </div>
    </div>

    <!-- 同意 -->
    <el-dialog v-model="approveVisible" title="同意售后" width="440px">
      <div v-if="target" class="dlg-info">
        <div class="dlg-line">
          <span class="dlg-label">售后单</span>
          <span class="tnum">{{ target.refundNo }}</span>
        </div>
        <div class="dlg-line">
          <span class="dlg-label">类型</span>
          <span>{{ target.refundTypeText }}</span>
        </div>
        <div class="dlg-line">
          <span class="dlg-label">退款</span>
          <span class="tnum">¥{{ formatYuan(target.totalRefund) }}</span>
        </div>
      </div>
      <el-alert
        v-if="target?.refundType === 2"
        type="info"
        :closable="false"
        show-icon
        title="同意后等待买家寄回；收货并质检合格才会退款"
      />
      <el-alert
        v-else
        type="warning"
        :closable="false"
        show-icon
        title="仅退款：同意后立即回补库存并发起退款"
      />
      <el-form label-width="76px" class="dlg-form">
        <el-form-item label="备注">
          <el-input v-model="forms.approveRemark" maxlength="255" placeholder="选填" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="approveVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitApprove">确认同意</el-button>
      </template>
    </el-dialog>

    <!-- 拒绝 -->
    <el-dialog v-model="rejectVisible" title="拒绝售后" width="440px">
      <div v-if="target" class="dlg-info">
        <div class="dlg-line">
          <span class="dlg-label">售后单</span>
          <span class="tnum">{{ target.refundNo }}</span>
        </div>
      </div>
      <el-form label-width="76px" class="dlg-form">
        <el-form-item label="拒绝理由">
          <el-input
            v-model="forms.rejectReason"
            type="textarea"
            :rows="3"
            maxlength="255"
            show-word-limit
            placeholder="买家能看到这条理由，请写清楚"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="rejectVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitReject">确认拒绝</el-button>
      </template>
    </el-dialog>

    <!-- 质检 -->
    <el-dialog v-model="qualityVisible" title="提交质检结果" width="460px">
      <div v-if="target" class="dlg-info">
        <div class="dlg-line">
          <span class="dlg-label">售后单</span>
          <span class="tnum">{{ target.refundNo }}</span>
        </div>
        <div class="dlg-line">
          <span class="dlg-label">退款</span>
          <span class="tnum">¥{{ formatYuan(target.totalRefund) }}</span>
        </div>
      </div>
      <el-form label-width="76px" class="dlg-form">
        <el-form-item label="质检结果">
          <el-radio-group v-model="forms.passed">
            <el-radio :value="true">合格</el-radio>
            <el-radio :value="false">不合格</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="质检备注">
          <el-input
            v-model="forms.qualityRemark"
            type="textarea"
            :rows="2"
            maxlength="255"
            placeholder="选填。不合格时建议写清原因"
          />
        </el-form-item>
        <el-form-item label="留证照片">
          <div class="upload">
            <div v-for="(url, i) in qualityPreviews" :key="url" class="upload-item">
              <img :src="url" alt="质检留证" @error="onImageError" />
              <span class="upload-del" @click="removeQualityImage(i)">×</span>
            </div>
            <label
              v-if="qualityPreviews.length < 9"
              class="upload-add"
              :class="{ disabled: qualityUploading }"
            >
              <input type="file" accept="image/*" hidden @change="onQualityPick" />
              <span v-if="qualityUploading">上传中…</span>
              <span v-else>+ 添加</span>
            </label>
          </div>
          <div class="form-hint">选填，最多 9 张，作为质检留证随售后单保存</div>
        </el-form-item>
      </el-form>
      <el-alert
        :type="forms.passed ? 'success' : 'warning'"
        :closable="false"
        show-icon
        :title="
          forms.passed
            ? '合格：商品回到可售库存，并立即发起退款'
            : '不合格：商品进残次品池（不可售），本次售后直接终结'
        "
      />
      <template #footer>
        <el-button @click="qualityVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitQualityForm">提交</el-button>
      </template>
    </el-dialog>

    <!-- ============ 售后详情 ============ -->
    <el-drawer
      v-model="detailVisible"
      :title="`售后详情 · ${detail?.refundNo ?? ''}`"
      size="680px"
    >
      <div v-loading="detailLoading" class="detail">
        <template v-if="detail">
          <div class="dt-head">
            <el-tag :type="refundTagType(detail.status)" size="small" effect="light">
              {{ detail.statusText }}
            </el-tag>
            <span class="dt-sub">
              {{ detail.refundTypeText }} · {{ detail.reasonTypeText }}
            </span>
            <div class="spacer" />
            <span v-if="detail.deadline" class="dt-sub tnum">
              当前环节截止 {{ formatDateTime(detail.deadline) }}
            </span>
          </div>

          <!-- 操作直接复用列表那三个弹窗；can* 由服务端算，前端不重复判状态 -->
          <div
            v-if="detail.canApprove || detail.canReceive || detail.canQuality"
            class="dt-actions"
          >
            <el-button
              v-if="detail.canApprove"
              type="primary"
              size="small"
              @click="openApprove(detail)"
            >
              同意售后
            </el-button>
            <el-button v-if="detail.canApprove" size="small" @click="openReject(detail)">
              拒绝
            </el-button>
            <el-button
              v-if="detail.canReceive"
              type="primary"
              size="small"
              :loading="acting"
              @click="onReceive(detail)"
            >
              确认收货
            </el-button>
            <el-button
              v-if="detail.canQuality"
              type="primary"
              size="small"
              @click="openQuality(detail)"
            >
              提交质检
            </el-button>
          </div>

          <!-- 金额拆分 -->
          <div class="dt-block">
            <h4 class="dt-h">退款金额</h4>
            <div class="dt-rows">
              <div class="dt-row">
                <span>商品退款</span><span class="tnum">¥{{ formatYuan(detail.refundAmount) }}</span>
              </div>
              <div class="dt-row">
                <span>退还运费</span><span class="tnum">¥{{ formatYuan(detail.refundFreight) }}</span>
              </div>
              <div class="dt-row total">
                <span>合计</span>
                <span class="tnum">¥{{ formatYuan(detail.totalRefund) }}</span>
              </div>
              <div class="dt-row">
                <span>退货运费承担</span><span>{{ detail.freightBearerText }}</span>
              </div>
            </div>
          </div>

          <!-- 商品明细 -->
          <div class="dt-block">
            <h4 class="dt-h">商品明细</h4>
            <el-table :data="detail.items" size="small">
              <el-table-column label="商品" min-width="240">
                <template #default="{ row }">
                  <div class="goods">
                    <img
                      :src="row.coverImage"
                      :alt="row.title"
                      class="goods-img"
                      @error="onImageError"
                    />
                    <div class="goods-text">
                      <div class="goods-title">{{ row.title }}</div>
                      <div class="goods-sub">{{ row.specText }}</div>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="退款件数" width="90" align="right">
                <template #default="{ row }">
                  <span class="tnum">{{ row.refundNum }}</span>
                </template>
              </el-table-column>
              <el-table-column label="退款额" width="100" align="right">
                <template #default="{ row }">
                  <span class="money tnum">¥{{ formatYuan(row.refundAmount) }}</span>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 申请信息 -->
          <div class="dt-block">
            <h4 class="dt-h">申请信息</h4>
            <div class="dt-rows">
              <div class="dt-row">
                <span>申请说明</span><span>{{ detail.reasonDesc || '—' }}</span>
              </div>
              <div v-if="detail.merchantRemark" class="dt-row">
                <span>商家备注</span><span>{{ detail.merchantRemark }}</span>
              </div>
              <div v-if="detail.rejectReason" class="dt-row">
                <span>拒绝理由</span><span>{{ detail.rejectReason }}</span>
              </div>
            </div>
            <div v-if="detail.images.length" class="dt-thumbs">
              <img
                v-for="img in detail.images"
                :key="img"
                :src="mediaUrl(img)"
                alt="买家凭证"
                @error="onImageError"
              />
            </div>
          </div>

          <!-- 退货物流 -->
          <div v-if="detail.returnExpress || detail.returnExpressNo" class="dt-block">
            <h4 class="dt-h">退货物流</h4>
            <div class="dt-rows">
              <div class="dt-row">
                <span>快递公司</span><span>{{ detail.returnExpress || '—' }}</span>
              </div>
              <div class="dt-row">
                <span>运单号</span><span class="tnum">{{ detail.returnExpressNo || '—' }}</span>
              </div>
            </div>
          </div>

          <!-- 质检结果 -->
          <div v-if="detail.qualityResult" class="dt-block">
            <h4 class="dt-h">质检结果</h4>
            <div class="dt-rows">
              <div class="dt-row">
                <span>结论</span>
                <span>
                  <el-tag
                    :type="detail.qualityResult === 1 ? 'success' : 'danger'"
                    size="small"
                    effect="light"
                  >
                    {{ detail.qualityResultText }}
                  </el-tag>
                </span>
              </div>
              <div class="dt-row">
                <span>质检备注</span><span>{{ detail.qualityRemark || '—' }}</span>
              </div>
            </div>
            <div v-if="detail.qualityImages.length" class="dt-thumbs">
              <img
                v-for="img in detail.qualityImages"
                :key="img"
                :src="mediaUrl(img)"
                alt="质检留证"
                @error="onImageError"
              />
            </div>
          </div>

          <!-- 时间线 -->
          <div class="dt-block">
            <h4 class="dt-h">进度</h4>
            <div class="dt-rows">
              <div v-for="node in timelineOf(detail)" :key="node.label" class="dt-row">
                <span>{{ node.label }}</span>
                <span class="tnum">{{ formatDateTime(node.at) }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* 只用语义 token，见 docs/17-frontend-design-system.md */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.hint {
  font-size: var(--text-xs);
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
}

/* ---------- 表格 ---------- */

.goods {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.goods-img {
  width: 40px;
  height: 40px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--media-bg);
  flex: 0 0 auto;
}

.goods-text {
  min-width: 0;
}

.goods-title {
  font-size: var(--text-sm);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.goods-sub,
.cell-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.cell-mono {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.money {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

.more {
  display: flex;
  justify-content: center;
  padding: var(--space-3);
}

/* ---------- 弹窗 ---------- */

.dlg-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.dlg-line {
  display: flex;
  gap: var(--space-3);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.dlg-label {
  flex: 0 0 56px;
  color: var(--color-text-tertiary);
}

.dlg-form {
  margin-top: var(--space-4);
}

/* ---------- 详情抽屉 ---------- */

.detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  min-height: 120px;
}

.dt-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.dt-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.dt-actions {
  display: flex;
  gap: var(--space-2);
}

.dt-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.dt-h {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.dt-rows {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.dt-row {
  display: flex;
  justify-content: space-between;
  gap: var(--space-4);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.dt-row.total {
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-border);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.dt-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.dt-thumbs img {
  width: 72px;
  height: 72px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--media-bg);
  cursor: zoom-in;
}

/* ---------- 质检留证上传 ---------- */

.upload {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.upload-item {
  position: relative;
  width: 64px;
  height: 64px;
}

.upload-item img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--media-bg);
}

.upload-del {
  position: absolute;
  top: -6px;
  right: -6px;
  width: 18px;
  height: 18px;
  line-height: 16px;
  text-align: center;
  border-radius: var(--radius-pill);
  background: var(--color-danger);
  color: var(--color-text-inverse);
  font-size: var(--text-xs);
  cursor: pointer;
}

.upload-add {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 64px;
  height: 64px;
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  cursor: pointer;
}

.upload-add:hover {
  border-color: var(--color-accent);
  color: var(--color-accent);
}

.upload-add.disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.form-hint {
  width: 100%;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
  line-height: var(--leading-snug);
}
</style>
