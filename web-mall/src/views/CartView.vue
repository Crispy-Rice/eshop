<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  CART_VALID,
  clearInvalidItems,
  fetchCart,
  removeCartItems,
  selectCartItems,
  updateCartNum,
  type Cart,
  type CartItem,
} from '@/api/cart'
import { isBizError } from '@/api/errors'
import { useCartStore } from '@/stores/cart'
import { useAuthStore } from '@/stores/auth'
import { formatYuan } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()
const cartStore = useCartStore()
const router = useRouter()

const cart = ref<Cart | null>(null)
const loading = ref(false)
/** 正在改数量的 skuId，用于禁用该行的步进器避免连点 */
const busySku = ref<string | null>(null)

const isEmpty = computed(
  () => !cart.value || (cart.value.groups.length === 0 && cart.value.invalidItems.length === 0),
)

async function load(): Promise<void> {
  loading.value = true
  try {
    cart.value = await fetchCart()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载购物车失败')
  } finally {
    loading.value = false
  }
  // ★ 角标跟着一起刷。这一页的每个改动（移除/改数量/勾选/清失效）最后都落到
  //   load()，在这里刷一次就全覆盖；否则删完商品角标还是旧数字，得刷页面才对。
  //   失败路径（catch 里那次 load）也会走到，正好把角标拉回真实值。
  await cartStore.refresh()
}

async function changeNum(item: CartItem, num: number): Promise<void> {
  if (num === item.num) return
  busySku.value = item.skuId
  try {
    await updateCartNum(item.skuId, num)
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '修改数量失败')
    await load() // 失败时拉回真实值，避免界面停在错误的数字上
  } finally {
    busySku.value = null
  }
}

async function toggleSelected(item: CartItem): Promise<void> {
  try {
    await selectCartItems([item.skuId], !item.selected)
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  }
}

/** 整店全选/全不选 */
async function toggleShop(items: CartItem[]): Promise<void> {
  const allSelected = items.filter((i) => i.status === CART_VALID).every((i) => i.selected)
  try {
    await selectCartItems(
      items.map((i) => i.skuId),
      !allSelected,
    )
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  }
}

async function removeItems(items: CartItem[]): Promise<void> {
  const label = items.length === 1 ? `「${items[0]?.title}」` : `这 ${items.length} 件商品`
  try {
    await ElMessageBox.confirm(`确定要从购物车移除${label}吗？`, '移除商品', { type: 'warning' })
  } catch {
    return // 用户取消
  }
  try {
    await removeCartItems(items.map((i) => i.skuId))
    ElMessage.success('已移除')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '移除失败')
  }
}

async function clearInvalid(): Promise<void> {
  try {
    await clearInvalidItems()
    ElMessage.success('已清除失效商品')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '清除失败')
  }
}

function onCheckout(): void {
  // 结算页已经能算价（含逐级优惠与分摊），但下单（trade）还没实现
  void router.push({ name: 'checkout' })
}

onMounted(async () => {
  if (auth.user === null) await auth.restore()
  if (!auth.isLoggedIn) {
    void router.push({ name: 'login', query: { redirect: '/cart' } })
    return
  }
  await load()
})
</script>

