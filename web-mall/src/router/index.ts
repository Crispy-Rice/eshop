import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('@/views/HomeView.vue'),
    },
    // 业务路由随模块实现逐步补充：
    //   商品详情 /spu/:spuId、购物车 /cart、结算 /checkout、
    //   订单 /orders、售后 /aftersales、我的 /me
  ],
})

export default router
