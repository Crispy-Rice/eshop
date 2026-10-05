<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { listAddresses, type Address } from '@/api/auth'
import { fetchCart, removeCartItems, type Cart, type CartItem } from '@/api/cart'
import { isBizError } from '@/api/errors'
import {
  CODE_UNUSED,
  calcPrice,
  fetchMyCoupons,
  type CalcPriceResult,
  type MyCoupon,
} from '@/api/promotion'
import { createOrder, createPayment } from '@/api/trade'
import { useAuthStore } from '@/stores/auth'
import { useCartStore } from '@/stores/cart'
import { newIdempotencyKey } from '@/utils/idempotency'
import { formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()
const cartStore = useCartStore()
const router = useRouter()

const cart = ref<Cart | null>(null)
const myCoupons = ref<MyCoupon[]>([])
const calc = ref<CalcPriceResult | null>(null)
const selectedCoupons = ref<string[]>([])
const addresses = ref<Address[]>([])
const addressId = ref<string>('')
const loading = ref(false)
const submitting = ref(false)

/** 结算的商品 = 购物车里**已勾选且有效**的那些 */
const lines = computed<CartItem[]>(() =>
  (cart.value?.groups ?? [])
    .flatMap((g) => g.items)
    .filter((i) => i.selected && i.status === 1),
)

const payableCoupons = computed(() => myCoupons.value.filter((c) => c.status === CODE_UNUSED && !c.expired))

/** 券的来源类型，与后端 `promotion/models.py` 的 DISCOUNT_COUPON_* 一致 */
const COUPON_PLATFORM = 'COUPON_PLATFORM'
const COUPON_SHOP = 'COUPON_SHOP'

interface CouponGroup {
  key: string
  title: string
  coupons: MyCoupon[]
}

const shopNames = computed<Record<string, string>>(() =>
  Object.fromEntries((cart.value?.groups ?? []).map((g) => [g.shopId, g.shopName])),
)

/**
 * 券按「平台券 / 各店券」分组。
 *
 * ★ 分组不是为了好看：**同一个冲突组里只能用一张**（后端 `_conflict_group` 的
 *   上限就是 1，规则表里 COUPON_PLATFORM×COUPON_PLATFORM 也是不可叠加）。
 *   以前所有券堆成一排随便多选，选了两张只减一张 —— 看着像"叠加坏了"，
 *   实际是规则如此。按组摆出来，规则自己就说明白了。
 */
const couponGroups = computed<CouponGroup[]>(() => {
  const groups: CouponGroup[] = []
  const platform = payableCoupons.value.filter((c) => c.shopId === '0')
  if (platform.length > 0) groups.push({ key: 'platform', title: '平台券', coupons: platform })

  const byShop = new Map<string, MyCoupon[]>()
  for (const c of payableCoupons.value) {
    if (c.shopId === '0') continue
    const list = byShop.get(c.shopId) ?? []
    list.push(c)
    byShop.set(c.shopId, list)
  }
  for (const [shopId, list] of byShop) {
    groups.push({
      key: `shop:${shopId}`,
      title: `店铺券 · ${shopNames.value[shopId] ?? '店铺'}`,
      coupons: list,
    })
  }
  return groups
})

/**
 * 真正生效的券 id。
 *
 * ★ 高亮以**算价结果**为准，不是以"用户点过哪些"为准 —— 点过的券可能门槛不够
 *   没生效，那种情况下高亮它等于骗人。这样"看到的"和"减掉的"永远一致。
 */
const appliedCouponIds = computed(() => {
  const ids = new Set<string>()
  for (const d of calc.value?.discounts ?? []) {
    if (d.sourceType === COUPON_PLATFORM || d.sourceType === COUPON_SHOP) ids.add(d.sourceId)
  }
  return ids
})

const currentAddress = computed(() => addresses.value.find((a) => a.id === addressId.value) ?? null)

async function recalc(): Promise<void> {
  if (lines.value.length === 0) {
    calc.value = null
    return
  }
  try {
    calc.value = await calcPrice({
      items: lines.value.map((i) => ({ skuId: i.skuId, num: i.num })),
      couponCodeIds: selectedCoupons.value,
      // ★ 必须带地址：运费按收货地区算，不带的话运费恒为 0
      addressId: addressId.value || undefined,
    })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '算价失败')
    calc.value = null
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    // 顺序执行：共享同一个 axios 实例，并发没必要
    cart.value = await fetchCart()
    myCoupons.value = await fetchMyCoupons()
    addresses.value = await listAddresses()
    // 默认选中默认地址，没有默认就取第一个
    const preferred = addresses.value.find((a) => a.isDefault) ?? addresses.value[0]
    addressId.value = preferred?.id ?? ''
    await recalc()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载失败')
  } finally {
    loading.value = false
  }
}

