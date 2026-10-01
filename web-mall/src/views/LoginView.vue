<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { isBizError } from '@/api/errors'
import { useAuthStore } from '@/stores/auth'

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

async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  try {
    if (mode.value === 'login') {
      await auth.login(form.phone, form.password)
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

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="手机号" prop="phone">
          <el-input v-model="form.phone" placeholder="11 位手机号" maxlength="11" />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="至少 8 位，含字母/数字/符号中的两类"
          />
        </el-form-item>

        <el-form-item v-if="mode === 'register'" label="确认密码" prop="confirmPassword">
          <el-input v-model="form.confirmPassword" type="password" show-password />
        </el-form-item>

        <el-form-item v-if="mode === 'register'" label="昵称（选填）" prop="nickname">
          <el-input v-model="form.nickname" maxlength="64" />
        </el-form-item>

        <el-button
          type="primary"
          class="submit"
          :loading="auth.loading"
          native-type="submit"
          @click="onSubmit"
        >
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
