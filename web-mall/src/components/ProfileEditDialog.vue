<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { updateMe } from '@/api/auth'
import { isBizError } from '@/api/errors'
import AvatarUploader from '@/components/AvatarUploader.vue'
import { useAuthStore } from '@/stores/auth'

/**
 * 个人资料编辑。
 *
 * 只放开**昵称 / 头像 / 性别**三样。手机号是登录标识，改它得走短信验证，
 * 不能放在这个表单里 —— 所以这里只读展示（而且是脱敏后的）。
 * 后端的 `UpdateProfileRequest` 还支持生日，但 `UserOut` 不返回它，
 * 拉不到就没法回填，先不做。
 *
 * 存完直接刷 store 里的用户信息，父组件不用管。
 */
const visible = defineModel<boolean>({ required: true })

const auth = useAuthStore()
const saving = ref(false)

const form = reactive({
  nickname: '',
  avatar: null as string | null,
  gender: 0,
})

// 每次打开都用当前用户信息回填 —— 上次点了取消留下的改动不能带进来
watch(visible, (open) => {
  const user = auth.user
  if (!open || !user) return
  form.nickname = user.nickname
  form.avatar = user.avatar
  form.gender = user.gender
})

const GENDERS = [
  { value: 0, label: '保密' },
  { value: 1, label: '男' },
  { value: 2, label: '女' },
]

async function submit(): Promise<void> {
  const nickname = form.nickname.trim()
  if (!nickname) {
    ElMessage.warning('昵称不能为空')
    return
  }

  saving.value = true
  try {
    await updateMe({ nickname, avatar: form.avatar, gender: form.gender })
    await auth.restore()
    ElMessage.success('资料已保存')
    visible.value = false
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="编辑资料" width="440px">
    <el-form label-width="72px">
      <el-form-item label="头像">
        <AvatarUploader v-model="form.avatar" />
      </el-form-item>
      <el-form-item label="昵称">
        <el-input v-model="form.nickname" maxlength="64" placeholder="1~64 个字符" />
      </el-form-item>
      <el-form-item label="性别">
        <el-radio-group v-model="form.gender">
          <el-radio v-for="g in GENDERS" :key="g.value" :value="g.value">{{ g.label }}</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="手机号">
        <div class="readonly-block">
          <span class="readonly">{{ auth.user?.phone }}</span>
          <div class="hint">手机号是登录账号，暂不支持在这里修改</div>
        </div>
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.readonly-block {
  display: flex;
  flex-direction: column;
  line-height: var(--leading-snug);
}

.readonly {
  color: var(--color-text-tertiary);
}

.hint {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}
</style>
