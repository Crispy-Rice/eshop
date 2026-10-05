<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { fetchCategoryTree, type Category } from '@/api/category'
import { isBizError } from '@/api/errors'
import {
  CALC_DIRECT,
  CALC_FIXED,
  CALC_RATE,
  CALC_TYPE_TEXT,
  COUPON_TYPE_DISCOUNT,
  COUPON_TYPE_EXCHANGE,
  COUPON_TYPE_FREIGHT,
  COUPON_TYPE_NO_THRESHOLD,
  COUPON_TYPE_TEXT,
  COUPON_TYPE_THRESHOLD,
  LEVEL_ITEM,
  LEVEL_PLATFORM,
  LEVEL_SHOP,
  LEVEL_TEXT,
  PROMO_TYPE_BY_LEVEL,
  PROMO_TYPE_TEXT,
  SCOPE_ALL,
  SCOPE_CATEGORY,
  SCOPE_SKU,
  SCOPE_SHOP,
  SCOPE_TYPE_TEXT,
  TPL_ONGOING,
  VALID_DAYS_AFTER,
  VALID_FIXED,
  createCouponTemplate,
  createPromoActivity,
  issueCoupon,
  listCouponTemplates,
  listPromoActivities,
  lookupUserByPhone,
  type AdminCouponTemplate,
  type AdminPromoActivity,
  type UserLookup,
} from '@/api/promotion'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime, formatYuan, yuanToFen } from '@/utils/money'

const auth = useAuthStore()

/** 营销台是运营专属：后端接口只放给 admin / finance，商家看不到这个页面 */
const isAdmin = computed(() => auth.user?.role === 'admin' || auth.user?.role === 'finance')

const tab = ref<'coupon' | 'activity'>('coupon')

// ---------- 券模板列表 ----------
const coupons = ref<AdminCouponTemplate[]>([])
const couponCursor = ref<string | null>(null)
const couponMore = ref(false)
const couponStatus = ref<number | undefined>(undefined)

// ---------- 活动列表 ----------
const activities = ref<AdminPromoActivity[]>([])
const activityCursor = ref<string | null>(null)
const activityMore = ref(false)
const activityStatus = ref<number | undefined>(undefined)

const loading = ref(false)
const loadingMore = ref(false)
const acting = ref(false)
const categories = ref<Category[]>([])

const STATUS_TABS = [
  { label: '全部', value: undefined },
  { label: '未开始', value: 1 },
  { label: '进行中', value: TPL_ONGOING },
  { label: '已结束', value: 3 },
] as const

const COUPON_TYPES = [
  COUPON_TYPE_THRESHOLD,
  COUPON_TYPE_DISCOUNT,
  COUPON_TYPE_NO_THRESHOLD,
  COUPON_TYPE_EXCHANGE,
  COUPON_TYPE_FREIGHT,
]

const LEVELS = [LEVEL_ITEM, LEVEL_SHOP, LEVEL_PLATFORM]
const CALC_TYPES = [CALC_DIRECT, CALC_RATE, CALC_FIXED]

// ============================================================
// 券模板表单
//
// 字段是**类型条件相关**的，不是平铺的一张表：满减要门槛、折扣要折数与封顶、
// 运费券两者都不要。所以按 type 分开存，提交时再按 type 合成 discountValue。
// ============================================================
const couponVisible = ref(false)
const couponForm = reactive({
  name: '',
  type: COUPON_TYPE_THRESHOLD as number,
  thresholdYuan: 0,
  discountYuan: 0,
  /** 折扣券专用：按「折」填，8.5 折 → 提交时换算成 8500 */
  rate: 8.5,
  maxDiscountYuan: 0,
  totalCount: 100,
  perUserLimit: 1,
  validType: VALID_FIXED as number,
  validRange: [] as string[],
  validDays: 7,
  scopeType: SCOPE_ALL as number,
  categoryIds: [] as string[],
  /** 指定商品 / 指定店铺：运营端没有"列出全部 SPU/店铺"的接口，只能手填 ID */
  manualScopeIds: '',
})

