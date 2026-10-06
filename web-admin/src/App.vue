<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { setUnauthorizedHandler } from '@/api/http'
import ProfileEditDialog from '@/components/ProfileEditDialog.vue'
import { usePoll } from '@/composables/usePoll'
import { MALL_APP_URL } from '@/utils/siblingApp'
import { onImageError } from '@/utils/placeholder'
import { useAuthStore } from '@/stores/auth'
import { useSupportStore } from '@/stores/support'

const auth = useAuthStore()
const support = useSupportStore()
const router = useRouter()
const route = useRoute()
const profileVisible = ref(false)

/** 营销中心只有平台运营能进：后端接口只放给 admin / finance */
const isAdmin = computed(() => auth.user?.role === 'admin' || auth.user?.role === 'finance')

/**
 * 类目维护比营销更窄：后端 product 模块的 AdminDep 是 require_role("admin")，
 * 不含 finance。所以这里单独判，别复用 isAdmin 把财务也放进来。
 */
const isPlatformAdmin = computed(() => auth.user?.role === 'admin')

/**
 * 有没有店铺 —— 商家菜单的判据用**这个**，不用 role。
 *
 * ★ 后端 `/api/merchant/*` 一律按**店铺归属**判权（CurrentShopIdDep 查库），
 *   前端用同一个信号，才不会出现"菜单看得见、点进去弹『请先开通店铺』"。
 *   平台运营账号没有店铺，于是自然看不到那一批商家菜单 —— 那正是对的：
 *   运营维护的是平台数据（类目、营销），不该假装能管某个店的库存和运费。
 */
const hasShop = computed(() => Boolean(auth.user?.shopId))

/**
 * 顶栏身份标签。
 *
 * ★ 平台侧显示**角色**而不是"开没开店" —— 运营账号本来就不该有店铺，
 *   给它标「未开店」像是在提示一件没做完的事。
 */
const identityTag = computed(() => {
  const role = auth.user?.role
  if (role === 'admin') return { text: '平台运营', type: 'warning' as const }
  if (role === 'finance') return { text: '财务', type: 'warning' as const }
  return hasShop.value
    ? { text: '商家', type: 'success' as const }
    : { text: '未开店', type: 'info' as const }
})

/** 昵称下拉：个人资料 / 退出登录 */
function onUserCommand(command: 'profile' | 'logout'): void {
  if (command === 'profile') {
    profileVisible.value = true
    return
  }
  void onLogout()
}

setUnauthorizedHandler(() => {
  auth.clearLocal()
  support.reset()
  ElMessage.warning('登录已过期，请重新登录')
  void router.push({ name: 'login', query: { redirect: route.fullPath } })
})

onMounted(async () => {
  await auth.restore()
  // 角标要等登录态恢复之后再拉，否则未登录时白跑一次
  await support.refresh()
})

/**
 * 「待回复」角标：60 秒一轮（切到后台会停）+ 每次路由切换补一次。
 *
 * `immediate: false`：初次拉取交给上面 onMounted 那次（它排在 auth.restore 之后），
 * 否则轮询件会赶在恢复登录态之前拿着空令牌白跑一遍。
 */
usePoll(() => support.refresh(), 60_000, { immediate: false })
watch(
  () => route.fullPath,
  () => void support.refresh(),
)

