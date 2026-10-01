import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('@/views/HomeView.vue'),
    },
    // 后台路由随模块实现逐步补充：
    //   商品管理 /spus、库存 /inventory、订单 /orders、发货 /deliveries、
    //   售后 /aftersales、优惠券 /coupons、运费模板 /freight-templates、
    //   评价审核 /reviews、对账差异 /reconcile、告警 /alerts
  ],
})

export default router
