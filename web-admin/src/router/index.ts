import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'products',
    component: () => import('@/views/ProductListView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/products/new',
    name: 'product-create',
    component: () => import('@/views/ProductCreateView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/products/:spuId',
    name: 'product-edit',
    component: () => import('@/views/ProductEditView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/inventory',
    name: 'inventory',
    component: () => import('@/views/InventoryView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/orders',
    name: 'orders',
    component: () => import('@/views/OrderListView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/aftersales',
    name: 'aftersales',
    component: () => import('@/views/AfterSaleView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/reviews',
    name: 'reviews',
    component: () => import('@/views/ReviewView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 运营专属（后端只放给 admin / finance）；商家进来会看到空状态而不是报错
    path: '/promotions',
    name: 'promotions',
    component: () => import('@/views/PromotionView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 运营专属：后端的 /api/admin/categories 只放给 admin（不含 finance）
    path: '/categories',
    name: 'categories',
    component: () => import('@/views/CategoryView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/inventory/flows',
    name: 'inventory-flows',
    component: () => import('@/views/InventoryFlowsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/freight',
    name: 'freight',
    component: () => import('@/views/FreightTemplateView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { guestOnly: true },
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
  if (auth.user === null) await auth.restore()

  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.guestOnly && auth.isLoggedIn) {
    return { name: 'products' }
  }
  return true
})

export default router
