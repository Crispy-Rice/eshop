<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <div class="layout">
    <header class="header">
      <RouterLink to="/" class="brand">eshop 商城</RouterLink>
    </header>

    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

.header {
  height: 56px;
  display: flex;
  align-items: center;
  padding: 0 24px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.brand {
  font-size: 16px;
  font-weight: 600;
}

.content {
  flex: 1;
  padding: 24px;
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
}
</style>
