<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { fetchCategoryTree, type Category } from '@/api/category'
import { isBizError } from '@/api/errors'
import { fetchSkusByIds } from '@/api/product'
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
  SCOPE_SHOP,
  SCOPE_TYPE_TEXT,
  TPL_NOT_STARTED,
  TPL_ONGOING,
  VALID_DAYS_AFTER,
  VALID_FIXED,
  createCouponTemplate,
  createPromoActivity,
  fetchShopsByIds,
  fetchSiteContact,
  fetchSiteTheme,
  issueCoupon,
  listCouponIssues,
  listCouponTemplates,
  listPromoActivities,
  lookupUserByPhone,
  updateSiteContact,
  updateSiteTheme,
  voidCouponTemplate,
  voidPromoActivity,
  type AdminCouponTemplate,
  type AdminPromoActivity,
  type CouponIssueRecord,
  type CouponIssueSummary,
  type SkinOption,
  type UserLookup,
} from '@/api/promotion'
import ScopePicker from '@/components/ScopePicker.vue'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime, formatYuan, yuanToFen } from '@/utils/money'

const auth = useAuthStore()

/** 营销台是运营专属：后端接口只放给 admin / finance，商家看不到这个页面 */
const isAdmin = computed(() => auth.user?.role === 'admin' || auth.user?.role === 'finance')

const tab = ref<'coupon' | 'activity' | 'site'>('coupon')

// ---------- 站点设置：全站皮肤 ----------
/** 当前选中的皮肤（未保存前只是本地选择） */
const siteSkin = ref('neutral')
/** 已保存的皮肤 —— 用它判断"改了没有"，避免白提交一次 */
const savedSkin = ref('neutral')
/** 可选清单来自后端，前端不维护第二份 */
const skinOptions = ref<SkinOption[]>([])
const skinLoading = ref(false)
const skinSaving = ref(false)

// ---------- 站点设置：客服联系方式 ----------
/**
 * 三项都按**字符串**编辑（空串 = 不填）。
 *
 * ★ 后端把"空"存成 NULL，这里用空串表达同一件事：`el-input` 的 v-model 是字符串，
 *   中间再插一层 null ↔ '' 的转换只会多一处出错的地方。提交时后端做归一，
 *   返回的归一化结果再回填表单（见 saveSiteContact）。
 */
const contactForm = reactive({ serviceEmail: '', servicePhone: '', serviceHours: '' })
const contactSaving = ref(false)

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
  /** 适用范围的目标 id（雪花，**string**）。全场是 null —— 由 ScopePicker 抛回来 */
  scopeValue: null as string[] | null,
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
  couponForm.scopeValue = null
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

  // ★ 选了适用范围却没挑到任何目标，会建出一张**永远匹配不到商品**的券 ——
  //   它在列表上和正常券一模一样，只有下单时才发现不生效。后端也会拒，
  //   但在这里拦住能给出一句更贴事的话。
  const scopeValue = couponForm.scopeValue
  if (couponForm.scopeType !== SCOPE_ALL && (scopeValue === null || scopeValue.length === 0)) {
    const what = SCOPE_TYPE_TEXT[couponForm.scopeType] ?? '目标'
    ElMessage.warning(`适用范围选了「${what}」，就要挑至少一个目标`)
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
      scopeValue,
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

// ============================================================
// 适用范围
//
// 选择器（全场 / 指定商品 / 指定类目 / 指定店铺）抽到了
// ``components/ScopePicker.vue`` —— 券与活动用的是同一套后端契约，
// 复制一份等于给自己留两份要同步的代码。
// ============================================================

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
  /** 适用范围。与券同一套语义（全场 / 商品 / 类目 / 店铺） */
  scopeType: SCOPE_ALL as number,
  scopeValue: null as string[] | null,
})

/** 层级决定参与哪一层算价，也决定 type 这个字符串键 */
const activityType = computed(() => PROMO_TYPE_BY_LEVEL[activityForm.level] ?? 'PROMO_ITEM')
const activityIsDiscount = computed(() => activityForm.calcType === CALC_RATE)

/**
 * 「特价」只对单品级有意义 —— 它改的是**单价**。
 *
 * 店铺级 / 平台级算的是"从总额里减一笔"，特价在那一层没有对应物：
 * 后端只会把它当成直降（所以接口现在直接拒），界面上写着"特价即定价"、
 * 算出来却是另一个数，是最难发现的那种坏法。那一档干脆不列出来。
 */
