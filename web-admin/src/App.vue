<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <el-container class="layout">
    <el-header class="header">
      <RouterLink to="/" class="brand">eshop 商家 / 运营后台</RouterLink>
      <span class="roles">商家、平台运营、财务共用一套，按角色区分菜单与接口权限</span>
    </el-header>

    <el-main class="content">
      <RouterView />
    </el-main>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100%;
}

.header {
  display: flex;
  align-items: center;
  gap: 16px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.brand {
  font-size: 16px;
  font-weight: 600;
}

.roles {
  color: #909399;
  font-size: 13px;
}

.content {
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
}
</style>
