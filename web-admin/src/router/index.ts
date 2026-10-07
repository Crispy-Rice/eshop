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
    // 替换规格：**复用发布页**那个编辑器（规格组/取值/笛卡尔积那一套），
    // 同一组件靠 route.params.spuId 有无来区分"新建"与"替换"。
    // 不复用 /products/new 加 query —— 那会和"创建后跳编辑页"的逻辑打架。
    path: '/products/:spuId/specs',
    name: 'product-specs',
    component: () => import('@/views/ProductCreateView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/inventory',
    name: 'inventory',
    component: () => import('@/views/InventoryView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 仓库管理：地址、覆盖区域、默认仓。发货仓路由按这里的配置走
    path: '/warehouses',
    name: 'warehouses',
    component: () => import('@/views/WarehouseView.vue'),
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
    // 客服会话。商家看本店队列、平台运营看全部 —— 同一页按身份换端点
    path: '/support',
    name: 'support',
    component: () => import('@/views/SupportView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 智能客服（店小蜜）：开关 + 问答维护 + 今日计数。
    // ★ 端点全是**本店**范围（后端 caller.require_shop()），所以只有商家能进 ——
    //   让不让 AI 以本店的名义答买家是商户自己的事，运营进来会撞 403。
    path: '/shopbot',
    name: 'shopbot',
    component: () => import('@/views/ShopBotView.vue'),
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
    // 运营专属：后端的 /api/admin/spus 只放给 admin（不含 finance）
    path: '/audits',
    name: 'product-audits',
    component: () => import('@/views/ProductAuditView.vue'),
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
    // 运营专属：后端的 /api/admin/users 只放给 admin（不含 finance）——
    // 财务能看营销数据，但不该能封人或重置密码
    path: '/users',
    name: 'users',
    component: () => import('@/views/UserListView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 运营专属：后端的 /api/admin/banners 放给 admin / finance（与营销同角色）
    path: '/banners',
    name: 'banners',
    component: () => import('@/views/BannerView.vue'),
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
    // 店铺设置：改名 / LOGO / 简介。没开店直接敲 URL 进来会看到一句引导（后端回 404）
    path: '/shop',
    name: 'shop-settings',
    component: () => import('@/views/ShopSettingsView.vue'),
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

  /**
   * 平台运营账号没有店铺，`/`（我的商品）永远是空的 —— 一登录就看到一个空壳页面。
   * 直接落到审核队列，它才是运营每天要处理的入口。
   *
   * ★ 只对 admin 生效：`/audits` 依赖后端的 `/api/admin/spus`，那条只放给 admin
   *   （不含 finance），把财务也送过去只会撞 403。
   * ★ 带 `!shopId` 是双保险：平台账号本来就禁止开店，正常不会命中，
   *   但真出现"既是 admin 又有店铺"的账号时，不该把它从自己的商品页赶走。
   */
  const platformOnly = auth.user?.role === 'admin' && !auth.user?.shopId

  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.guestOnly && auth.isLoggedIn) {
    return platformOnly ? { name: 'product-audits' } : { name: 'products' }
  }
  // 点 logo、手敲 "/"、或登录后的默认跳转，都会走到这里
  if (to.name === 'products' && platformOnly) {
    return { name: 'product-audits' }
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
 *    **带 base 的完整地址**：后台挂在 `/admin/` 下，`to.href` 并不存在，
 *    而 `fullPath` 会丢掉 base）。
 */
const CHUNK_RELOAD_KEY = 'eshop.chunkReloadAt'
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