const activityCalcTypes = computed(() =>
  activityForm.level === LEVEL_ITEM ? [CALC_DIRECT, CALC_RATE, CALC_FIXED] : [CALC_DIRECT, CALC_RATE],
)

// 从单品级切到店铺级 / 平台级时，把已经选中的「特价」退回直降
watch(
  () => activityForm.level,
  (level) => {
    if (level !== LEVEL_ITEM && activityForm.calcType === CALC_FIXED) {
      activityForm.calcType = CALC_DIRECT
    }
  },
)

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
  activityForm.scopeType = SCOPE_ALL
  activityForm.scopeValue = null
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
  // 与券同一条：选了范围却没挑到目标，活动会**永远匹配不到任何商品**，
  // 而列表上它看起来和正常活动一模一样
  const scopeValue = activityForm.scopeValue
  if (activityForm.scopeType !== SCOPE_ALL && (scopeValue === null || scopeValue.length === 0)) {
    const what = SCOPE_TYPE_TEXT[activityForm.scopeType] ?? '目标'
    ElMessage.warning(`适用范围选了「${what}」，就要挑至少一个目标`)
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
      scopeType: activityForm.scopeType,
      scopeValue,
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
// 补发记录
//
// ★ 补发**不占活动额度**，"活动库存"根本不构成约束 —— 能被看见才是最有效的那道。
//   这个抽屉就是那道约束：谁、何时、给谁、发了哪张券，全部摆在明面上。
//   后端另有两道配额闸（单用户单模板、单运营 24 小时），撞了会在发券时报错。
// ============================================================
const recordsVisible = ref(false)
const recordsLoading = ref(false)
const records = ref<CouponIssueRecord[]>([])
const recordsSummary = ref<CouponIssueSummary | null>(null)
const recordsCursor = ref<string | null>(null)
const recordsHasMore = ref(false)

async function openIssueRecords(): Promise<void> {
  recordsVisible.value = true
  await loadIssueRecords(true)
}

async function loadIssueRecords(reset = false): Promise<void> {
  if (reset) {
    records.value = []
    recordsCursor.value = null
    recordsSummary.value = null
  }
  recordsLoading.value = true
  try {
    const page = await listCouponIssues({ cursor: recordsCursor.value, limit: 20 })
    records.value = [...records.value, ...page.items]
    recordsSummary.value = page.summary
    recordsCursor.value = page.nextCursor
    recordsHasMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载补发记录失败')
  } finally {
    recordsLoading.value = false
  }
}

// ============================================================
// 下线（作废）
//
// ★ 用"作废"而不是"删除"：活动一旦产生过订单，订单里的优惠快照就指着它 ——
//   删行会让那些快照变成查不到来源的孤儿。作废只是改状态，**算价当场不再带它**
//   （后端算价查询要求 status = 2），已产生的订单不受影响。
//   后半句必须写在确认框里：运营一听"下线"就怕已经卖出去的单子跟着变，不说明白不敢点。
// ============================================================
/** 未开始 / 进行中的才需要作废：已结束的没什么可停，已作废的不必再点。 */
function canVoid(status: number): boolean {
  return status === TPL_NOT_STARTED || status === TPL_ONGOING
}

async function voidActivity(row: AdminPromoActivity): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定作废「${row.name}」吗？作废后立即不再参与算价，已产生的订单不受影响。`,
      '作废促销活动',
      { type: 'warning', confirmButtonText: '作废', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await voidPromoActivity(row.id)
    ElMessage.success('活动已作废')
    await loadActivities(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '作废失败')
  }
}

async function voidCoupon(row: AdminCouponTemplate): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定作废「${row.name}」吗？作废后不能再被领取，已经领到券的用户不受影响。`,
      '作废券模板',
      { type: 'warning', confirmButtonText: '作废', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await voidCouponTemplate(row.id)
    ElMessage.success('券模板已作废')
    await loadCoupons(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '作废失败')
  }
}

// ============================================================
// 详情
//
// ★ 列表只放得下"最该看的那几列"，而关键的东西恰恰不在列上：适用范围存的是 id、
//   折扣封顶和优先级根本没地方显示。运营问"这条到底管了谁"时只能猜。
// ============================================================
const detailVisible = ref(false)
const detailTitle = ref('')
const detailRows = ref<{ label: string; value: string }[]>([])
const detailLoading = ref(false)