<template>
  <div v-loading="loading" class="page">
    <div v-if="isEmpty && !loading" class="empty">
      <el-empty description="购物车还是空的">
        <el-button type="primary" @click="router.push({ name: 'home' })">去逛逛</el-button>
      </el-empty>
    </div>

    <template v-else-if="cart">
      <!-- 按店铺分块。每块是一个结算单位，页脚汇总所有块。 -->
      <section v-for="group in cart.groups" :key="group.shopId" class="shop-block">
        <header class="shop-head">
          <el-checkbox
            :model-value="
              group.items.filter((i) => i.status === CART_VALID).every((i) => i.selected) &&
              group.items.some((i) => i.status === CART_VALID)
            "
            @change="toggleShop(group.items)"
          />
          <span class="shop-name">{{ group.shopName }}</span>
          <span v-if="group.selectedCount > 0" class="shop-sel">
            已选 {{ group.selectedCount }} 件
          </span>
        </header>

        <ul class="items">
          <li v-for="item in group.items" :key="item.skuId" class="item">
            <el-checkbox
              :model-value="item.selected"
              :disabled="item.status !== CART_VALID"
              @change="toggleSelected(item)"
            />

            <div class="cover">
              <img :src="item.coverImage" :alt="item.title" @error="onImageError" />
            </div>

            <div class="info">
              <h3 class="title" :title="item.title">{{ item.title }}</h3>
              <p class="spec">{{ item.specText || '—' }}</p>

              <div class="tags">
                <el-tag v-if="item.status === 4" type="warning" size="small" disable-transitions>
                  无货
                </el-tag>
                <span v-if="item.priceDown > 0" class="price-down tnum">
                  较加购时降 ¥{{ formatYuan(item.priceDown) }}
                </span>
              </div>
            </div>

            <div class="price-cell">
              <span class="price tnum">¥{{ formatYuan(item.price) }}</span>
              <!-- 降价时把原价划掉，让"降了多少"有对比 -->
              <span v-if="item.priceDown > 0" class="price-was tnum">
                ¥{{ formatYuan(item.priceSnapshot) }}
              </span>
            </div>

            <div class="num-cell">
              <el-input-number
                :model-value="item.num"
                :min="1"
                :max="200"
                size="small"
                :disabled="item.status !== CART_VALID || busySku === item.skuId"
                @change="(v: number | undefined) => changeNum(item, v ?? 1)"
              />
            </div>

            <div class="amount-cell tnum">¥{{ formatYuan(item.itemAmount) }}</div>

            <el-button link type="danger" @click="removeItems([item])">移除</el-button>
          </li>
        </ul>
      </section>

      <!-- 失效商品单独一块：不参与合计，只提供"移除"和"全部清除" -->
      <section v-if="cart.invalidItems.length > 0" class="shop-block invalid-block">
        <header class="shop-head">
          <span class="shop-name">失效商品</span>
          <span class="shop-sel">{{ cart.invalidItems.length }} 件</span>
          <div class="spacer" />
          <el-button size="small" @click="clearInvalid">清除全部失效</el-button>
        </header>

        <ul class="items">
          <li v-for="item in cart.invalidItems" :key="item.skuId" class="item is-invalid">
            <div class="cover">
              <img :src="item.coverImage" :alt="item.title" @error="onImageError" />
            </div>
            <div class="info">
              <h3 class="title">{{ item.title }}</h3>
              <p class="spec">{{ item.specText || '—' }}</p>
            </div>
            <el-tag type="info" size="small" disable-transitions>{{ item.statusText }}</el-tag>
            <div class="spacer" />
            <el-button link type="danger" @click="removeItems([item])">移除</el-button>
          </li>
        </ul>
      </section>
    </template>

    <!-- 结算条固定在底部：合计随时可见 -->
    <footer v-if="cart && !isEmpty" class="checkout-bar">
      <div class="checkout-inner">
        <span class="total-label">已选</span>
        <span class="total-count tnum">{{ cart.totalCount }}</span>
        <span class="total-label">件，合计</span>
        <span class="total-amount tnum">¥{{ formatYuan(cart.totalAmount) }}</span>
        <div class="spacer" />
        <el-button
          type="primary"
          size="large"
          :disabled="cart.totalCount === 0"
          @click="onCheckout"
        >
          结算
        </el-button>
      </div>
    </footer>
  </div>
</template>

<style scoped>
/* 只用语义 token，见 docs/17-frontend-design-system.md */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  /* 给固定结算条留出空间，否则最后一行被盖住 */
  padding-bottom: 72px;
}

.empty {
  padding: var(--space-10) 0;
}

.spacer {
  flex: 1;
}

/* --------------------------------------------------------------------------
 * 店铺分块
 * ------------------------------------------------------------------------*/

.shop-block {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.shop-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-subtle);
}

.shop-name {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.shop-sel {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.items {
  list-style: none;
  margin: 0;
  padding: 0;
}

/* 一行一件商品。用 grid 固定列宽，整列对齐比 flex 更稳 */
.item {
  display: grid;
  grid-template-columns: 24px 80px minmax(180px, 1fr) 110px 130px 110px 64px;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.item:last-child {
  border-bottom: none;
}

.item.is-invalid {
  grid-template-columns: 80px minmax(180px, 1fr) auto 1fr 64px;
  opacity: 0.72;
}

.cover {
  width: 80px;
  height: 80px;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--media-bg);
  flex: 0 0 auto;
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
  font-weight: var(--weight-normal);
  line-height: var(--leading-snug);
  color: var(--color-text);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.spec {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.tags {
  margin-top: var(--space-2);
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

/* 降价提示用成功色，与"涨价"区分开（涨价不提示） */
.price-down {
  font-size: var(--text-xs);
  color: var(--color-success);
}

.price-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}

.price {
  font-size: var(--text-md);
  color: var(--color-price);
  font-weight: var(--weight-medium);
}

/* 被划掉的原价：只在降价时出现 */
.price-was {
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
  text-decoration: line-through;
}

.num-cell {
  display: flex;
  justify-content: center;
}

.amount-cell {
  text-align: right;
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

/* --------------------------------------------------------------------------
 * 结算条
 * ------------------------------------------------------------------------*/

.checkout-bar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: var(--z-sticky);
  background: var(--color-bg-surface);
  border-top: 1px solid var(--color-border);
}

.checkout-inner {
  max-width: var(--layout-max);
  margin: 0 auto;
  padding: var(--space-3) var(--layout-gutter);
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.total-label {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.total-count {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

.total-amount {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-price);
}

/* --------------------------------------------------------------------------
 * 窄屏：一行放不下七列，改成两段式布局
 * ------------------------------------------------------------------------*/

@media (max-width: 900px) {
  .item,
  .item.is-invalid {
    grid-template-columns: 24px 64px 1fr auto;
    grid-template-areas:
      "check cover info info"
      ".     .     num  amount";
    gap: var(--space-3);
  }

  .cover {
    width: 64px;
    height: 64px;
    grid-area: cover;
  }

  .info {
    grid-area: info;
  }

  .num-cell {
    grid-area: num;
    justify-content: flex-start;
  }

  .amount-cell {
    grid-area: amount;
  }

  .price-cell {
    grid-column: 3 / -1;
    align-items: flex-start;
    flex-direction: row;
    gap: var(--space-2);
  }

  .item > .el-button,
  .item > .el-tag {
    grid-column: 4;
    justify-self: end;
  }
}
</style>
