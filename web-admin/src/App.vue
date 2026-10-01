<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { setUnauthorizedHandler } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

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
  <el-container class="layout">
    <el-header class="header">
      <div class="header-inner">
        <RouterLink to="/" class="brand">eshop 商家后台</RouterLink>

        <nav v-if="auth.isLoggedIn" class="nav">
          <RouterLink to="/" class="nav-link">我的商品</RouterLink>
          <RouterLink to="/products/new" class="nav-link">发布商品</RouterLink>
        </nav>

        <div class="spacer" />

        <div v-if="auth.isLoggedIn" class="user">
          <span class="nickname">{{ auth.user?.nickname }}</span>
          <el-tag size="small" type="success">
            {{ auth.user?.shopId ? '商家' : '未开店' }}
          </el-tag>
          <el-button link type="primary" @click="onLogout">退出</el-button>
        </div>
        <RouterLink v-else to="/login">
          <el-button type="primary" size="small">登录</el-button>
        </RouterLink>
      </div>
    </el-header>

    <el-main class="content">
      <RouterView />
    </el-main>

    <el-footer class="footer">
      <RouterLink to="/status" class="footer-link">系统状态</RouterLink>
    </el-footer>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100%;
}

.header {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  padding: 0;
}

.header-inner {
  height: 56px;
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px;
  display: flex;
  align-items: center;
  gap: 28px;
}

.brand {
  font-size: 16px;
  font-weight: 600;
  color: #409eff;
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

.spacer {
  flex: 1;
}

.user {
  display: flex;
  align-items: center;
  gap: 12px;
}

.nickname {
  font-size: 14px;
}

.content {
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
}

.footer {
  text-align: center;
}

.footer-link {
  font-size: 12px;
  color: #a8abb2;
}
</style>