/** 类目 id → 名字。一次拉全树在内存里查，别逐个查库 */
async function categoryNames(ids: string[]): Promise<string[]> {
  const flat = new Map<string, string>()
  const walk = (nodes: Category[]): void => {
    for (const n of nodes) {
      flat.set(n.id, n.name)
      if (n.children?.length) walk(n.children)
    }
  }
  walk(await fetchCategoryTree())
  return ids.map((id) => flat.get(id) ?? id)
}

/**
 * 把适用范围讲成人话。
 *
 * 三类目标分别反查：类目走类目树、规格走 `/skus/batch`、店铺走按 id 回看
 * （`fetchShopsByIds`）。
 *
 * ★ **解析不出来的不吞掉** —— 那通常意味着目标已经下架或删除，而"这条活动
 *   还指着一个不存在的商品"正是运营最该看到的事。
 */
async function scopeText(scopeType: number, scopeValue: string[] | null): Promise<string> {
  const what = SCOPE_TYPE_TEXT[scopeType] ?? '范围'
  if (scopeType === SCOPE_ALL) return '全场'
  const ids = scopeValue ?? []
  if (ids.length === 0) return `${what}：（没有目标，这条不会生效）`

  if (scopeType === SCOPE_CATEGORY) {
    return `${what}：${(await categoryNames(ids)).join('、')}（含全部子类目）`
  }
  if (scopeType === SCOPE_SHOP) {
    const names = new Map((await fetchShopsByIds(ids)).map((s) => [s.id, s.name]))
    return `${what}：${ids.map((id) => names.get(id) ?? `${id}（已不存在）`).join('、')}`
  }
  // 剩下的一档就是「指定商品」：存的是 **SKU id**（算价按 SKU 匹配）
  const named = new Map(
    (await fetchSkusByIds(ids)).map((s) => [s.id, `${s.title}（${s.specText || '默认规格'}）`]),
  )
  return `${what}：${ids.map((id) => named.get(id) ?? `${id}（已下架或删除）`).join('、')}`
}

async function openActivityDetail(row: AdminPromoActivity): Promise<void> {
  detailTitle.value = row.name
  detailRows.value = []
  detailVisible.value = true
  detailLoading.value = true
  try {
    const scope = await scopeText(row.scopeType, row.scopeValue)
    detailRows.value = [
      { label: '活动名称', value: row.name },
      { label: '优惠层级', value: `${row.levelText}（${row.typeText}）` },
      { label: '优惠方式', value: activityValueText(row) },
      {
        label: '订单门槛',
        // 单品级的门槛是订单级的概念、引擎不读它，接口现在也不再放行 ——
        // 只有历史数据里还挂着值，这里直说它没生效
        value:
          row.level === LEVEL_ITEM
            ? '不适用（门槛是订单级的概念，单品级不参与计算）'
            : row.threshold > 0
              ? `满 ¥${formatYuan(row.threshold)}`
              : '无门槛',
      },
      { label: '适用范围', value: scope },
      {
        label: '活动时间',
        value: `${formatDateTime(row.startAt)} 至 ${formatDateTime(row.endAt)}`,
      },
      { label: '优先级', value: String(row.priority) },
      { label: '状态', value: row.statusText },
      { label: '创建时间', value: formatDateTime(row.createdAt) },
    ]
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '读取详情失败')
  } finally {
    detailLoading.value = false
  }
}

