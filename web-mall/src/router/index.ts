import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

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

export default router
