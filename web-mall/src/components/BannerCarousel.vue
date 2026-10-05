<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { fetchBanners, type Banner } from '@/api/banner'
import { onImageError } from '@/utils/placeholder'

/**
 * 首页轮播图。
 *
 * ★ 没配置就**整块不渲染** —— 不留一个空框占着首屏。
 * ★ 拉不到也不弹错：首页不该为一张图报错，逛商品才是主线。
 * ★ 点击只走**站内路径**。后端已经限过一次（`link_url` 必须 `/` 开头），
 *   这里再挡一层，防的是脏数据把路由带飞。
 */
const router = useRouter()
const banners = ref<Banner[]>([])

onMounted(async () => {
  try {
    banners.value = await fetchBanners()
  } catch {
    banners.value = []
  }
})

function go(banner: Banner): void {
  if (banner.linkUrl?.startsWith('/')) void router.push(banner.linkUrl)
}
</script>

<template>
  <el-carousel
    v-if="banners.length > 0"
    class="banner"
    height="320px"
    :interval="4500"
    arrow="hover"
  >
    <el-carousel-item v-for="b in banners" :key="b.id">
      <img
        class="shot"
        :class="{ 'is-clickable': b.linkUrl }"
        :src="b.image"
        :alt="b.title"
        @error="onImageError"
        @click="go(b)"
      />
    </el-carousel-item>
  </el-carousel>
</template>

<style scoped>
.banner {
  border-radius: var(--radius-lg);
  overflow: hidden;
  /* 指示点压在图上，深色底保证浅色图上也看得见 */
  --el-carousel-indicator-active-bg-color: var(--color-text-inverse);
}

.shot {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  background: var(--media-bg);
}

.shot.is-clickable {
  cursor: pointer;
}

.banner :deep(.el-carousel__indicators--horizontal) {
  bottom: 12px;
}
</style>
