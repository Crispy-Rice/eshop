<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { isBizError } from '@/api/errors'
import {
  USER_BANNED,
  USER_CLOSED,
  USER_NORMAL,
  banUser,
  fetchAdminUsers,
  resetUserPassword,
  unbanUser,
  userTagType,
  type AdminUser,
} from '@/api/user'
import { formatDateTime } from '@/utils/money'
import { onImageError } from '@/utils/placeholder'

const users = ref<AdminUser[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const acting = ref(false)
const cursor = ref<string | null>(null)
const hasMore = ref(false)

const status = ref<number | undefined>(undefined)
const keyword = ref('')

const STATUS_TABS = [
  { label: '全部', value: undefined },
  { label: '正常', value: USER_NORMAL },
  { label: '已封禁', value: USER_BANNED },
  { label: '已注销', value: USER_CLOSED },
] as const

/** 重置密码拿到的临时口令。**只在这里停留一次** —— 关掉就再也看不到了 */
const tempPassword = ref<string | null>(null)
const tempVisible = ref(false)

async function load(reset = true): Promise<void> {
  if (reset) {
    loading.value = true
    cursor.value = null
  } else {
    loadingMore.value = true
  }
  try {
    const page = await fetchAdminUsers({
      status: status.value,
      keyword: keyword.value.trim() || undefined,
      cursor: reset ? undefined : (cursor.value ?? undefined),
      limit: 20,
    })
    users.value = reset ? page.items : [...users.value, ...page.items]
    cursor.value = page.nextCursor
    hasMore.value = page.hasMore
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载用户失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function switchStatus(value: number | undefined): Promise<void> {
  if (status.value === value) return
  status.value = value
  await load(true)
}

/**
 * 平台账号（运营 / 财务）：后端不允许封禁它们、也不允许重置它们的密码
 * （一次误操作会把平台自己锁死，没有第二条救济路径）。
 * 前端先挡一层，省得点下去只换来一句 403。
 */
function isPlatformAccount(row: AdminUser): boolean {
  return row.role === 'admin' || row.role === 'finance'
}

async function onSearch(): Promise<void> {
  await load(true)
}

async function onBan(user: AdminUser): Promise<void> {
  let reason: string
  try {
    // 提示里写清两件事：**这句话会给对方看到**，以及生效时机（不是"立即"）
    const { value } = await ElMessageBox.prompt(
      `封禁后「${user.nickname}」将无法登录，已登录的会在最长 30 分钟内被挤出。` +
        '下面这段理由会展示给该用户看，请写清楚。',
      '封禁用户',
      {
        inputPlaceholder: '例如：疑似刷单，多次异常下单',
        inputValidator: (v: string) => (v && v.trim().length >= 2 ? true : '理由至少 2 个字'),
        confirmButtonText: '封禁',
        confirmButtonClass: 'el-button--danger',
      },
    )
    reason = value.trim()
  } catch {
    return // 用户取消
  }

  acting.value = true
  try {
    await banUser(user.id, reason)
    ElMessage.success('已封禁')
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '封禁失败')
  } finally {
    acting.value = false
  }
}

async function onUnban(user: AdminUser): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定解封「${user.nickname}」吗？解封后他就能正常登录了（需要重新登录一次）。`,
      '解封用户',
      { type: 'warning' },
    )
  } catch {
    return
  }

  acting.value = true
  try {
    await unbanUser(user.id)
    ElMessage.success('已解封')
    await load(true)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '解封失败')
  } finally {
    acting.value = false
  }
}

async function onResetPassword(user: AdminUser): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定重置「${user.nickname}」的密码吗？旧密码立即失效、该用户的登录会被退出；` +
        '新口令只显示一次，请当场转告他。',
      '重置密码',
      { type: 'warning', confirmButtonText: '重置', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }

  acting.value = true
  try {
    const data = await resetUserPassword(user.id)
    tempPassword.value = data.tempPassword
    tempVisible.value = true
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '重置失败')
  } finally {
    acting.value = false
  }
}