async function openCouponDetail(row: AdminCouponTemplate): Promise<void> {
  detailTitle.value = row.name
  detailRows.value = []
  detailVisible.value = true
  detailLoading.value = true
  try {
    const scope = await scopeText(row.scopeType, row.scopeValue)
    const rows = [
      { label: '券名称', value: row.name },
      { label: '券类型', value: row.typeText },
      { label: '面额', value: couponValueText(row) },
      {
        label: '使用门槛',
        value: row.threshold > 0 ? `满 ¥${formatYuan(row.threshold)}` : '无门槛',
      },
    ]
    if (row.maxDiscount > 0) {
      rows.push({ label: '折扣封顶', value: `¥${formatYuan(row.maxDiscount)}` })
    }
    rows.push(
      {
        label: '发行 / 已发 / 已核销',
        value: `${row.totalCount} / ${row.issuedCount} / ${row.usedCount}`,
      },
      { label: '每人限领', value: `${row.perUserLimit} 张` },
      {
        label: '有效期',
        value:
          row.validType === VALID_DAYS_AFTER
            ? `领取后 ${row.validDays} 天`
            : `${formatDateTime(row.validStart ?? '')} 至 ${formatDateTime(row.validEnd ?? '')}`,
      },
      { label: '适用范围', value: scope },
      { label: '状态', value: row.statusText },
      { label: '创建时间', value: formatDateTime(row.createdAt) },
    )
    detailRows.value = rows
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '读取详情失败')
  } finally {
    detailLoading.value = false
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

async function switchTab(next: 'coupon' | 'activity' | 'site'): Promise<void> {
  if (tab.value === next) return
  tab.value = next
  if (next === 'coupon') await loadCoupons(true)
  else if (next === 'activity') await loadActivities(true)
  else await loadSiteSettings()
}

/** 工具栏那个「刷新」按当前页签刷新 —— 三个页签各有各的数据源 */
function refreshCurrent(): void {
  if (tab.value === 'coupon') void loadCoupons(true)
  else if (tab.value === 'activity') void loadActivities(true)
  else void loadSiteSettings()
}

/**
 * 读两张站点配置：皮肤 + 客服联系方式。
 *
 * ★ 一起拉、共用一个 loading：它们同属一个页签，分两次 loading 只会让面板闪两下。
 * ★ 皮肤的 `options` 也一并从后端取：清单只有后端一份（`SITE_SKINS`），前端不另存
 *   数组 —— 否则会出现"后台能选、买家端不认"的漂移。
 */
async function loadSiteSettings(): Promise<void> {
  skinLoading.value = true
  try {
    const [theme, contact] = await Promise.all([fetchSiteTheme(), fetchSiteContact()])
    siteSkin.value = theme.skin
    savedSkin.value = theme.skin
    skinOptions.value = theme.options
    contactForm.serviceEmail = contact.serviceEmail ?? ''
    contactForm.servicePhone = contact.servicePhone ?? ''
    contactForm.serviceHours = contact.serviceHours ?? ''
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载站点设置失败')
  } finally {
    skinLoading.value = false
  }
}

/**
 * 保存站点皮肤，**立刻对全站生效**。
 *
 * ★ 不做二次确认：它不是破坏性操作，而且随时能改回来。这个页面里真正需要
 *   确认的是**作废**活动/券模板（不可逆），那是另一回事。
 */
async function saveSiteTheme(): Promise<void> {
  if (skinSaving.value || siteSkin.value === savedSkin.value) return
  skinSaving.value = true
  try {
    savedSkin.value = (await updateSiteTheme(siteSkin.value)).skin
    ElMessage.success('已切换全站皮肤，买家端刷新后生效')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    skinSaving.value = false
  }
}

/**
 * 保存客服联系方式。
 *
 * ★ 保存后**用返回值回填表单**：后端会把空串 / 纯空格归一成 null，回填的是归一化
 *   之后的值 —— 否则界面上留着空格、库里其实已经清了，运营下次看到会以为没保存上。
 */
async function saveSiteContact(): Promise<void> {
  if (contactSaving.value) return
  contactSaving.value = true
  try {
    const saved = await updateSiteContact({ ...contactForm })
    contactForm.serviceEmail = saved.serviceEmail ?? ''
    contactForm.servicePhone = saved.servicePhone ?? ''
    contactForm.serviceHours = saved.serviceHours ?? ''
    ElMessage.success('已保存客服联系方式')
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    contactSaving.value = false
  }
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
      <el-button size="small" :loading="loading" @click="refreshCurrent">刷新</el-button>
    </div>

    <el-empty v-if="!isAdmin" description="营销中心仅对平台运营开放" />

    <template v-else>
      <div class="tabs">
        <el-radio-group
          :model-value="tab"
          size="small"
          @change="switchTab($event as 'coupon' | 'activity' | 'site')"
        >
          <el-radio-button value="coupon">优惠券</el-radio-button>
          <el-radio-button value="activity">促销活动</el-radio-button>
          <el-radio-button value="site">站点设置</el-radio-button>
        </el-radio-group>
        <div class="spacer" />
        <!-- 补发记录：让"谁给谁发了多少"看得见。这是约束运营发券的主要手段 -->
        <el-button v-if="tab === 'coupon'" size="small" @click="openIssueRecords">
          补发记录
        </el-button>
        <!-- 站点设置是两个配置表单，没有"新建"这个动作 -->
        <el-button
          v-if="tab !== 'site'"
          type="primary"
          size="small"
          @click="tab === 'coupon' ? openCouponDialog() : openActivityDialog()"
        >
          {{ tab === 'coupon' ? '新建券模板' : '新建活动' }}
        </el-button>
      </div>

      <!-- 状态档只有两个列表页用：站点设置没有状态可筛 -->
      <el-radio-group
        v-if="tab !== 'site'"
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

          <el-table-column label="操作" width="220" align="right" fixed="right">
            <template #default="{ row }">
              <el-button size="small" @click="openCouponDetail(row)">详情</el-button>
              <el-button size="small" @click="openIssue(row)">发券</el-button>
              <el-button
                v-if="canVoid(row.status)"
                size="small"
                type="danger"
                plain
                @click="voidCoupon(row)"
              >
                作废
              </el-button>
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
                <!-- 限了范围就必须显示出来：列表上看着和全场活动一样，
                     运营没法判断这条到底管谁 -->
                <span v-if="row.scopeType !== SCOPE_ALL">
                  · {{ SCOPE_TYPE_TEXT[row.scopeType] ?? '—' }}
                </span>
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

          <el-table-column label="操作" width="160" align="right" fixed="right">
            <template #default="{ row }">
              <el-button size="small" @click="openActivityDetail(row)">详情</el-button>
              <el-button
                v-if="canVoid(row.status)"
                size="small"
                type="danger"
                plain
                @click="voidActivity(row)"
              >
                作废
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="activityMore" class="more">
          <el-button size="small" :loading="loadingMore" @click="loadActivities(false)">
            加载更多
          </el-button>
        </div>
      </div>

      <!-- ---------- 站点设置 ---------- -->
      <!-- 两张**全站**配置：皮肤 + 客服联系方式。皮肤保存后买家端所有人下次加载就
           换过来，买家自己不能选（docs/17 §3），所以文案要写清影响面，别让运营
           以为只是改了自己看到的样子。联系方式同理 —— 它也不是"给谁看的偏好"。 -->
      <div v-if="tab === 'site'" v-loading="skinLoading" class="panel">
        <div class="site-body">
          <section class="site-block">
            <h3 class="site-title">全站皮肤</h3>
            <p class="site-lead">皮肤由平台在这里启用，保存后全站生效，买家端没有切换入口。</p>
            <el-radio-group v-model="siteSkin">
              <el-radio-button v-for="o in skinOptions" :key="o.value" :value="o.value">
                {{ o.label }}
              </el-radio-button>
            </el-radio-group>
            <p class="hint">
              没有大促时保持「默认·中性」。大促皮肤与促销活动是两件事：皮肤管外观，
              顶部那条活动公告读的是真实进行中的活动。
            </p>
            <el-button
              type="primary"
              :loading="skinSaving"
              :disabled="siteSkin === savedSkin"
              @click="saveSiteTheme"
            >
              保存
            </el-button>
          </section>

          <section class="site-block">
            <h3 class="site-title">客服联系方式</h3>
            <p class="site-lead">
              展示在买家端的登录页与「账号被冻结」提示上。这两个位置正是给登不进来的人
              看的，所以这一项不是装饰：三项全留空，那些用户就没有任何联系入口。
            </p>
            <el-form :model="contactForm" label-width="80px" class="site-form">
              <el-form-item label="客服邮箱">
                <el-input
                  v-model="contactForm.serviceEmail"
                  maxlength="128"
                  placeholder="如：service@example.com"
                />
              </el-form-item>
              <el-form-item label="客服电话">
                <el-input
                  v-model="contactForm.servicePhone"
                  maxlength="32"
                  placeholder="如：400-000-1234"
                />
              </el-form-item>
              <el-form-item label="服务时间">
                <el-input
                  v-model="contactForm.serviceHours"
                  maxlength="64"
                  placeholder="如：工作日 9:00-18:00"
                />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="contactSaving" @click="saveSiteContact">
                  保存
                </el-button>
                <span class="hint">留空就是取消该一项。</span>
              </el-form-item>
            </el-form>
          </section>
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

        <ScopePicker
          v-model:type="couponForm.scopeType"
          @change="couponForm.scopeValue = $event"
        />

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
            <el-radio-button v-for="c in activityCalcTypes" :key="c" :value="c">
              {{ CALC_TYPE_TEXT[c] }}
            </el-radio-button>
          </el-radio-group>
          <div v-if="activityForm.level !== LEVEL_ITEM" class="form-hint">
            「特价」只对单品级有意义（它改的是单价）；订单级是从总额里减一笔
          </div>
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

        <!-- 适用范围放在门槛前面：两者是同一类东西（"这笔优惠管谁、什么时候成立"），
             挨着看才读得通 -->
        <ScopePicker
          v-model:type="activityForm.scopeType"
          @change="activityForm.scopeValue = $event"
        />

        <!-- ★ 单品级不给「订单门槛」：门槛是**订单级**的概念，引擎只在 Level 1/2
             用它过滤候选（_apply_order_level），单品级压根不读它
             （_apply_item_level）。以前这一栏三档都显示，运营填了"满 50 减 10"，
             实际是"每一件都减 10" —— 连 8999 的笔记本也减。后端同样会拒。
             要表达"满 X 减 Y"就建店铺级 / 平台级活动。 -->
        <el-form-item v-if="activityForm.level !== LEVEL_ITEM" label="订单门槛">
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

    <!-- ============ 详情 ============ -->
    <el-dialog v-model="detailVisible" :title="detailTitle" width="560px">
      <div v-loading="detailLoading" class="detail">
        <div v-for="r in detailRows" :key="r.label" class="detail-row">
          <div class="detail-label">{{ r.label }}</div>
          <div class="detail-value">{{ r.value }}</div>
        </div>
      </div>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
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

    <!-- ============ 补发记录 ============ -->
    <el-drawer v-model="recordsVisible" title="补发记录" size="680px">
      <div v-if="recordsSummary" class="issue-summary">
        <span>
          最近 24 小时共补发 <b class="tnum">{{ recordsSummary.total }}</b> 张
        </span>
        <span v-for="op in recordsSummary.byOperator" :key="op.operatorName" class="by-op">
          {{ op.operatorName }} {{ op.count }} 张
        </span>
      </div>

      <el-table v-if="records.length > 0" v-loading="recordsLoading" :data="records" size="small">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ formatDateTime(row.createdAt) }}</template>
        </el-table-column>
        <el-table-column label="操作人" width="100" prop="operatorName" />
        <el-table-column label="收件人" min-width="160">
          <template #default="{ row }">
            <div>{{ row.nickname || '—' }}</div>
            <div class="sub tnum">{{ row.phoneMasked }}</div>
          </template>
        </el-table-column>
        <el-table-column label="券模板" min-width="130" prop="templateName" />
        <el-table-column label="券码" width="150" prop="code" />
      </el-table>

      <el-empty v-else-if="!recordsLoading" description="还没有补发记录" />

      <div v-if="recordsHasMore" class="load-more">
        <el-button size="small" :loading="recordsLoading" @click="loadIssueRecords(false)">
          加载更多
        </el-button>
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

