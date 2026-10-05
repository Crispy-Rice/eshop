<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { isBizError } from '@/api/errors'
import { useAuthStore } from '@/stores/auth'
import { rememberPasswordInBrowser } from '@/utils/credential'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const mode = ref<'login' | 'register'>('login')
const formRef = ref<FormInstance>()

const form = reactive({
  phone: '',
  password: '',
  confirmPassword: '',
  nickname: '',
})

/**
 * 记住手机号。
 *
 * ★ **只存手机号，不存密码。**
 *
 * - 手机号是**非机密**的标识，明摆着写在页面上，存 localStorage 不增加暴露面。
 * - 密码是机密。明文存进 localStorage，页面里任何一处 XSS 都能读走；而且用户
 *   往往在别处用同一个密码，伤害半径比丢一个 token 大得多。这个项目已经因为
 *   "refresh token 存 localStorage"承担了一份这样的风险（docs/15 §1.1），
 *   再叠一个明文密码属于额外、且完全可以避免的暴露。
 * - 真要"记住密码"，正确做法是交给**浏览器自带的密码管理器**：下面输入框上的
 *   `autocomplete` 属性就是为它准备的，浏览器会询问是否保存，并由系统钥匙串
 *   保管。那是零风险的那一半功能，我们不用自己实现。
 */
const REMEMBERED_PHONE_KEY = 'eshop.rememberedPhone'
const rememberPhone = ref(false)

onMounted(() => {
  const saved = localStorage.getItem(REMEMBERED_PHONE_KEY)
  if (saved) {
    form.phone = saved
    rememberPhone.value = true
  }
})

const rules: FormRules = {
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: /^1[3-9]\d{9}$/, message: '手机号格式不正确', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, max: 128, message: '密码至少 8 位', trigger: 'blur' },
  ],
  confirmPassword: [
    {
      validator: (_rule, value: string, callback) => {
        if (mode.value === 'register' && value !== form.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

/**
 * 手机号的实时指示：绿勾 / 红叉。
 *
 * ★ 用的是**后端同一条规则**（1[3-9] 开头 + 9 位数字），而不是"够 11 位就行" ——
 *   号段是真实约束，11 位但开头是 2 照样提交不上去，那时候再报错就晚了。
 *
 * 空着不显示图标：一进页面就飘个红叉既没必要也吵，要的是"填错了"的即时反馈。
 */
const phoneState = computed<'idle' | 'ok' | 'bad'>(() => {
  if (!form.phone) return 'idle'
  return /^1[3-9]\d{9}$/.test(form.phone) ? 'ok' : 'bad'
})

/**
 * 提交。**绑在表单的 submit 上，不是按钮的 click 上**。
 *
 * 早先只绑了 `@click` 而表单是 `@submit.prevent`，于是**回车提交是死的** ——
 * 浏览器自动填充完账号密码、用户顺手一敲回车，什么都不会发生。这既是最自然的
 * 登录手势，也是浏览器判断"这是一次登录、要不要存密码"的信号之一。
 */
async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  try {
    if (mode.value === 'login') {
      await auth.login(form.phone, form.password)
      // 交给系统钥匙串保管，我们自己不留密码副本
      rememberPasswordInBrowser(form.phone, form.password)
      if (rememberPhone.value) {
        localStorage.setItem(REMEMBERED_PHONE_KEY, form.phone)
      } else {
        localStorage.removeItem(REMEMBERED_PHONE_KEY)
      }
      ElMessage.success('登录成功')
    } else {
      await auth.register(form.phone, form.password, form.nickname || undefined)
      ElMessage.success('注册成功')
    }
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.push(redirect)
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败，请重试')
  }
}

function switchMode(): void {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  formRef.value?.clearValidate()
}
</script>

<template>
  <div class="wrap">
    <el-card class="card" shadow="never">
      <h2 class="title">{{ mode === 'login' ? '登录' : '注册' }}</h2>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="onSubmit"
      >
        <el-form-item label="手机号" prop="phone">
          <el-input
            v-model="form.phone"
            name="username"
            placeholder="11 位手机号"
            maxlength="11"
            autocomplete="username"
          >
            <template #suffix>
              <span v-if="phoneState === 'ok'" class="phone-hint ok" title="手机号格式正确">✓</span>
              <span v-else-if="phoneState === 'bad'" class="phone-hint bad" title="手机号格式不对"
                >✕</span
              >
            </template>
          </el-input>
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            name="password"
            type="password"
            show-password
            placeholder="至少 8 位，含字母/数字/符号中的两类"
            :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
          />
        </el-form-item>

        <!-- 只记手机号；密码交给浏览器自己的密码管理器（见脚本里的说明） -->
        <el-form-item v-if="mode === 'login'">
          <el-checkbox v-model="rememberPhone">记住手机号</el-checkbox>
        </el-form-item>

        <el-form-item v-if="mode === 'register'" label="确认密码" prop="confirmPassword">
          <el-input
            v-model="form.confirmPassword"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>

        <el-form-item v-if="mode === 'register'" label="昵称（选填）" prop="nickname">
          <el-input v-model="form.nickname" maxlength="64" />
        </el-form-item>

        <!-- 提交交给表单的 submit：点按钮和按回车走的是同一条路径 -->
        <el-button type="primary" class="submit" :loading="auth.loading" native-type="submit">
          {{ mode === 'login' ? '登录' : '注册并登录' }}
        </el-button>
      </el-form>

      <div class="switch">
        <span>{{ mode === 'login' ? '还没有账号？' : '已有账号？' }}</span>
        <el-button link type="primary" @click="switchMode">
          {{ mode === 'login' ? '去注册' : '去登录' }}
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.wrap {
  display: flex;
  justify-content: center;
  padding: var(--space-10) var(--space-4);
}

.card {
  width: 380px;
  max-width: 100%;
}

/* 手机号的实时指示。颜色走语义 token，四套皮肤下自动跟着变 */
.phone-hint {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  line-height: 1;
}

.phone-hint.ok {
  color: var(--color-success);
}

.phone-hint.bad {
  color: var(--color-danger);
}

.title {
  margin: 0 0 var(--space-5);
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.submit {
  width: 100%;
}

.switch {
  margin-top: var(--space-4);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
  text-align: center;
}
</style>