const isDiscount = computed(() => couponForm.type === COUPON_TYPE_DISCOUNT)
const isExchange = computed(() => couponForm.type === COUPON_TYPE_EXCHANGE)
/** 满减券才有使用门槛；别的类型填了也没意义 */
const needsThreshold = computed(() => couponForm.type === COUPON_TYPE_THRESHOLD)
/** 折扣券才有封顶 */
const needsCap = computed(() => couponForm.type === COUPON_TYPE_DISCOUNT)

function openCouponDialog(): void {
  couponForm.name = ''
  couponForm.type = COUPON_TYPE_THRESHOLD
  couponForm.thresholdYuan = 200
  couponForm.discountYuan = 30
  couponForm.rate = 8.5
  couponForm.maxDiscountYuan = 0
  couponForm.totalCount = 100
  couponForm.perUserLimit = 1
  couponForm.validType = VALID_FIXED
  couponForm.validRange = []
  couponForm.validDays = 7
  couponForm.scopeType = SCOPE_ALL
  couponForm.categoryIds = []
  couponForm.manualScopeIds = ''
  couponVisible.value = true
}

async function submitCoupon(): Promise<void> {
  if (couponForm.name.trim().length < 2) {
    ElMessage.warning('券名称至少 2 个字')
    return
  }
  if (couponForm.validType === VALID_FIXED && couponForm.validRange.length !== 2) {
    ElMessage.warning('固定有效期要选开始与结束时间')
    return
  }
  if (isExchange.value) {
    ElMessage.warning('兑换券走券码核销，这里只建模板即可')
    return
  }

  // 折扣券：8.5 折 → 8500（万分比）。别的类型都是金额（分）
  const discountValue = isDiscount.value
    ? Math.round(couponForm.rate * 1000)
    : yuanToFen(couponForm.discountYuan)

  if (discountValue < 1) {
    ElMessage.warning('优惠值必须大于 0')
    return
  }

  acting.value = true
  try {
    await createCouponTemplate({
      name: couponForm.name.trim(),
      type: couponForm.type,
      discountValue,
      maxDiscount: needsCap.value ? yuanToFen(couponForm.maxDiscountYuan) : 0,
      threshold: needsThreshold.value ? yuanToFen(couponForm.thresholdYuan) : 0,
      totalCount: couponForm.totalCount,
      perUserLimit: couponForm.perUserLimit,
      validType: couponForm.validType,
      validStart: couponForm.validType === VALID_FIXED ? couponForm.validRange[0] : null,
      validEnd: couponForm.validType === VALID_FIXED ? couponForm.validRange[1] : null,
      validDays: couponForm.validType === VALID_DAYS_AFTER ? couponForm.validDays : null,
      scopeType: couponForm.scopeType,
      scopeValue: scopeValuePayload(),
    })
    ElMessage.success('券模板已创建')
    couponVisible.value = false
    await loadCoupons(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '创建失败')
  } finally {
    acting.value = false
  }
}

function scopeValuePayload(): number[] | null {
  if (couponForm.scopeType === SCOPE_ALL) return null
  if (couponForm.scopeType === SCOPE_CATEGORY) {
    return couponForm.categoryIds.map((v) => Number(v)).filter((v) => Number.isFinite(v))
  }
  return couponForm.manualScopeIds
    .split(/[,，\s]+/)
    .map((v) => Number(v.trim()))
    .filter((v) => Number.isFinite(v) && v > 0)
}

// ============================================================
// 活动表单
// ============================================================
const activityVisible = ref(false)
const activityForm = reactive({
  name: '',
  level: LEVEL_SHOP as number,
  calcType: CALC_DIRECT as number,
  valueYuan: 10,
  rate: 9,
  maxDiscountYuan: 0,
  thresholdYuan: 0,
  range: [] as string[],
  priority: 0,
})

/** 层级决定参与哪一层算价，也决定 type 这个字符串键 */
const activityType = computed(() => PROMO_TYPE_BY_LEVEL[activityForm.level] ?? 'PROMO_ITEM')
const activityIsDiscount = computed(() => activityForm.calcType === CALC_RATE)

