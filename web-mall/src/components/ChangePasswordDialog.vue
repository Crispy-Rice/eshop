<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { isBizError } from '@/api/errors'
import { useAuthStore } from '@/stores/auth'

const visible = defineModel<boolean>({ required: true })
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = ref({ oldPassword: '', newPassword: '', confirmPassword: '' })

/**
 * 与后端 `core/security.py` 的 `validate_password_strength` 对齐：
 * 长度 ≥ 8 且**至少含两类字符**（小写 / 大写 / 数字 / 符号）。
 *
 * ★ 登录页那条规则只校验了长度，跟后端不一致 —— 这里按后端口径写全，
 *   否则用户填一条 8 位纯数字会在这里放行、被后端打回来。
 */
function hasTwoCharClasses(value: string): boolean {
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((re) => re.test(value)).length
  return classes >= 2
}

const rules: FormRules = {
  oldPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, max: 128, message: '密码至少 8 位', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) =>
        hasTwoCharClasses(value)
          ? callback()
          : callback(new Error('需包含字母、数字、符号中的至少两类')),
      trigger: 'blur',
    },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) =>
        value === form.value.newPassword ? callback() : callback(new Error('两次输入的密码不一致')),
      trigger: 'blur',
    },
  ],
}

async function onSubmit(): Promise<void> {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  submitting.value = true
  try {
    await auth.changePassword(form.value.oldPassword, form.value.newPassword)
    // 后端已把其它设备的 refresh token 全部吊销，并给当前会话补发了新令牌
    ElMessage.success('密码已修改，其它设备的登录已退出')
    visible.value = false
  } catch (e) {
    // 原密码不对是 400，message 直接可读，透出去
    ElMessage.error(isBizError(e) ? e.message : '修改失败')
  } finally {
    submitting.value = false
  }
}

/** 关闭时清空，避免下次打开还留着上次输了一半的密码 */
function onClosed(): void {
  form.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  formRef.value?.clearValidate()
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="修改密码"
    width="440px"
    :close-on-click-modal="false"
    @closed="onClosed"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px" @submit.prevent>
      <el-form-item label="当前密码" prop="oldPassword">
        <el-input
          v-model="form.oldPassword"
          type="password"
          show-password
          autocomplete="current-password"
        />
      </el-form-item>
      <el-form-item label="新密码" prop="newPassword">
        <el-input
          v-model="form.newPassword"
          type="password"
          show-password
          autocomplete="new-password"
          placeholder="至少 8 位，含字母/数字/符号中的两类"
        />
      </el-form-item>
      <el-form-item label="确认新密码" prop="confirmPassword">
        <el-input
          v-model="form.confirmPassword"
          type="password"
          show-password
          autocomplete="new-password"
        />
      </el-form-item>
    </el-form>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="修改后，你在其它设备上的登录会被退出；当前设备不用重新登录。"
    />

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="onSubmit">确定</el-button>
    </template>
  </el-dialog>
</template>
