import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { CHUNK_RELOAD_KEY } from '@/utils/storageKeys'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'home',
    component: () => import('@/views/ProductListView.vue'),
  },
  {
    path: '/products/:spuId',
    name: 'product-detail',
    component: () => import('@/views/ProductDetailView.vue'),
  },
  {
    // 店铺页。入口在商品详情页那块店铺信息上 —— 不放在商品卡上：
    // 卡片整体已经是一个 <a>，里面再嵌一个链接是无效 HTML
    path: '/shops/:shopId',
    name: 'shop',
    component: () => import('@/views/ShopView.vue'),
  },
  {
    path: '/cart',
    name: 'cart',
    component: () => import('@/views/CartView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/checkout',
    name: 'checkout',
    component: () => import('@/views/CheckoutView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/orders',
    name: 'orders',
    component: () => import('@/views/OrderListView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/orders/:orderMainNo',
    name: 'order-detail',
    component: () => import('@/views/OrderDetailView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/pay/:payNo',
    name: 'payment',
    component: () => import('@/views/PaymentView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/reviews',
    name: 'reviews',
    component: () => import('@/views/ReviewListView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 发评价与追评共用一页：带 ?followUp=<reviewId> 就是追评模式
    path: '/reviews/submit/:orderItemId?',
    name: 'review-submit',
    component: () => import('@/views/ReviewSubmitView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/refunds',
    name: 'refunds',
    component: () => import('@/views/RefundListView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/refunds/apply/:orderSubNo',
    name: 'refund-apply',
    component: () => import('@/views/RefundApplyView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/refunds/:refundNo',
    name: 'refund-detail',
    component: () => import('@/views/RefundDetailView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/coupons',
    name: 'coupons',
    component: () => import('@/views/CouponCenterView.vue'),
    // 券中心要显示"你已领几张"，接口需要登录，路由也就跟着要
    meta: { requiresAuth: true },
  },
  {
    // 客服会话列表。带查询参数（shopId / orderMainNo / refundNo）时**直接开会话**
    // 并跳到那一条 —— 商品页 / 订单页 / 售后页的「联系客服」入口都用这个深链接。
    path: '/support',
    name: 'support',
    component: () => import('@/views/SupportView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/support/:ticketNo',
    name: 'support-ticket',
    component: () => import('@/views/SupportTicketView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/notifications',
    name: 'notifications',
    component: () => import('@/views/NotificationView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/my/coupons',
    name: 'my-coupons',
    component: () => import('@/views/MyCouponsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { guestOnly: true },
  },
  {
    path: '/account',
    name: 'account',
    component: () => import('@/views/AccountView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/status',
    name: 'status',
    component: () => import('@/views/StatusView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 首次进入时尝试恢复登录态（页面刷新后内存里的 user 会丢）
  if (auth.user === null) await auth.restore()

  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.guestOnly && auth.isLoggedIn) {
    return { name: 'home' }
  }
  return true
})

/**
 * ★ 资源拉不到就**整页刷新一次**。
 *
 * 来由：每次发布都会换掉带 hash 的文件名，而**发布前就打开着的标签页**手里还是旧的
 * index.html —— 它按需去拉路由 chunk 时那些名字已经 404 了，表现就是**点导航没反应**
 * （控制台里是 "Failed to fetch dynamically imported module"）。用户报过这个。
 *
 * 三条纪律：
 * 1. 只认「拉不到模块」这一类。别的导航错误（守卫里抛的、业务异常）不该被吞成一次刷新。
 * 2. 一分钟内**只刷一次**（时间戳记在 sessionStorage）：资源真缺失时无限刷新比按钮
 *    点不动更糟 —— 至少要让人能看见"它坏了"。
 * 3. 带着用户原本要去的地址刷（`router.resolve(to).href` —— 那个 `href` 才是
 *    **带 base 的完整地址**，`to.href` 并不存在，`fullPath` 又会丢掉 base）。
 */
const CHUNK_RELOAD_COOLDOWN_MS = 60_000
// Chrome / Edge 是前两句；Firefox 与部分浏览器是后两句
const CHUNK_LOAD_ERROR =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i

router.onError((error, to) => {
  const message = error instanceof Error ? error.message : String(error)
  if (!CHUNK_LOAD_ERROR.test(message)) return
  const last = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY) ?? 0)
  if (Date.now() - last < CHUNK_RELOAD_COOLDOWN_MS) return
  sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()))
  window.location.assign(router.resolve(to).href)
})

export default router