/**
 * 选券：**组内单选**，再点一次 = 这一组不用券。
 *
 * ★ 组内只能有一张生效（后端 `_conflict_group` 的上限就是 1，规则表里
 *   COUPON_PLATFORM×COUPON_PLATFORM 也是不可叠加）。跨组（平台券 + 店铺券）
 *   可以叠加，引擎会分别按作用域分摊。
 * ★ 空列表 = **不用券**：后端的 `_load_coupons` 收到空 id 列表就直接不加载任何券，
 *   所以"取消勾选"是真的取消，不会变成"后端替我挑一张"。
 */
async function pickCoupon(c: MyCoupon, group: CouponGroup): Promise<void> {
  const groupIds = new Set(group.coupons.map((x) => x.id))
  const others = selectedCoupons.value.filter((id) => !groupIds.has(id))
  const picked = selectedCoupons.value.includes(c.id)
  selectedCoupons.value = picked ? others : [...others, c.id]
  await recalc()
}

/** 换收货地址要重算运费 —— 不同省份的首重/续重可能不同 */
async function pickAddress(id: string): Promise<void> {
  if (id === addressId.value) return
  addressId.value = id
  await recalc()
}

/**
 * 提交订单。
 *
 * ★ 幂等键在**发起前生成一次**并持有：网络卡住重试时要沿用同一个键，
 *   否则等于没有幂等，会下出两个订单、扣两次库存。
 */
async function onCheckout(): Promise<void> {
  if (lines.value.length === 0) return
  if (!addressId.value) {
    ElMessage.warning('请先选择收货地址')
    return
  }

  const key = newIdempotencyKey()
  submitting.value = true
  try {
    const order = await createOrder(
      {
        items: lines.value.map((i) => ({ skuId: i.skuId, num: i.num })),
        addressId: addressId.value,
        couponCodeIds: selectedCoupons.value,
      },
      key,
    )

    // 下单成功后把已购买的行从购物车里摘掉。放在这里是**尽力而为**：
    // 摘不掉不影响订单已经成立，用户可以自己去购物车清
    const orderedSkus = lines.value.map((i) => i.skuId)
    try {
      await removeCartItems(orderedSkus)
      await cartStore.refresh()
    } catch {
      // 忽略：购物车清理失败不该让用户以为下单失败了
    }

    // 直接建支付单并跳到收银台，省掉"去订单详情再点支付"这一步
    const payment = await createPayment(order.orderMainNo)
    ElMessage.success('下单成功')
    void router.replace({ name: 'payment', params: { payNo: payment.payNo } })
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '下单失败')
    // 价格/库存变了就重算一次，让用户看到最新的数字再决定
    await recalc()
  } finally {
    submitting.value = false
  }
}

/** 券不可用的原因由后端给出（含"还差 ¥X"），直接展示 */
const unavailable = computed(() => calc.value?.unavailableCoupons ?? [])

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: '/checkout' } })
    return
  }
  await load()
})
</script>

