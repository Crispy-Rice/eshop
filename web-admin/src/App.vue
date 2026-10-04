<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { setUnauthorizedHandler } from '@/api/http'
import { MALL_APP_URL } from '@/utils/siblingApp'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

/** 营销中心只有平台运营能进：后端接口只放给 admin / finance */
const isAdmin = computed(() => auth.user?.role === 'admin' || auth.user?.role === 'finance')

setUnauthorizedHandler(() => {
  auth.clearLocal()
  ElMessage.warning('登录已过期，请重新登录')
  void router.push({ name: 'login', query: { redirect: route.fullPath } })
})

onMounted(() => auth.restore())

async function onLogout(): Promise<void> {
  await auth.logout()
  ElMessage.success('已退出登录')
  void router.push({ name: 'login' })
}
</script>

<template>
  <div class="layout">
    <header class="header">
      <div class="header-inner">
        <RouterLink to="/" class="brand">
          <span class="brand-mark">eshop</span>
          <span class="brand-sub">商家后台</span>
        </RouterLink>

        <nav v-if="auth.isLoggedIn" class="nav">
          <RouterLink to="/" class="nav-link">我的商品</RouterLink>
          <RouterLink to="/products/new" class="nav-link">发布商品</RouterLink>
          <RouterLink to="/orders" class="nav-link">订单</RouterLink>
          <RouterLink to="/aftersales" class="nav-link">售后</RouterLink>
          <RouterLink to="/reviews" class="nav-link">评价</RouterLink>
          <RouterLink v-if="isAdmin" to="/promotions" class="nav-link">营销</RouterLink>
          <RouterLink to="/inventory" class="nav-link">库存</RouterLink>
          <RouterLink to="/freight" class="nav-link">运费</RouterLink>
        </nav>

        <div class="spacer" />

        <!-- 同一个账号也能逛商城（卖家账号买东西是常规做法），给个顺手的入口 -->
        <a :href="MALL_APP_URL" class="mall-link">去商城</a>

        <div v-if="auth.isLoggedIn" class="user">
          <span class="nickname">{{ auth.user?.nickname }}</span>
          <el-tag :type="auth.user?.shopId ? 'success' : 'info'" size="small" disable-transitions>
            {{ auth.user?.shopId ? '商家' : '未开店' }}
          </el-tag>
          <el-button link type="primary" @click="onLogout">退出</el-button>
        </div>
        <RouterLink v-else to="/login">
          <el-button type="primary" size="small">登录</el-button>
        </RouterLink>
      </div>
    </header>

    <main class="content">
      <RouterView />
    </main>

    <footer class="footer">
      <div class="footer-inner">
        <span class="footer-copy">eshop 商家 / 运营后台 · 演示环境</span>
        <RouterLink to="/status" class="footer-link">系统状态</RouterLink>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

/* ---------- 头部 ---------- */

.header {
  position: sticky;
  top: 0;
  z-index: var(--z-header);
  background: var(--color-bg-surface);
  border-bottom: 1px solid var(--color-border);
}

/* 比前台矮 4px：后台每一像素都留给内容 */
.header-inner {
  height: var(--header-h);
  max-width: var(--layout-max);
  margin: 0 auto;
  padding: 0 var(--layout-gutter);
  display: flex;
  align-items: center;
  gap: var(--space-6);
}

.brand {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  flex: 0 0 auto;
}

.brand-mark {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  letter-spacing: -0.01em;
  color: var(--color-accent);
}

.brand-sub {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.nav {
  display: flex;
  gap: var(--space-5);
  flex: 0 0 auto;
}

.nav-link {
  position: relative;
  font-size: var(--text-base);
  color: var(--color-text-secondary);
  transition: color var(--dur-fast) var(--ease-out);
}

.nav-link:hover {
  color: var(--color-text);
}

.nav-link.router-link-exact-active {
  color: var(--color-text);
  font-weight: var(--weight-medium);
}

/* 当前模块用一条短下划线标记位置 */
.nav-link.router-link-exact-active::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: -19px;
  height: 2px;
  background: var(--color-accent);
  border-radius: var(--radius-pill);
}

.spacer {
  flex: 1;
}

/* 跨端入口：贴着用户区但不参与它内部的紧凑排布 */
.mall-link {
  flex: 0 0 auto;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  transition: color var(--dur-fast) var(--ease-out);
}

.mall-link:hover {
  color: var(--color-accent);
}

.user {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex: 0 0 auto;
}

.nickname {
  font-size: var(--text-base);
  color: var(--color-text);
}

/* ---------- 内容 ---------- */

.content {
  flex: 1;
  width: 100%;
  max-width: var(--layout-max);
  margin: 0 auto;
  padding: var(--space-5) var(--layout-gutter) var(--space-8);
}

/* ---------- 页脚 ---------- */

.footer {
  border-top: 1px solid var(--color-border);
  background: var(--color-bg-surface);
}

.footer-inner {
  max-width: var(--layout-max);
  margin: 0 auto;
  padding: var(--space-4) var(--layout-gutter);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}

.footer-copy,
.footer-link {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.footer-link:hover {
  color: var(--color-text-secondary);
}

@media (max-width: 768px) {
  .nav {
    display: none;
  }
}
</style>