async function copyTemp(): Promise<void> {
  if (!tempPassword.value) return
  try {
    await navigator.clipboard.writeText(tempPassword.value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选中复制')
  }
}

function closeTemp(): void {
  tempVisible.value = false
  tempPassword.value = null
}

onMounted(() => {
  void load(true)
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">用户</h2>
      <span class="hint">
        封禁只禁登录（已登录的最长 30 分钟内失效）；注销是用户自助的终态，运营不可代办
      </span>
      <div class="spacer" />
      <el-input
        v-model="keyword"
        size="small"
        class="search"
        clearable
        placeholder="手机号（完整）或昵称"
        @keyup.enter="onSearch"
        @clear="onSearch"
      />
      <el-button size="small" type="primary" @click="onSearch">查询</el-button>
      <el-button size="small" :loading="loading" @click="load(true)">刷新</el-button>
    </div>

    <el-radio-group :model-value="status" size="small" @change="switchStatus($event as number | undefined)">
      <el-radio-button v-for="t in STATUS_TABS" :key="t.label" :value="t.value">
        {{ t.label }}
      </el-radio-button>
    </el-radio-group>

    <div class="panel">
      <el-empty v-if="!loading && users.length === 0" description="没有匹配的用户" />

      <el-table v-else v-loading="loading" :data="users">
        <el-table-column label="用户" min-width="200">
          <template #default="{ row }">
            <div class="user-cell">
              <img v-if="row.avatar" class="avatar" :src="row.avatar" alt="头像" @error="onImageError" />
              <div>
                <div class="name">
                  {{ row.nickname }}
                  <el-tag v-if="row.isShopOwner" size="small" type="warning" effect="light">
                    店主
                  </el-tag>
                </div>
                <div class="cell-sub tnum">{{ row.phone }}</div>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="角色" width="100">
          <template #default="{ row }">
            <span class="cell-sub">{{ row.role }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="userTagType(row.status)" size="small" effect="light">
              {{ row.statusText }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="注册时间" width="160">
          <template #default="{ row }">
            <span class="cell-sub tnum">{{ formatDateTime(row.registerTime) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="200" align="right" fixed="right">
          <template #default="{ row }">
            <!-- 已注销是终态：封禁/解封/重置都没有意义，只留一个说明 -->
            <span v-if="row.status === USER_CLOSED" class="cell-sub">已注销</span>
            <!-- 平台账号封不得也重置不得，前端先挡一层 -->
            <span v-else-if="isPlatformAccount(row)" class="cell-sub">平台账号</span>
            <template v-else>
              <el-button
                v-if="row.status === USER_NORMAL"
                size="small"
                type="danger"
                plain
                :loading="acting"
                @click="onBan(row)"
              >
                封禁
              </el-button>
              <el-button
                v-if="row.status === USER_BANNED"
                size="small"
                :loading="acting"
                @click="onUnban(row)"
              >
                解封
              </el-button>
              <el-button size="small" :loading="acting" @click="onResetPassword(row)">
                重置密码
              </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="hasMore" class="more">
        <el-button size="small" :loading="loadingMore" @click="load(false)">加载更多</el-button>
      </div>
    </div>

    <!-- 临时口令：只在重置成功后出现一次，关掉就没了 -->
    <el-dialog
      v-model="tempVisible"
      title="临时密码（只显示这一次）"
      width="440px"
      :close-on-click-modal="false"
      @closed="closeTemp"
    >
      <p class="temp-pwd tnum">{{ tempPassword }}</p>
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="关闭这个窗口后就再也查不到了。请立刻转告用户，并让他登录后自行修改密码。"
      />
      <template #footer>
        <el-button @click="copyTemp">复制</el-button>
        <el-button type="primary" @click="tempVisible = false">我已记下</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 只用语义 token，见 docs/17-frontend-design-system.md */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.hint {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.spacer {
  flex: 1;
}

.search {
  width: 200px;
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.user-cell {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.avatar {
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  object-fit: cover;
  border-radius: var(--radius-pill);
  background: var(--media-bg);
}

.name {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--color-text);
}

.cell-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.more {
  display: flex;
  justify-content: center;
  padding: var(--space-3);
}

.temp-pwd {
  margin-bottom: var(--space-3);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  text-align: center;
  letter-spacing: 1px;
  word-break: break-all;
}
</style>