<template>
  <div v-loading="loading" class="page">
    <el-empty v-if="!loading && lines.length === 0" description="没有待结算的商品">
      <el-button type="primary" @click="router.push({ name: 'cart' })">去购物车</el-button>
    </el-empty>

    <template v-else-if="calc">
      <!-- 收货地址。**必须先选**：运费按地区算，不选就算不出运费 -->
      <section class="panel">
        <header class="panel-head">收货地址</header>
        <div v-if="addresses.length === 0" class="empty-line">
          还没有收货地址，<RouterLink to="/account" class="link">去添加</RouterLink>
        </div>
        <div v-else class="addr-list">
          <button
            v-for="a in addresses"
            :key="a.id"
            type="button"
            class="addr"
            :class="{ picked: a.id === addressId }"
            @click="pickAddress(a.id)"
          >
            <span class="addr-name">{{ a.receiverName }}</span>
            <span class="addr-phone tnum">{{ a.phone }}</span>
            <span class="addr-detail">
              {{ a.province }}{{ a.city }}{{ a.district }}{{ a.detail }}
            </span>
            <span v-if="a.isDefault" class="addr-tag">默认</span>
          </button>
        </div>
      </section>

      <!-- 商品清单 + 每行的优惠分摊 -->
      <section class="panel">
        <header class="panel-head">商品清单</header>
        <ul class="items">
          <li v-for="line in calc.items" :key="line.skuId" class="item">
            <div class="cover">
              <img :src="line.coverImage" :alt="line.title" @error="onImageError" />
            </div>
            <div class="info">
              <h3 class="title">{{ line.title }}</h3>
              <p class="spec">{{ line.specText || '—' }}</p>
              <!-- 分摊明细：退款时按这个算每行退多少，所以必须让用户看得到 -->
              <p v-if="line.allocations.length" class="allocs">
                <span v-for="a in line.allocations" :key="a.sourceName" class="alloc tnum">
                  {{ a.sourceName }} -¥{{ formatYuan(a.amount) }}
                </span>
              </p>
            </div>
            <div class="price-cell">
              <span class="unit tnum">¥{{ formatYuan(line.unitPrice) }}</span>
              <span class="qty">×{{ line.num }}</span>
            </div>
            <div class="amount-cell">
              <span v-if="line.discountAmount > 0" class="was tnum">
                ¥{{ formatYuan(line.amount) }}
              </span>
              <span class="final tnum">¥{{ formatYuan(line.payableAmount) }}</span>
            </div>
          </li>
        </ul>
      </section>

      <!-- 选券 -->
      <section class="panel">
        <header class="panel-head">优惠券</header>
        <div v-if="payableCoupons.length === 0" class="empty-line">
          没有可用券，<RouterLink to="/coupons" class="link">去领券</RouterLink>
        </div>
        <div v-else class="coupon-groups">
          <!-- 按组摆：同一组只能用一张（平台券一张、每店券一张），跨组可叠加 -->
          <div v-for="g in couponGroups" :key="g.key" class="coupon-group">
            <div class="group-head">
              <span class="group-title">{{ g.title }}</span>
              <span v-if="g.coupons.length > 1" class="group-hint">选一张</span>
            </div>
            <div class="coupons">
              <button
                v-for="c in g.coupons"
                :key="c.id"
                type="button"
                class="coupon-chip"
                :class="{ picked: appliedCouponIds.has(c.id) }"
                @click="pickCoupon(c, g)"
              >
                <span class="chip-value tnum">
                  {{ c.couponType === 2 ? `${(c.discountValue / 1000).toFixed(1).replace(/\.0$/, '')}折` : `¥${formatYuan(c.discountValue)}` }}
                </span>
                <span class="chip-name">{{ c.name }}</span>
              </button>
            </div>
          </div>
        </div>

        <!-- 不可用的券要说清原因，尤其"还差多少"——那能促使用户凑单 -->
        <div v-if="unavailable.length" class="unavailable">
          <div v-for="u in unavailable" :key="u.codeId" class="unavailable-row">
            <span class="u-name">{{ u.name }}</span>
            <span class="u-reason">{{ u.reasonText }}</span>
          </div>
        </div>
      </section>

      <!-- 金额明细 -->
      <section class="panel">
        <header class="panel-head">金额明细</header>
        <dl class="breakdown">
          <div class="row">
            <dt>商品总额</dt>
            <dd class="tnum">¥{{ formatYuan(calc.totalAmount) }}</dd>
          </div>
          <div v-if="calc.itemDiscount > 0" class="row">
            <dt>单品优惠</dt>
            <dd class="tnum minus">-¥{{ formatYuan(calc.itemDiscount) }}</dd>
          </div>
          <div v-if="calc.shopDiscount > 0" class="row">
            <dt>店铺优惠</dt>
            <dd class="tnum minus">-¥{{ formatYuan(calc.shopDiscount) }}</dd>
          </div>
          <div v-if="calc.platformDiscount > 0" class="row">
            <dt>平台优惠</dt>
            <dd class="tnum minus">-¥{{ formatYuan(calc.platformDiscount) }}</dd>
          </div>
          <div class="row">
            <dt>运费</dt>
            <dd class="tnum">¥{{ formatYuan(calc.freight) }}</dd>
          </div>

          <!-- 本期未实现的两项后端会标注，直接透传给用户，别让人以为算漏了 -->
          <div v-for="n in calc.notices" :key="n" class="notice">{{ n }}</div>

          <div class="row total">
            <dt>应付</dt>
            <dd class="tnum">¥{{ formatYuan(calc.payableAmount) }}</dd>
          </div>
        </dl>
      </section>

      <footer class="submit-bar">
        <div class="summary">
          <template v-if="currentAddress">
            <span class="ship-to">寄往 {{ currentAddress.city }}{{ currentAddress.district }}</span>
            <span class="dot">·</span>
          </template>
          共 <span class="tnum strong">{{ calc.items.reduce((s, i) => s + i.num, 0) }}</span> 件， 应付
          <span class="tnum payable">¥{{ formatYuan(calc.payableAmount) }}</span>
        </div>
        <el-button
          type="primary"
          size="large"
          :loading="submitting"
          :disabled="!addressId"
          @click="onCheckout"
        >
          {{ addressId ? '提交订单' : '请先选择地址' }}
        </el-button>
      </footer>
    </template>
  </div>