/* 详情：左标签右值。用 grid 而不是 flex —— 范围那一行的值可能挂着一串目标，
   flex 里长值会把标签挤扁 */
.detail-row {
  display: grid;
  grid-template-columns: 88px 1fr;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  font-size: var(--text-sm);
  border-bottom: 1px solid var(--color-border);
}

.detail-row:last-child {
  border-bottom: none;
}

.detail-label {
  color: var(--color-text-tertiary);
}

.detail-value {
  color: var(--color-text);
  word-break: break-all;
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

/* 补发记录的汇总条：一眼看出发了多少、谁发得多 */
.issue-summary {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.issue-summary b {
  color: var(--color-price);
}

/* 按操作人的小分组，用描边和总数区分开 */
.by-op {
  padding: 2px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-size: var(--text-xs);
}

.sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.load-more {
  margin-top: var(--space-3);
  text-align: center;
}

/* ---------- 站点设置 ---------- */

.site-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  padding: var(--space-5);
}

/* 两块配置各保存各的，用描边分开 —— 别让运营以为一个「保存」管两件事 */
.site-block {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-4);
  padding: var(--space-5);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.site-title {
  margin: 0;
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.site-lead {
  max-width: 60em;
  font-size: var(--text-base);
  line-height: var(--leading-normal);
  color: var(--color-text);
}

/* 输入框撑满一块，否则默认宽度装不下一个邮箱 */
.site-form {
  width: 100%;
  max-width: 460px;
}
</style>
