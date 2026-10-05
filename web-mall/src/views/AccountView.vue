<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import * as authApi from '@/api/auth'
import type { Address, AddressInput } from '@/api/auth'
import { ErrorCode, isBizError } from '@/api/errors'
import { post } from '@/api/http'
import ProfileEditDialog from '@/components/ProfileEditDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { onImageError } from '@/utils/placeholder'

const auth = useAuthStore()
const addresses = ref<Address[]>([])
const loading = ref(false)
const shopLoading = ref(false)
const dialogVisible = ref(false)
const profileVisible = ref(false)
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance>()

/**
 * 角色标签。
 *
 * ★ 必须逐个列全。这里原来写的是「不是 buyer 就叫商家」的二元判断，
 *   于是平台运营（admin）和财务（finance）在「我的」页都显示成「商家」。
 */
const ROLE_TEXT: Record<string, string> = {
  buyer: '买家',
  merchant: '商家',
  admin: '平台运营',
  finance: '财务',
}

const ROLE_TAG_TYPE: Record<string, 'info' | 'success' | 'warning' | 'danger'> = {
  buyer: 'info',
  merchant: 'success',
  admin: 'warning',
  finance: 'danger',
}

const roleText = computed(() => ROLE_TEXT[auth.user?.role ?? ''] ?? auth.user?.role ?? '')
const roleTagType = computed(() => ROLE_TAG_TYPE[auth.user?.role ?? ''] ?? 'info')

/** 平台侧账号。他们不参与买卖双方那套流程，「成为商家」对他们没有意义 */
const isPlatformRole = computed(() => auth.user?.role === 'admin' || auth.user?.role === 'finance')

/** 与后端 `account/models.py` 的 gender 注释一致：0未知 1男 2女 */
const GENDER_TEXT: Record<number, string> = { 0: '保密', 1: '男', 2: '女' }

const emptyForm = (): AddressInput => ({
  receiverName: '',
  phone: '',
  province: '',
  city: '',
  district: '',
  detail: '',
  regionCode: '',
  tag: null,
  isDefault: false,
})

const form = ref<AddressInput>(emptyForm())

/** 与后端 `account/schemas.py` 的 PHONE_PATTERN 保持一致 */
const PHONE_PATTERN = /^1[3-9]\d{9}$/
const REGION_CODE_PATTERN = /^\d{6}$/

/**
 * 收货手机号的实时指示：绿勾 / 红叉。
 *
 * ★ 和登录页同一条规则（复用了上面的 PHONE_PATTERN，不另写一份正则），
 *   而不是"够 11 位就行" —— 11 位但号段不对照样过不了后端校验。
 *   空着不显示图标。
 */
const phoneState = computed<'idle' | 'ok' | 'bad'>(() => {
  if (!form.value.phone) return 'idle'
  return PHONE_PATTERN.test(form.value.phone) ? 'ok' : 'bad'
})

/**
 * 省市区是三格输入、一个表单项。
 *
 * 后端把三者都设为必填，这里必须一起判 —— 否则用户填了省市漏了区，
 * 照样只会在提交后收到一句「参数错误」。
 */
function validateRegion(
  _rule: unknown,
  _value: unknown,
  callback: (error?: Error) => void,
): void {
  const { province, city, district } = form.value
  if (province.trim() && city.trim() && district.trim()) callback()
  else callback(new Error('请填写完整的省、市、区'))
}

const rules: FormRules = {
  receiverName: [{ required: true, message: '请填写收货人', trigger: 'blur' }],
  phone: [
    { required: true, message: '请填写手机号', trigger: 'blur' },
    { pattern: PHONE_PATTERN, message: '手机号格式不对，应为 1 开头的 11 位数字', trigger: 'blur' },
  ],
  province: [{ required: true, validator: validateRegion, trigger: 'blur' }],
  detail: [{ required: true, message: '请填写详细地址', trigger: 'blur' }],
  regionCode: [
    { required: true, message: '请填写区划码', trigger: 'blur' },
    { pattern: REGION_CODE_PATTERN, message: '区划码是 6 位数字，如 310115', trigger: 'blur' },
  ],
}

/** 后端字段名 → 中文，用于兜底提示 */
const FIELD_LABELS: Record<string, string> = {
  receiverName: '收货人',
  phone: '手机号',
  province: '省份',
  city: '城市',
  district: '区县',
  detail: '详细地址',
  regionCode: '区划码',
}

/**
 * 前端规则已经挡掉了绝大多数情况；万一还是被后端拒了，
 * 至少说清是哪个字段，别再只弹一句「参数错误」。
 */
function saveErrorMessage(e: unknown): string {
  if (isBizError(e, ErrorCode.VALIDATION_ERROR)) {
    const errors = (e.data as { errors?: { field?: string }[] } | null)?.errors
    const field = errors?.[0]?.field
    if (field && FIELD_LABELS[field]) return `${FIELD_LABELS[field]}填写有误，请检查`
  }
  return isBizError(e) ? e.message : '保存失败'
}

async function load(): Promise<void> {
  loading.value = true
  try {
    addresses.value = await authApi.listAddresses()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载地址失败')
  } finally {
    loading.value = false
  }
}

function openDialog(): void {
  dialogVisible.value = true
  // 上一次留下的红色提示不该跟着带到这一次
  void nextTick(() => formRef.value?.clearValidate())
}

function openCreate(): void {
  editingId.value = null
  form.value = emptyForm()
  openDialog()
}