</template>

<style scoped>
/* 只用语义 token，见 docs/17-frontend-design-system.md */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding-bottom: 80px;
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.panel-head {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

/* ---------- 收货地址 ---------- */

.addr-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-3);
  padding: var(--space-4);
}

.addr {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2) var(--space-3);
  padding: var(--space-3) var(--space-4);
  text-align: left;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-surface);
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease-out),
    background-color var(--dur-fast) var(--ease-out);
}

.addr:hover {
  border-color: var(--color-accent);
}

.addr.picked {
  border-color: var(--color-accent);
  background: var(--color-accent-soft);
}

.addr-name {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.addr-phone {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.addr-detail {
  flex: 1 1 100%;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  line-height: var(--leading-snug);
}

.addr-tag {
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-size: var(--text-xs);
}

/* ---------- 商品行 ---------- */

.items {
  list-style: none;
  margin: 0;
  padding: 0;
}

.item {
  display: grid;
  grid-template-columns: 72px minmax(160px, 1fr) 120px 130px;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.item:last-child {
  border-bottom: none;
}

.cover {
  width: 72px;
  height: 72px;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--media-bg);
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.info {
  min-width: 0;
}

.title {
  font-size: var(--text-base);
  color: var(--color-text);
  line-height: var(--leading-snug);
}

.spec {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.allocs {
  margin-top: var(--space-2);
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

/* 优惠金额用成功色，与价格的交易色区分开 */
.alloc {
  font-size: var(--text-xs);
  color: var(--color-success);
}

.price-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.unit {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.qty {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.amount-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}

.was {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
  text-decoration: line-through;
}

.final {
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

/* ---------- 选券 ---------- */

/* 一组 = 一个冲突组（平台券 / 某个店的券）。组内只能选一张，组间可叠加。 */
.coupon-groups {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-4);
}

.group-head {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}

.group-title {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.group-hint {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.coupons {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.coupon-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-4);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-surface);
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease-out),
    background-color var(--dur-fast) var(--ease-out);
}

.coupon-chip:hover {
  border-color: var(--color-accent);
}

/* 选中的券用强调色 —— 跟着皮肤走 */
.coupon-chip.picked {
  border-color: var(--color-accent);
  background: var(--color-accent-soft);
}

.chip-value {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-accent);
}

.chip-name {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.empty-line {
  padding: var(--space-4);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.link {
  color: var(--color-accent);
}

.unavailable {
  border-top: 1px dashed var(--color-border);
  padding: var(--space-3) var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.unavailable-row {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  font-size: var(--text-xs);
}

.u-name {
  color: var(--color-text-tertiary);
}

/* 「还差 ¥X」用警示色，是转化的关键提示 */
.u-reason {
  color: var(--color-warning);
}

/* ---------- 金额明细 ---------- */

.breakdown {
  margin: 0;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: var(--text-base);
  color: var(--color-text-secondary);
}

.minus {
  color: var(--color-success);
}

.notice {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.total {
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.total dd {
  font-size: var(--text-2xl);
  color: var(--color-price);
}

/* ---------- 提交条 ---------- */

.submit-bar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: var(--z-sticky);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-4);
  padding: var(--space-3) var(--layout-gutter);
  background: var(--color-bg-surface);
  border-top: 1px solid var(--color-border);
}

.summary {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.ship-to {
  color: var(--color-text-tertiary);
}

.dot {
  margin: 0 var(--space-2);
  color: var(--color-text-placeholder);
}

.strong {
  color: var(--color-text);
  font-weight: var(--weight-medium);
}

.payable {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

@media (max-width: 900px) {
  .item {
    grid-template-columns: 60px 1fr auto;
    grid-template-areas:
      "cover info amount"
      "cover price amount";
    gap: var(--space-3);
  }

  .cover {
    width: 60px;
    height: 60px;
    grid-area: cover;
  }

  .info {
    grid-area: info;
  }

  .price-cell {
    grid-area: price;
    align-items: flex-start;
    flex-direction: row;
    gap: var(--space-2);
  }

  .amount-cell {
    grid-area: amount;
  }

  .submit-bar {
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-2);
  }

  .summary {
    text-align: right;
  }
}
</style>