async function onLogout(): Promise<void> {
  await auth.logout()
  support.reset()
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
          <span class="brand-sub">商家/平台后台</span>
        </RouterLink>

        <nav v-if="auth.isLoggedIn" class="nav">
          <!-- 商品审核排第一：它是平台运营每天要处理的入口，比别的都常用。
               商家账号看不到这一项（v-if="isPlatformAdmin"），所以对商家的菜单顺序没有影响。 -->
          <RouterLink v-if="isPlatformAdmin" to="/audits" class="nav-link">商品审核</RouterLink>
          <!-- 商家菜单：判据是"有没有店铺"，和后端 /api/merchant/* 的判权方式一致。
               平台运营点进去只会弹「请先开通店铺」，索性不给看。 -->
          <RouterLink v-if="hasShop" to="/" class="nav-link">我的商品</RouterLink>
          <RouterLink v-if="hasShop" to="/products/new" class="nav-link">发布商品</RouterLink>
          <RouterLink v-if="hasShop" to="/orders" class="nav-link">订单</RouterLink>
          <RouterLink v-if="hasShop" to="/aftersales" class="nav-link">售后</RouterLink>
          <!-- 客服两边都能看：商家看本店队列、平台运营看全部（页面内部按身份换端点） -->
          <RouterLink v-if="hasShop || isAdmin" to="/support" class="nav-link">
            客服
            <span v-if="support.pending > 0" class="badge tnum">
              {{ support.pending > 99 ? '99+' : support.pending }}
            </span>
          </RouterLink>
          <!-- 评价两边都能看：商家看本店、运营看审核队列，页面内部自己分会话 -->
          <RouterLink v-if="hasShop || isAdmin" to="/reviews" class="nav-link">评价</RouterLink>
          <RouterLink v-if="isPlatformAdmin" to="/categories" class="nav-link">类目</RouterLink>
          <!-- 用户管理只给 admin（不含 finance）：能看营销数据不等于能封人 -->
          <RouterLink v-if="isPlatformAdmin" to="/users" class="nav-link">用户</RouterLink>
          <RouterLink v-if="isAdmin" to="/promotions" class="nav-link">营销</RouterLink>
          <RouterLink v-if="isAdmin" to="/banners" class="nav-link">轮播图</RouterLink>
          <RouterLink v-if="hasShop" to="/inventory" class="nav-link">库存</RouterLink>
          <RouterLink v-if="hasShop" to="/warehouses" class="nav-link">仓库</RouterLink>
          <RouterLink v-if="hasShop" to="/freight" class="nav-link">运费</RouterLink>
          <RouterLink v-if="hasShop" to="/shop" class="nav-link">店铺设置</RouterLink>
        </nav>

        <div class="spacer" />

        <!-- 同一个账号也能逛商城（卖家账号买东西是常规做法），给个顺手的入口 -->
        <a :href="MALL_APP_URL" class="jump-link">去商城</a>

        <div v-if="auth.isLoggedIn" class="user">
          <!-- 昵称是个人资料的入口：点开是下拉，和后台控制台的惯例一致 -->
          <el-dropdown trigger="click" @command="onUserCommand">
            <span class="user-chip" title="个人资料">
              <img
                v-if="auth.user?.avatar"
                class="chip-avatar"
                :src="auth.user.avatar"
                alt="头像"
                @error="onImageError"
              />
              <span class="chip-name">{{ auth.user?.nickname }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">个人资料</el-dropdown-item>
                <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-tag :type="identityTag.type" size="small" disable-transitions>
            {{ identityTag.text }}
          </el-tag>
        </div>
        <RouterLink v-else to="/login">
          <el-button type="primary" size="small">登录</el-button>
        </RouterLink>
      </div>
    </header>

    <main class="content">
      <RouterView />
    </main>

    <ProfileEditDialog v-model="profileVisible" />
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

/* 导航：hover 给一块浅底，当前项给一块实心强调色胶囊。
 *
 * ★ 原来是"灰字 + 当前项一条 2px 下划线"，一组纯文字在头部里读不出可点性。
 *
 * 横向内边距比商城那边紧一档（8px vs 12px）：后台菜单最多的时候有 10 项
 * （商家账号），而 header 内容宽只有 1200px —— 用 12px 会把「去商城」和用户区
 * 挤出可视区。
 */
.nav {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex: 0 0 auto;
}

.nav-link {
  display: inline-flex;
  align-items: center;
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-pill);
  font-size: var(--text-base);
  color: var(--color-text-secondary);
  white-space: nowrap;
  transition:
    background-color var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out);
}

.nav-link:hover {
  color: var(--color-text);
  background: var(--color-bg-subtle);
}

.nav-link.router-link-exact-active {
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-weight: var(--weight-medium);
}

/* 角标。被激活的导航项底色就是强调色，所以那里要**反相**，否则数字和底同色看不见 */
.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  margin-left: var(--space-1);
  padding: 0 5px;
  border-radius: var(--radius-pill);
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  line-height: 1;
}

.nav-link.router-link-exact-active .badge {
  background: var(--color-accent-contrast);
  color: var(--color-accent);
}

.spacer {
  flex: 1;
}

/* 跨端入口：贴着用户区但不参与它内部的紧凑排布 */
/* 跨端入口：描边小胶囊。它跳出当前应用，所以用"盒子"把它和站内导航区分开 */
.jump-link {
  flex: 0 0 auto;
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  transition:
    color var(--dur-fast) var(--ease-out),
    border-color var(--dur-fast) var(--ease-out);
}

.jump-link:hover {
  border-color: var(--color-accent);
  color: var(--color-accent);
}

.user {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex: 0 0 auto;
}

/* 用户胶囊：头像 + 名字，整体是个通往个人资料的下拉 */
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  cursor: pointer;
  outline: none;
  transition: background-color var(--dur-fast) var(--ease-out);
}

.user-chip:hover {
  background: var(--color-bg-hover);
}

.chip-avatar {
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  border-radius: var(--radius-pill);
  object-fit: cover;
  background: var(--media-bg);
}

.chip-name {
  font-size: var(--text-sm);
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

@media (max-width: 768px) {
  .nav {
    display: none;
  }
}
</style>
