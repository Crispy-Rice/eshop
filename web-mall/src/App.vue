<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import PromoStrip from '@/components/PromoStrip.vue'
import { setUnauthorizedHandler } from '@/api/http'
import { useTheme } from '@/composables/useTheme'
import { useAuthStore } from '@/stores/auth'
import { useCartStore } from '@/stores/cart'
import { ADMIN_APP_URL } from '@/utils/siblingApp'

const auth = useAuthStore()
const cart = useCartStore()
const router = useRouter()
const route = useRoute()
const { theme, themes, setTheme } = useTheme()

/**
 * 后台入口。只给真进得去的人显示 —— 买家点「商家后台」只会撞 403，
 * 不如等他在「我的」里开完店再出现（运营/财务则直接给运营后台）。
 */
const backendEntry = computed(() => {
  const user = auth.user
  if (!user) return null
  if (user.role === 'admin' || user.role === 'finance') {
    return { label: '运营后台', url: ADMIN_APP_URL }
  }
  return user.shopId ? { label: '商家后台', url: ADMIN_APP_URL } : null
})

// 令牌失效时由 http 层回调：清状态并跳登录
setUnauthorizedHandler(() => {
  auth.clearLocal()
  cart.reset()
  ElMessage.warning('登录已过期，请重新登录')
  void router.push({ name: 'login', query: { redirect: route.fullPath } })
})

onMounted(async () => {
  await auth.restore()
  // 角标要在登录态恢复之后再拉，否则未登录时白跑一次
  await cart.refresh()
})

async function onLogout(): Promise<void> {
  await auth.logout()
  cart.reset()
  ElMessage.success('已退出登录')
  void router.push({ name: 'home' })
}
</script>

<template>
  <div class="layout">
    <header class="header">
      <div class="header-inner">
        <RouterLink to="/" class="brand">
          <span class="brand-mark">eshop</span>
          <span class="brand-sub">商城</span>
        </RouterLink>

        <nav class="nav">
          <RouterLink to="/" class="nav-link">全部商品</RouterLink>
          <RouterLink to="/coupons" class="nav-link">领券</RouterLink>
          <RouterLink v-if="auth.isLoggedIn" to="/cart" class="nav-link">
            购物车
            <span v-if="cart.count > 0" class="badge tnum">{{ cart.count }}</span>
          </RouterLink>
          <RouterLink v-if="auth.isLoggedIn" to="/orders" class="nav-link">我的订单</RouterLink>
          <RouterLink v-if="auth.isLoggedIn" to="/reviews" class="nav-link">评价</RouterLink>
          <RouterLink v-if="auth.isLoggedIn" to="/refunds" class="nav-link">退款/售后</RouterLink>
          <RouterLink v-if="auth.isLoggedIn" to="/account" class="nav-link">我的</RouterLink>
        </nav>

        <div class="spacer" />

        <!-- 皮肤切换：只改 <html data-theme>，货架结构一点不动 -->
        <div class="themes" role="group" aria-label="切换皮肤">
          <button
            v-for="t in themes"
            :key="t.id"
            type="button"
            class="theme-btn"
            :class="{ active: t.id === theme }"
            :title="`切换到「${t.label}」皮肤`"
            :aria-pressed="t.id === theme"
            @click="setTheme(t.id)"
          >
            <span class="theme-dot" :style="{ background: t.swatch }" />
            <span class="theme-label">{{ t.label }}</span>
          </button>
        </div>

        <div class="user">
          <template v-if="auth.isLoggedIn">
            <a v-if="backendEntry" :href="backendEntry.url" class="backend-link">
              {{ backendEntry.label }}
            </a>
            <span class="nickname">{{ auth.user?.nickname }}</span>
            <el-button link type="primary" @click="onLogout">退出</el-button>
          </template>
          <RouterLink v-else to="/login">
            <el-button type="primary" size="small">登录 / 注册</el-button>
          </RouterLink>
        </div>
      </div>
    </header>

    <PromoStrip />

    <main class="content">
      <RouterView />
    </main>

    <footer class="footer">
      <div class="footer-inner">
        <span class="footer-copy">eshop · 演示环境</span>
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

/* 品牌色跟着皮肤走，是换肤最明显的一处信号 */
.brand-mark {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  letter-spacing: -0.02em;
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
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-base);
  color: var(--color-text-secondary);
  transition: color var(--dur-fast) var(--ease-out);
}

/* 角标：跟着强调色走，换肤时自动变色 */
.badge {
  min-width: 16px;
  padding: 0 4px;
  border-radius: var(--radius-pill);
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-size: var(--text-xs);
  line-height: 16px;
  text-align: center;
}

.nav-link:hover {
  color: var(--color-text);
}

.nav-link.router-link-exact-active {
  color: var(--color-text);
  font-weight: var(--weight-medium);
}

/* 当前页用一条短下划线标记，比加粗更清晰 */
.nav-link.router-link-exact-active::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: -21px;
  height: 2px;
  background: var(--color-accent);
  border-radius: var(--radius-pill);
}

.spacer {
  flex: 1;
}

/* ---------- 皮肤切换 ---------- */

.themes {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  flex: 0 0 auto;
}

.theme-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-3);
  border: none;
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out);
}

.theme-btn:hover {
  color: var(--color-text);
}

.theme-btn.active {
  background: var(--color-bg-surface);
  color: var(--color-text);
  font-weight: var(--weight-medium);
  box-shadow: var(--shadow-xs);
}

.theme-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-pill);
  flex: 0 0 auto;
}

/* 窄屏只留色点，标签藏起来 */
@media (max-width: 900px) {
  .theme-label {
    display: none;
  }
}

/* ---------- 用户区 ---------- */

.user {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex: 0 0 auto;
}

/* 跨端入口：比站内导航轻一档，不跟"购物车/我的订单"抢注意力 */
.backend-link {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  transition: color var(--dur-fast) var(--ease-out);
}

.backend-link:hover {
  color: var(--color-accent);
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
  padding: var(--space-6) var(--layout-gutter) var(--space-12);
}

/* ---------- 页脚 ---------- */

.footer {
  border-top: 1px solid var(--color-border);
  background: var(--color-bg-surface);
}

.footer-inner {
  max-width: var(--layout-max);
  margin: 0 auto;
  padding: var(--space-5) var(--layout-gutter);
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
  .header-inner {
    gap: var(--space-4);
  }

  .nav {
    display: none;
  }

  /* 窄屏和 .nav 一并收起：header 里每样都是定宽，留着会把内容顶出可视区 */
  .backend-link {
    display: none;
  }
}
</style>