function openEdit(address: Address): void {
  editingId.value = address.id
  form.value = {
    receiverName: address.receiverName,
    phone: address.phone.replace(/\*/g, '0'),
    province: address.province,
    city: address.city,
    district: address.district,
    detail: address.detail,
    regionCode: address.regionCode,
    tag: address.tag,
    isDefault: address.isDefault,
  }
  openDialog()
}

async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  try {
    if (editingId.value) {
      await authApi.updateAddress(editingId.value, form.value)
      ElMessage.success('地址已更新')
    } else {
      await authApi.createAddress(form.value)
      ElMessage.success('地址已添加')
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(saveErrorMessage(e))
  }
}

async function onDelete(address: Address): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除「${address.receiverName}」这个地址吗？`, '删除地址', {
      type: 'warning',
    })
  } catch {
    return // 用户取消
  }
  try {
    await authApi.deleteAddress(address.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '删除失败')
  }
}

async function openShop(): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt('给你的店铺起个名字', '成为商家', {
      inputPattern: /^.{2,64}$/,
      inputErrorMessage: '店铺名 2~64 个字符',
      confirmButtonText: '开通',
    })
    shopLoading.value = true
    await post('/merchant/shop', { name: value })
    await auth.restore()
    ElMessage.success('店铺已开通，可以到商家后台上架商品了')
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(isBizError(e) ? e.message : '开通失败')
  } finally {
    shopLoading.value = false
  }
}

onMounted(() => {
  void auth.restore()
  void load()
})
</script>

<template>
  <div class="account">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>账号信息</span>
          <el-button size="small" @click="profileVisible = true">编辑资料</el-button>
        </div>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="昵称">
          <div class="nick-cell">
            <img
              v-if="auth.user?.avatar"
              class="avatar"
              :src="auth.user.avatar"
              alt="头像"
              @error="onImageError"
            />
            <span>{{ auth.user?.nickname }}</span>
          </div>
        </el-descriptions-item>
        <el-descriptions-item label="手机号">{{ auth.user?.phone }}</el-descriptions-item>
        <el-descriptions-item label="角色">
          <el-tag :type="roleTagType">{{ roleText }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="性别">{{ GENDER_TEXT[auth.user?.gender ?? 0] }}</el-descriptions-item>
      </el-descriptions>

      <div class="shop-row">
        <template v-if="auth.user?.shopId">
          <span class="hint">店铺 ID：{{ auth.user.shopId }}</span>
        </template>
        <!-- 平台账号不该看到「成为商家」：它没有也不需要店铺，商家菜单在后台也不会出现 -->
        <template v-else-if="isPlatformRole">
          <span class="hint">当前是{{ roleText }}账号，不需要开店。</span>
        </template>
        <template v-else>
          <span class="hint">你还没有店铺。开通后可以在商家后台上架商品。</span>
          <el-button type="primary" :loading="shopLoading" @click="openShop">成为商家</el-button>
        </template>
      </div>
    </el-card>

    <el-card v-loading="loading" class="addresses" shadow="never">
      <template #header>
        <div class="card-header">
          <span>收货地址</span>
          <el-button type="primary" size="small" @click="openCreate">新增地址</el-button>
        </div>
      </template>

      <el-empty v-if="addresses.length === 0" description="还没有收货地址" />

      <el-table v-else :data="addresses">
        <el-table-column prop="receiverName" label="收货人" width="120" />
        <el-table-column prop="phone" label="电话" width="140" />
        <el-table-column label="地址" min-width="240">
          <template #default="{ row }">
            {{ row.province }}{{ row.city }}{{ row.district }}{{ row.detail }}
          </template>
        </el-table-column>
        <el-table-column label="默认" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.isDefault" type="success" size="small">默认</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑地址' : '新增地址'"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="收货人" prop="receiverName">
          <el-input v-model="form.receiverName" maxlength="64" />
        </el-form-item>
        <el-form-item label="手机号" prop="phone">
          <el-input v-model="form.phone" maxlength="11">
            <template #suffix>
              <span v-if="phoneState === 'ok'" class="phone-hint ok" title="手机号格式正确">✓</span>
              <span v-else-if="phoneState === 'bad'" class="phone-hint bad" title="手机号格式不对"
                >✕</span
              >
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="省市区" prop="province">
          <div class="region">
            <el-input v-model="form.province" placeholder="省" />
            <el-input v-model="form.city" placeholder="市" />
            <el-input v-model="form.district" placeholder="区" />
          </div>
        </el-form-item>
        <el-form-item label="详细地址" prop="detail">
          <el-input v-model="form.detail" maxlength="255" />
        </el-form-item>
        <el-form-item label="区划码" prop="regionCode">
          <el-input v-model="form.regionCode" placeholder="如 310115，运费计算用" maxlength="16" />
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.isDefault" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="onSubmit">保存</el-button>
      </template>
    </el-dialog>

    <ProfileEditDialog v-model="profileVisible" />
  </div>
</template>

<style scoped>
.account {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
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

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

/* 昵称一格：头像在前、名字在后，头像缺席时不留空位 */
.nick-cell {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.nick-cell .avatar {
  width: 24px;
  height: 24px;
  flex: 0 0 auto;
  border-radius: var(--radius-pill);
  object-fit: cover;
  background: var(--media-bg);
}

.shop-row {
  margin-top: var(--space-4);
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.hint {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.region {
  display: flex;
  gap: var(--space-2);
  width: 100%;
}
</style>