function openActivityDialog(): void {
  activityForm.name = ''
  activityForm.level = LEVEL_SHOP
  activityForm.calcType = CALC_DIRECT
  activityForm.valueYuan = 10
  activityForm.rate = 9
  activityForm.maxDiscountYuan = 0
  activityForm.thresholdYuan = 0
  activityForm.range = []
  activityForm.priority = 0
  activityVisible.value = true
}

async function submitActivity(): Promise<void> {
  if (activityForm.name.trim().length < 2) {
    ElMessage.warning('活动名称至少 2 个字')
    return
  }
  // 解构出来再判空：tsconfig 开了 noUncheckedIndexedAccess，直接取下标拿到的是
  // string | undefined，不能直接赋给必填的 startAt / endAt
  const [startAt, endAt] = activityForm.range
  if (!startAt || !endAt) {
    ElMessage.warning('要选活动开始与结束时间')
    return
  }

  const discountValue = activityIsDiscount.value
    ? Math.round(activityForm.rate * 1000)
    : yuanToFen(activityForm.valueYuan)

  acting.value = true
  try {
    await createPromoActivity({
      name: activityForm.name.trim(),
      level: activityForm.level,
      type: activityType.value,
      calcType: activityForm.calcType,
      discountValue,
      maxDiscount: activityIsDiscount.value ? yuanToFen(activityForm.maxDiscountYuan) : 0,
      threshold: yuanToFen(activityForm.thresholdYuan),
      startAt,
      endAt,
      priority: activityForm.priority,
    })
    ElMessage.success('活动已创建')
    activityVisible.value = false
    await loadActivities(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '创建失败')
  } finally {
    acting.value = false
  }
}

// ============================================================
// 定向发券
//
// ★ 入口是**手机号**，不是 userId。运营拿不到用户的雪花 ID ——
//   原来那个"填 18 位数字"的输入框，实际没人填得出来。用户报手机号才是
//   真实场景，所以流程改成：先按手机号查人 → 确认没找错 → 再发券。
//   发券是**给钱**的动作，多这一步值得。
// ============================================================
const issueVisible = ref(false)
const issueTarget = ref<AdminCouponTemplate | null>(null)
const issueForm = reactive({ phone: '', count: 1 })
/** 手机号查到的收件人。为空就发不出去 */
const issueUser = ref<UserLookup | null>(null)
const looking = ref(false)

function openIssue(row: AdminCouponTemplate): void {
  issueTarget.value = row
  issueForm.phone = ''
  issueForm.count = 1
  issueUser.value = null
  issueVisible.value = true
}

// 手机号一改，之前查到的那个人就作废 —— 否则可能把券发给上一个查到的人
watch(
  () => issueForm.phone,
  () => {
    issueUser.value = null
  },
)

async function lookupIssueUser(): Promise<void> {
  const phone = issueForm.phone.trim()
  if (!/^1\d{10}$/.test(phone)) {
    ElMessage.warning('请填 11 位手机号')
    return
  }
  looking.value = true
  try {
    issueUser.value = await lookupUserByPhone(phone)
  } catch (e) {
    issueUser.value = null
    ElMessage.error(isBizError(e) ? e.message : '没查到这个手机号')
  } finally {
    looking.value = false
  }
}

async function submitIssue(): Promise<void> {
  if (!issueTarget.value) return
  if (!issueUser.value) {
    ElMessage.warning('请先点「查询」确认发给谁')
    return
  }
  acting.value = true
  try {
    const codes = await issueCoupon({
      templateId: issueTarget.value.id,
      userId: issueUser.value.userId,
      count: issueForm.count,
    })
    ElMessage.success(`已发 ${codes.length} 张`)
    issueVisible.value = false
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '发券失败')
  } finally {
    acting.value = false
  }
}

