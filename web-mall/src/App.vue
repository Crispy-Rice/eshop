<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { setUnauthorizedHandler } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

// 令牌失效时由 http 层回调：清状态并跳登录
setUnauthorizedHandler(() => {
  auth.clearLocal()
  ElMessage.warning('登录已过期，请重新登录')
  void router.push({ name: 'login', query: { redirect: route.fullPath } })
})

onMounted(() => auth.restore())

async function onLogout(): Promise<void> {
  await auth.logout()
  ElMessage.success('已退出登录')
  void router.push({ name: 'home' })
}
</script>

<template>
  <div class="layout">
    <header class="header">
      <div class="header-inner">
        <RouterLink to="/" class="brand">eshop 商城</RouterLink>

        <div class="spacer" />

        <nav class="nav">
          <RouterLink to="/" class="nav-link">全部商品</RouterLink>
          <RouterLink v-if="auth.isLoggedIn" to="/account" class="nav-link">我的</RouterLink>
        </nav>

        <div class="user">
          <template v-if="auth.isLoggedIn">
            <span class="nickname">{{ auth.user?.nickname }}</span>
            <el-button link type="primary" @click="onLogout">退出</el-button>
          </template>
          <RouterLink v-else to="/login">
            <el-button type="primary" size="small">登录 / 注册</el-button>
          </RouterLink>
        </div>
      </div>
    </header>

    <main class="content">
      <RouterView />
    </main>

    <footer class="footer">
      <RouterLink to="/status" class="footer-link">系统状态</RouterLink>
    </footer>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

.header {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  position: sticky;
  top: 0;
  z-index: 10;
}

.header-inner {
  height: 56px;
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px;
  display: flex;
  align-items: center;
  gap: 24px;
}

.brand {
  font-size: 16px;
  font-weight: 600;
  color: #d93025;
}

.spacer {
  flex: 1;
}

.nav {
  display: flex;
  gap: 20px;
}

.nav-link {
  color: #606266;
  font-size: 14px;
}

.nav-link.router-link-exact-active {
  color: #303133;
  font-weight: 500;
}

.user {
  display: flex;
  align-items: center;
  gap: 12px;
}

.nickname {
  font-size: 14px;
  color: #303133;
}

.content {
  flex: 1;
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}

.footer {
  padding: 16px 24px 32px;
  text-align: center;
}

.footer-link {
  font-size: 12px;
  color: #a8abb2;
}
</style>
