<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import PromoStrip from '@/components/PromoStrip.vue'
import ProfileEditDialog from '@/components/ProfileEditDialog.vue'
import { accountBlockOf } from '@/api/errors'
import { setUnauthorizedHandler } from '@/api/http'
import { fetchSiteTheme } from '@/api/site'
import { usePoll } from '@/composables/usePoll'
import { useTheme } from '@/composables/useTheme'
import { useAuthStore } from '@/stores/auth'
import { useCartStore } from '@/stores/cart'
import { useNotifyStore } from '@/stores/notify'
import { ADMIN_APP_URL } from '@/utils/siblingApp'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()
const cart = useCartStore()
const notify = useNotifyStore()
const router = useRouter()
const route = useRoute()
const { applyServerSkin } = useTheme()
const profileVisible = ref(false)

/**
 * 后台入口。只给真进得去的人显示 —— 买家点「商家后台」只会撞 403，
 * 不如等他在「我的」里开完店再出现（运营/财务则直接给运营后台）。
 */
const backendEntry = computed(() => {
  const user = auth.user
  if (!user) return null
  if (user.role === 'admin' || user.role === 'finance') {
    return { label: '去后台', url: ADMIN_APP_URL }
  }
  return user.shopId ? { label: '去后台', url: ADMIN_APP_URL } : null
})

/**
 * 令牌失效时由 http 层回调：清状态并跳登录。
 *
 * ★ 传进来的 `blocked` 只在**账号被冻结 / 已注销**时才有值。这两种情况不能提示
 *   「登录已过期，请重新登录」—— 重新登录不会成功，那是个死循环的指引。
 *   把详情交给登录页的持久面板说清楚（见 `LoginView`）。
 */
setUnauthorizedHandler((blocked) => {
  auth.clearLocal()
  cart.reset()
  notify.reset()
  if (blocked) {
    auth.setBlocked(accountBlockOf(blocked))
  } else {
    ElMessage.warning('登录已过期，请重新登录')
  }
  void router.push({ name: 'login', query: { redirect: route.fullPath } })
})

/**
 * 拉一次站点皮肤并应用。
 *
 * ★ 皮肤由**运营**在后台启用（后端一行配置），买家端没有切换器 ——
 *   本地缓存只负责首屏不闪，真正的值以这里拉到的为准。
 * ★ 失败就沿用缓存：这是纯视觉的事，拉不到不该影响任何功能，也不值得打扰用户。
 */
async function loadSiteSkin(): Promise<void> {
  try {
    applyServerSkin((await fetchSiteTheme()).skin)
  } catch {
    // 静默：沿用缓存里的皮肤
  }
}

onMounted(async () => {
  // 与 auth 并行、不等它 —— 主题早到晚到都不影响功能，排在 restore 前面
  // 只会白白推迟登录态的恢复
  void loadSiteSkin()
  await auth.restore()
  // 角标要在登录态恢复之后再拉，否则未登录时白跑一次
  await cart.refresh()
  await notify.refresh()
})

/**
 * 站内信角标：30 秒一轮（切到后台会停），另外**每次路由切换补一次** ——
 * 用户刚在消息页点完「全部已读」回到商品页时，角标必须已经是新的。
 *
 * `immediate: false`：初次拉取交给上面 onMounted 里那次，它排在 `auth.restore()`
 * 之后；轮询件如果自己在挂载时跑，会赶在恢复登录态之前拿着空令牌白跑一遍。
 */
usePoll(() => notify.refresh(), 30_000, { immediate: false })
watch(
  () => route.fullPath,
  () => void notify.refresh(),
)

/** 昵称下拉：个人资料 / 退出登录 —— 和后台是同一套（菜单直接开弹窗，不跳页） */
function onUserCommand(command: 'profile' | 'logout'): void {
  if (command === 'logout') {
    void onLogout()
    return
  }
  profileVisible.value = true
}

async function onLogout(): Promise<void> {
  await auth.logout()
  cart.reset()
  notify.reset()
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
          <RouterLink v-if="auth.isLoggedIn" to="/support" class="nav-link">客服</RouterLink>
          <RouterLink v-if="auth.isLoggedIn" to="/notifications" class="nav-link">
            消息
            <span v-if="notify.unread > 0" class="badge tnum">
              {{ notify.unread > 99 ? '99+' : notify.unread }}
            </span>
          </RouterLink>
          <RouterLink v-if="auth.isLoggedIn" to="/account" class="nav-link">我的</RouterLink>
        </nav>

        <div class="spacer" />

        <div class="user">
          <template v-if="auth.isLoggedIn">
            <!-- 跨端入口：描边小胶囊，和站内导航区分开（它跳出当前应用） -->
            <a v-if="backendEntry" :href="backendEntry.url" class="jump-link">
              {{ backendEntry.label }}
            </a>
            <!-- 用户胶囊：头像 + 名字，点开是下拉 —— 和后台同一套交互。
                 以前它是直接跳 /account，右边还并排挂着一个独立的「退出」按钮：
                 头像旁边贴着两个性质不同的按钮，既挤又容易点错。
                 落地页移到菜单里的「个人中心」，所以这里不再是 RouterLink。 -->
            <el-dropdown trigger="click" @command="onUserCommand">
              <span class="user-chip" title="个人中心">
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

    <!-- 个人资料弹窗挂在应用壳上：导航里的「个人资料」直接开它，不跳页（与后台一致） -->
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

/* 导航：hover 给一块浅底，当前项给一块**实心强调色胶囊**。
 *
 * ★ 原来是"灰字 + 当前项一条 2px 下划线"。在满是卡片、输入框、按钮的头部里，
 *   一组纯文字几乎读不出"这是可以点的"，当前项也只有一根细线。
 *   改成实心胶囊后有两个好处：位置一眼可见；而且胶囊跟着皮肤换色
 *   （中性=墨黑、618=红、双11=紫、年货节=橙）—— 换肤在头部就有了第二个信号，
 *   不用只靠品牌字那一处。
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
  gap: var(--space-1);
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-pill);
  font-size: var(--text-base);
  color: var(--color-text-secondary);
  white-space: nowrap;
  transition:
    background-color var(--dur-fast) var(--ease-out),
    color var(--dur-fast) var(--ease-out);
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
  background: var(--color-bg-subtle);
}

.nav-link.router-link-exact-active {
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-weight: var(--weight-medium);
}

/* ★ 购物车是当前项时，胶囊和角标都是强调色 —— 同色叠同色会看不见，要反过来 */
.nav-link.router-link-exact-active .badge {
  background: var(--color-accent-contrast);
  color: var(--color-accent);
}

.spacer {
  flex: 1;
}

/* ---------- 用户区 ---------- */

.user {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex: 0 0 auto;
}

/* 跨端入口：描边小胶囊。它跳出当前应用，所以用"盒子"把它和站内导航区分开 */
.jump-link {
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

/* 用户胶囊：头像 + 名字，整体是通往个人中心的下拉触发器。
   ★ cursor 和 outline 都要自己写：它以前是 RouterLink（<a>），浏览器自带手型光标，
     换成 <span> 之后没了；el-dropdown 还会给触发器套一层焦点轮廓。 */
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
  padding: var(--space-6) var(--layout-gutter) var(--space-12);
}

@media (max-width: 768px) {
  .header-inner {
    gap: var(--space-4);
  }

  .nav {
    display: none;
  }

  /* 窄屏和 .nav 一并收起：header 里每样都是定宽，留着会把内容顶出可视区 */
  .jump-link {
    display: none;
  }
}
</style>