// ============================================================
// 加载
// ============================================================
async function loadCoupons(reset = true): Promise<void> {
  if (reset) loading.value = true
  else loadingMore.value = true
  try {
    const page = await listCouponTemplates({
      status: couponStatus.value,
      cursor: reset ? undefined : (couponCursor.value ?? undefined),
      limit: 20,
    })
    coupons.value = reset ? page.items : [...coupons.value, ...page.items]
    couponCursor.value = page.nextCursor
    couponMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载券模板失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function loadActivities(reset = true): Promise<void> {
  if (reset) loading.value = true
  else loadingMore.value = true
  try {
    const page = await listPromoActivities({
      status: activityStatus.value,
      cursor: reset ? undefined : (activityCursor.value ?? undefined),
      limit: 20,
    })
    activities.value = reset ? page.items : [...activities.value, ...page.items]
    activityCursor.value = page.nextCursor
    activityMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载活动失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchTab(next: 'coupon' | 'activity'): Promise<void> {
  if (tab.value === next) return
  tab.value = next
  if (next === 'coupon') await loadCoupons(true)
  else await loadActivities(true)
}

async function switchStatus(value: number | undefined): Promise<void> {
  if (tab.value === 'coupon') {
    if (couponStatus.value === value) return
    couponStatus.value = value
    await loadCoupons(true)
  } else {
    if (activityStatus.value === value) return
    activityStatus.value = value
    await loadActivities(true)
  }
}

/** 面额要按券类型解释：折扣券是折扣率，别的一律是分 */
function couponValueText(row: AdminCouponTemplate): string {
  if (row.type === COUPON_TYPE_DISCOUNT) {
    return `${(row.discountValue / 1000).toFixed(1)} 折`
  }
  return `¥${formatYuan(row.discountValue)}`
}

function activityValueText(row: AdminPromoActivity): string {
  if (row.calcType === CALC_RATE) return `${(row.discountValue / 1000).toFixed(1)} 折`
  if (row.calcType === CALC_FIXED) return `特价 ¥${formatYuan(row.discountValue)}`
  return `减 ¥${formatYuan(row.discountValue)}`
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  try {
    categories.value = await fetchCategoryTree()
  } catch {
    // 类目只是"指定类目"那种券的可选项，拉不到不 blocking
  }
  await loadCoupons(true)
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">营销中心</h2>
      <span class="hint">
        券与活动建出来后立即生效；下单时会按层级依次计算，具体叠加规则由平台配置
      </span>
      <div class="spacer" />
      <el-button
        size="small"
        :loading="loading"
        @click="tab === 'coupon' ? loadCoupons(true) : loadActivities(true)"
      >
        刷新
      </el-button>
    </div>

    <el-empty v-if="!isAdmin" description="营销中心仅对平台运营开放" />

    <template v-else>
      <div class="tabs">
        <el-radio-group :model-value="tab" size="small" @change="switchTab($event as 'coupon' | 'activity')">
          <el-radio-button value="coupon">优惠券</el-radio-button>
          <el-radio-button value="activity">促销活动</el-radio-button>
        </el-radio-group>
        <div class="spacer" />
        <el-button
          type="primary"
          size="small"
          @click="tab === 'coupon' ? openCouponDialog() : openActivityDialog()"
        >
          {{ tab === 'coupon' ? '新建券模板' : '新建活动' }}
        </el-button>
      </div>

      <el-radio-group
        :model-value="tab === 'coupon' ? couponStatus : activityStatus"
        size="small"
        @change="switchStatus($event as number | undefined)"
      >
        <el-radio-button v-for="s in STATUS_TABS" :key="s.label" :value="s.value">
          {{ s.label }}
        </el-radio-button>
      </el-radio-group>

      <!-- ---------- 优惠券 ---------- -->
      <div v-if="tab === 'coupon'" class="panel">
        <el-empty v-if="!loading && coupons.length === 0" description="还没有券模板" />

        <el-table v-else v-loading="loading" :data="coupons">
          <el-table-column label="券" min-width="240">
            <template #default="{ row }">
              <div class="cell-title">{{ row.name }}</div>
              <div class="cell-sub">
                {{ row.typeText }} · {{ SCOPE_TYPE_TEXT[row.scopeType] ?? '—' }}
                <span v-if="row.perUserLimit > 1"> · 每人限领 {{ row.perUserLimit }}</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="面额" width="150">
            <template #default="{ row }">
              <div class="money tnum">{{ couponValueText(row) }}</div>
              <div v-if="row.threshold > 0" class="cell-sub tnum">
                满 ¥{{ formatYuan(row.threshold) }}
              </div>
              <div v-else class="cell-sub">无门槛</div>
            </template>
          </el-table-column>

          <el-table-column label="发行 / 已发" width="130" align="right">
            <template #default="{ row }">
              <span class="tnum">{{ row.issuedCount }} / {{ row.totalCount }}</span>
              <div class="cell-sub tnum">已核销 {{ row.usedCount }}</div>
            </template>
          </el-table-column>

          <el-table-column label="有效期" width="170">
            <template #default="{ row }">
              <template v-if="row.validType === VALID_DAYS_AFTER">
                <span class="cell-sub">领取后 {{ row.validDays }} 天</span>
              </template>
              <template v-else>
                <div class="cell-sub tnum">{{ formatDateTime(row.validStart ?? '') }}</div>
                <div class="cell-sub tnum">至 {{ formatDateTime(row.validEnd ?? '') }}</div>
              </template>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag
                :type="row.status === TPL_ONGOING ? 'success' : 'info'"
                size="small"
                effect="light"
              >
                {{ row.statusText }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="100" align="right" fixed="right">
            <template #default="{ row }">
              <el-button size="small" @click="openIssue(row)">发券</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="couponMore" class="more">
          <el-button size="small" :loading="loadingMore" @click="loadCoupons(false)">
            加载更多
          </el-button>
        </div>
      </div>

      <!-- ---------- 促销活动 ---------- -->
      <div v-else class="panel">
        <el-empty v-if="!loading && activities.length === 0" description="还没有促销活动" />

        <el-table v-else v-loading="loading" :data="activities">
          <el-table-column label="活动" min-width="220">
            <template #default="{ row }">
              <div class="cell-title">{{ row.name }}</div>
              <div class="cell-sub">
                {{ row.levelText }} · {{ row.typeText }}
                <span v-if="row.priority !== 0"> · 优先级 {{ row.priority }}</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="优惠" width="170">
            <template #default="{ row }">
              <div class="money tnum">{{ activityValueText(row) }}</div>
              <div v-if="row.threshold > 0" class="cell-sub tnum">
                满 ¥{{ formatYuan(row.threshold) }}
              </div>
              <div v-else class="cell-sub">{{ row.calcTypeText }}</div>
            </template>
          </el-table-column>

          <el-table-column label="活动时间" width="180">
            <template #default="{ row }">
              <div class="cell-sub tnum">{{ formatDateTime(row.startAt) }}</div>
              <div class="cell-sub tnum">至 {{ formatDateTime(row.endAt) }}</div>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag
                :type="row.status === TPL_ONGOING ? 'success' : 'info'"
                size="small"
                effect="light"
              >
                {{ row.statusText }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="activityMore" class="more">
          <el-button size="small" :loading="loadingMore" @click="loadActivities(false)">
            加载更多
          </el-button>
        </div>
      </div>
    </template>

    <!-- ============ 新建券模板 ============ -->
    <el-dialog v-model="couponVisible" title="新建券模板" width="560px">
      <el-form :model="couponForm" label-width="96px">
        <el-form-item label="券名称">
          <el-input v-model="couponForm.name" maxlength="64" placeholder="如：满 200 减 30" />
        </el-form-item>

        <el-form-item label="券类型">
          <el-select v-model="couponForm.type" style="width: 100%">
            <el-option
              v-for="t in COUPON_TYPES"
              :key="t"
              :label="COUPON_TYPE_TEXT[t]"
              :value="t"
            />
          </el-select>
        </el-form-item>

        <!-- 字段随类型变化：满减要门槛、折扣要折数与封顶、运费/无门槛只要面额 -->
        <el-form-item v-if="needsThreshold" label="使用门槛">
          <el-input-number v-model="couponForm.thresholdYuan" :min="0" :controls="false" />
          <span class="inline-hint">元，0 表示无门槛</span>
        </el-form-item>

        <el-form-item v-if="isDiscount" label="折扣">
          <el-input-number
            v-model="couponForm.rate"
            :min="0.1"
            :max="10"
            :step="0.1"
            :precision="1"
            :controls="false"
          />
          <span class="inline-hint">折，如 8.5 表示 85 折</span>
        </el-form-item>
        <el-form-item v-else-if="!isExchange" label="减免金额">
          <el-input-number v-model="couponForm.discountYuan" :min="0" :controls="false" />
          <span class="inline-hint">元{{ couponForm.type === COUPON_TYPE_FREIGHT ? '（抵扣运费）' : '' }}</span>
        </el-form-item>

        <el-form-item v-if="needsCap" label="折扣封顶">
          <el-input-number v-model="couponForm.maxDiscountYuan" :min="0" :controls="false" />
          <span class="inline-hint">元，0 表示不限</span>
        </el-form-item>

        <el-form-item label="发行总量">
          <el-input-number
            v-model="couponForm.totalCount"
            :min="1"
            :max="1000000"
            :controls="false"
          />
          <span class="inline-hint">每人限领</span>
          <el-input-number
            v-model="couponForm.perUserLimit"
            :min="1"
            :max="100"
            :controls="false"
            class="ml"
          />
          <span class="inline-hint">张</span>
        </el-form-item>

        <el-form-item label="有效期">
          <el-radio-group v-model="couponForm.validType">
            <el-radio-button :value="VALID_FIXED">固定区间</el-radio-button>
            <el-radio-button :value="VALID_DAYS_AFTER">领取后 N 天</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="couponForm.validType === VALID_FIXED" label="起止时间">
          <el-date-picker
            v-model="couponForm.validRange"
            type="datetimerange"
            value-format="YYYY-MM-DDTHH:mm:ssZ"
            start-placeholder="开始"
            end-placeholder="结束"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item v-else label="有效天数">
          <el-input-number v-model="couponForm.validDays" :min="1" :max="365" :controls="false" />
          <span class="inline-hint">天</span>
        </el-form-item>

        <el-form-item label="适用范围">
          <el-select v-model="couponForm.scopeType" style="width: 100%">
            <el-option
              v-for="(text, value) in SCOPE_TYPE_TEXT"
              :key="value"
              :label="text"
              :value="Number(value)"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="couponForm.scopeType === SCOPE_CATEGORY" label="指定类目">
          <el-select v-model="couponForm.categoryIds" multiple filterable style="width: 100%">
            <el-option v-for="c in categories" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item
          v-else-if="couponForm.scopeType === SCOPE_SKU || couponForm.scopeType === SCOPE_SHOP"
          label="目标 ID"
        >
          <el-input
            v-model="couponForm.manualScopeIds"
            placeholder="填 ID，多个用逗号分隔"
          />
          <div class="form-hint">
            运营端没有"列出全部商品 / 店铺"的接口，这里只能手填 ID
          </div>
        </el-form-item>

        <el-alert
          v-if="isExchange"
          type="info"
          :closable="false"
          show-icon
          title="兑换券通过券码核销，这里只建模板；发放走「发券」入口"
        />
      </el-form>

      <template #footer>
        <el-button @click="couponVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitCoupon">创建</el-button>
      </template>
    </el-dialog>

    <!-- ============ 新建活动 ============ -->
    <el-dialog v-model="activityVisible" title="新建促销活动" width="540px">
      <el-form :model="activityForm" label-width="96px">
        <el-form-item label="活动名称">
          <el-input v-model="activityForm.name" maxlength="64" placeholder="如：国庆店铺满减" />
        </el-form-item>

        <el-form-item label="优惠层级">
          <el-radio-group v-model="activityForm.level">
            <el-radio-button v-for="l in LEVELS" :key="l" :value="l">
              {{ LEVEL_TEXT[l] }}
            </el-radio-button>
          </el-radio-group>
          <div class="form-hint">
            提交后类型为 <code>{{ PROMO_TYPE_TEXT[activityType] }}</code>，决定它参与哪一层算价
          </div>
        </el-form-item>

        <el-form-item label="优惠方式">
          <el-radio-group v-model="activityForm.calcType">
            <el-radio-button v-for="c in CALC_TYPES" :key="c" :value="c">
              {{ CALC_TYPE_TEXT[c] }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="activityIsDiscount" label="折扣">
          <el-input-number
            v-model="activityForm.rate"
            :min="0.1"
            :max="10"
            :step="0.1"
            :precision="1"
            :controls="false"
          />
          <span class="inline-hint">折，如 9 表示 9 折</span>
        </el-form-item>
        <el-form-item v-else label="金额">
          <el-input-number v-model="activityForm.valueYuan" :min="0" :controls="false" />
          <span class="inline-hint">
            元{{ activityForm.calcType === CALC_FIXED ? '（特价即定价）' : '（直降额）' }}
          </span>
        </el-form-item>

        <el-form-item v-if="activityIsDiscount" label="折扣封顶">
          <el-input-number v-model="activityForm.maxDiscountYuan" :min="0" :controls="false" />
          <span class="inline-hint">元，0 表示不限</span>
        </el-form-item>

        <el-form-item label="订单门槛">
          <el-input-number v-model="activityForm.thresholdYuan" :min="0" :controls="false" />
          <span class="inline-hint">元，0 表示不限</span>
        </el-form-item>

        <el-form-item label="活动时间">
          <el-date-picker
            v-model="activityForm.range"
            type="datetimerange"
            value-format="YYYY-MM-DDTHH:mm:ssZ"
            start-placeholder="开始"
            end-placeholder="结束"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="优先级">
          <el-input-number v-model="activityForm.priority" :controls="false" />
          <span class="inline-hint">同一层级内多个活动时，越大越优先</span>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="activityVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitActivity">创建</el-button>
      </template>
    </el-dialog>

    <!-- ============ 定向发券 ============ -->
    <el-dialog v-model="issueVisible" title="定向发券" width="460px">
      <p v-if="issueTarget" class="dlg-quote">{{ issueTarget.name }}</p>
      <el-form :model="issueForm" label-width="80px">
        <el-form-item label="手机号">
          <div class="phone-row">
            <el-input
              v-model="issueForm.phone"
              placeholder="11 位手机号"
              maxlength="11"
              class="phone-input"
              @keyup.enter="lookupIssueUser"
            />
            <el-button :loading="looking" @click="lookupIssueUser">查询</el-button>
          </div>
        </el-form-item>
        <!-- 查到才显示收件人；这也是"确认发券"能不能点的前提 -->
        <el-form-item v-if="issueUser" label="收件人">
          <span class="issue-user">
            {{ issueUser.nickname }}（{{ issueUser.phoneMasked }}）
          </span>
        </el-form-item>
        <el-form-item label="张数">
          <el-input-number v-model="issueForm.count" :min="1" :max="100" :controls="false" />
        </el-form-item>
      </el-form>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="补发不占活动额度：券模板的「已发」计数不会增加"
      />
      <template #footer>
        <el-button @click="issueVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" :disabled="!issueUser" @click="submitIssue">
          确认发券
        </el-button>
      </template>
    </el-dialog>
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

.tabs {
  display: flex;
  align-items: center;
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.cell-title {
  font-size: var(--text-base);
  color: var(--color-text);
}

.cell-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
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

.inline-hint {
  margin-left: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.ml {
  margin-left: var(--space-3);
}

.form-hint {
  width: 100%;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
  line-height: var(--leading-snug);
}

.dlg-quote {
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

/* 手机号 + 查询按钮横排 */
.phone-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.phone-input {
  width: 180px;
}

/* 查到的收件人 —— 这是"确认发给谁"的唯一依据，给点份量 */
.issue-user {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}
</style>
