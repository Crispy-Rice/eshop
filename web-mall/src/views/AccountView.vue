<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { codeToText, regionData } from 'element-china-area-data'

import * as authApi from '@/api/auth'
import type { Address, AddressInput } from '@/api/auth'
import { ErrorCode, isBizError } from '@/api/errors'
import { post } from '@/api/http'
import ChangePasswordDialog from '@/components/ChangePasswordDialog.vue'
import ProfileEditDialog from '@/components/ProfileEditDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { onImageError } from '@/utils/placeholder'
import { ADMIN_APP_URL } from '@/utils/siblingApp'

const auth = useAuthStore()
const router = useRouter()
const addresses = ref<Address[]>([])
const loading = ref(false)
const shopLoading = ref(false)
const dialogVisible = ref(false)
const profileVisible = ref(false)
const pwdVisible = ref(false)
const deactivating = ref(false)
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

/**
 * 省市区三级联动的选中路径（省码 / 市码 / 区码）。
 *
 * ★ 它是一个**独立的 UI 状态**，不属于表单：表单里存的是"名字 + 区划码"
 *   （后端要的形状），而级联控件要的是路径。两者靠下面两个函数来回翻译。
 */
const regionPath = ref<string[]>([])

/**
 * 区划码 → 级联路径。
 *
 * 行政区划码（GB/T 2260）是**前缀结构**：``310115``（浦东新区）的前 2 位是上海市、
 * 前 4 位是上海市辖区。所以拿着区码就能反推出省市两级，不必另存一份路径。
 * 运费那边的区域匹配用的正是同一条性质（``freight/calculator.region_matches``）。
 */
function pathFromCode(code: string): string[] {
  if (code.length === 6) return [code.slice(0, 2), code.slice(0, 4), code]
  if (code.length === 4) return [code.slice(0, 2), code]
  return code ? [code] : []
}

/**
 * 选中即带出名字与区划码。
 *
 * ★ 用户**全程看不到那串数字**，也就不可能填错 —— 原来那栏
 *   「区划码，如 310115，运费计算用」只有开发者看得懂，填错了也不报错，
 *   只会在结算时匹配不到运费规则。
 */
function onRegionChange(codes: string[] | null): void {
  const path = codes ?? []
  const [province, city, district] = path
  form.value.province = province ? (codeToText[province] ?? '') : ''
  form.value.city = city ? (codeToText[city] ?? '') : ''
  form.value.district = district ? (codeToText[district] ?? '') : ''
  // 取最细一级做区划码。运费是按前缀匹配的，省码也能用，但能细就细
  form.value.regionCode = path[path.length - 1] ?? ''
}

/** 与后端 `account/schemas.py` 的 PHONE_PATTERN 保持一致 */
const PHONE_PATTERN = /^1[3-9]\d{9}$/

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
 * 省市区是**一个**表单项（三级联动的结果）。
 *
 * 后端把三者都设为必填，这里必须一起判 —— 只选到市没选到区，
 * 照样只会在提交后收到一句「参数错误」。
 */
function validateRegion(
  _rule: unknown,
  _value: unknown,
  callback: (error?: Error) => void,
): void {
  const { province, city, district } = form.value
  if (province.trim() && city.trim() && district.trim()) callback()
  else callback(new Error('请选择完整的省、市、区'))
}

const rules: FormRules = {
  receiverName: [{ required: true, message: '请填写收货人', trigger: 'blur' }],
  phone: [
    { required: true, message: '请填写手机号', trigger: 'blur' },
    { pattern: PHONE_PATTERN, message: '手机号格式不对，应为 1 开头的 11 位数字', trigger: 'blur' },
  ],
  // 触发时机是 change 不是 blur：值来自级联控件的选择，没有"失焦"这个动作
  province: [{ required: true, validator: validateRegion, trigger: 'change' }],
  detail: [{ required: true, message: '请填写详细地址', trigger: 'blur' }],
  // ★ regionCode 没有规则：它由选中的区县带出来，用户碰不到，也就无所谓校验
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
  regionPath.value = []
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
  // 编辑时手上只有区码（库里没存省市各自的码），靠前缀规则反推，级联控件才回显
  regionPath.value = pathFromCode(address.regionCode)
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

/**
 * 注销账号。三道关：
 *
 * 1. **先问后端能不能注销**（`GET /me/deactivation`）。被挡就弹一个**能点去处理**的
 *    对话框然后结束 —— ★ 不能等输完密码才说，那等于让用户白输一次；原先还只是一句
 *    转瞬即逝的 toast，既不说去哪儿处理、用户也无从照着做。
 * 2. `confirm` 讲清后果（不可恢复、会匿名化什么、订单保留、手机号 30 天冷静期）；
 * 3. `prompt` 要当前密码 —— 没有短信验证码，这是唯一能证明"是本人操作"的手段，
 *    免得别人拿着你的登录态把你的号注销掉。
 *
 * ★ 提交时**仍可能**被挡（第 1 步与第 3 步之间状态会变：刚下单、刚开店），
 *   那里走同一套话术，而不是弹一句 toast。
 * ★ 三条守卫只有后端一份（`account/router._deactivation_blockers`），预检与提交共用
 *   —— 两处各写一遍必然漂，漂的后果是"说能注销、提交却被拒"，比没有预检更糟。
 */
async function onDeactivate(): Promise<void> {
  deactivating.value = true
  let check: authApi.DeactivationCheck
  try {
    check = await authApi.fetchDeactivationCheck()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '暂时无法检查注销条件')
    return
  } finally {
    deactivating.value = false
  }

  if (!check.canDeactivate) {
    await showDeactivationBlockers(check)
    return
  }

  try {
    await ElMessageBox.confirm(
      '注销后不可恢复：手机号、昵称、历史地址都会被匿名化，历史评价会显示为「已注销用户」。' +
        '订单与售后记录按法规保留。同一手机号 30 天之后才能重新注册。确定继续吗？',
      '注销账号',
      { type: 'warning', confirmButtonText: '继续', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return // 用户取消
  }

  let password: string
  try {
    const { value } = await ElMessageBox.prompt('请输入当前密码以确认是本人操作', '注销账号', {
      inputType: 'password',
      inputPlaceholder: '当前登录密码',
      confirmButtonText: '确认注销',
      confirmButtonClass: 'el-button--danger',
      inputValidator: (v: string) => (v ? true : '请输入密码'),
    })
    password = value
  } catch {
    return
  }

  deactivating.value = true
  try {
    await auth.deactivate(password)
    ElMessage.success('账号已注销')
    await router.push('/')
  } catch (e) {
    if (
      isBizError(e, ErrorCode.ACCOUNT_HAS_UNFINISHED) ||
      isBizError(e, ErrorCode.SHOP_OWNER_CANNOT_DEACTIVATE)
    ) {
      // 预检之后状态又变了。**重新查一次**再按同一套话术说 ——
      // 这样"去哪处理"的按钮仍然是点得动的（错误体里只有句子，没有数量）
      try {
        await showDeactivationBlockers(await authApi.fetchDeactivationCheck())
      } catch {
        await ElMessageBox.alert(e.message, '还不能注销', { confirmButtonText: '知道了' })
      }
      return
    }
    ElMessage.error(isBizError(e) ? e.message : '注销失败')
  } finally {
    deactivating.value = false
  }
}

/** 被挡时的「下一步去哪」：按被挡的项给一个主按钮（店铺 > 订单 > 售后）。 */
function blockerAction(
  check: authApi.DeactivationCheck,
): { label: string; go: () => void } | null {
  if (check.hasShop) {
    // 店主在商城端没有可操作的地方，得去商家后台
    return {
      label: '去商家后台',
      go: () => {
        window.location.href = ADMIN_APP_URL
      },
    }
  }
  if (check.orderCount > 0) {
    return { label: '去看我的订单', go: () => void router.push({ name: 'orders' }) }
  }
  if (check.refundCount > 0) {
    return { label: '去看退款/售后', go: () => void router.push({ name: 'refunds' }) }
  }
  return null
}

/**
 * 说清被什么挡着，并给一个**能点着去处理**的按钮。
 *
 * ★ 这正是这次改动要解决的问题：原先只弹一句 toast —— 转瞬即逝、不说去哪、
 *   也没告诉用户有几笔。现在看得到数量，也有下一步。
 */
async function showDeactivationBlockers(check: authApi.DeactivationCheck): Promise<void> {
  const lines: string[] = []
  if (check.hasShop) lines.push('名下有店铺（要先下架商品、结清订单）')
  if (check.orderCount > 0) lines.push(`${check.orderCount} 笔未完成订单`)
  if (check.refundCount > 0) lines.push(`${check.refundCount} 笔进行中的售后`)
  const action = blockerAction(check)
  try {
    await ElMessageBox.alert(
      `暂时不能注销，你还有：${lines.join('、')}。处理完之后才能注销。`,
      '还不能注销',
      {
        confirmButtonText: action?.label ?? '知道了',
        showCancelButton: action !== null,
        cancelButtonText: '知道了',
      },
    )
    action?.go()
  } catch {
    // 点了「知道了」
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

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>账号安全</span>
          <el-button size="small" @click="pwdVisible = true">修改密码</el-button>
        </div>
      </template>

      <div class="safety-row">
        <div>
          <p class="safety-title">登录密码</p>
          <p class="hint">定期更换更安全。改完密码后，你在其它设备上的登录会被退出。</p>
        </div>
      </div>

      <el-divider />

      <!-- 危险区：分隔线 + 灰色底 + danger 文字按钮，与上面的常规设置明确分开 -->
      <div class="safety-row danger">
        <div>
          <p class="safety-title">注销账号</p>
          <p class="hint">
            不可恢复：手机号、昵称、历史地址会被匿名化，评价显示为「已注销用户」；
            订单与售后记录按法规保留。有未完成的订单或售后、或名下有店铺时无法注销。
          </p>
        </div>
        <el-button type="danger" plain :loading="deactivating" @click="onDeactivate">
          注销账号
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>咨询与客服</span>
        </div>
      </template>

      <div class="safety-row">
        <div>
          <p class="safety-title">联系客服</p>
          <p class="hint">
            对订单、售后有疑问，或需要平台协助，在会话里说明即可，客服会回复你。
            如果账号登不进来（比如被冻结），请用登录页上的联系方式。
          </p>
        </div>
        <el-button type="primary" plain @click="$router.push({ name: 'support' })">
          去客服
        </el-button>
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
          <!-- 三级联动。**区划码由选中的区县带出来，用户不填也看不见** ——
               原来那一栏「区划码，如 310115，运费计算用」只有开发者看得懂，
               而且填错了不会报错，只会在结算时匹配不到运费规则。 -->
          <el-cascader
            v-model="regionPath"
            class="w-full"
            :options="regionData"
            :props="{ value: 'value', label: 'label' }"
            placeholder="请选择省 / 市 / 区"
            @change="onRegionChange"
          />
        </el-form-item>
        <el-form-item label="详细地址" prop="detail">
          <el-input v-model="form.detail" maxlength="255" />
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
    <ChangePasswordDialog v-model="pwdVisible" />
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

/* 「账号安全」卡里的一行设置：左边标题+说明，右边动作 */
.safety-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}

.safety-title {
  margin-bottom: var(--space-1);
  font-size: var(--text-base);
  color: var(--color-text);
}

/* 危险区靠底色调区分，不靠边框 —— 与后台的分层约定一致 */
.safety-row.danger {
  padding: var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-danger-soft);
}

.safety-row.danger .hint {
  color: var(--color-text-secondary);
}

/* 级联控件默认只有内容宽，撑满才和上面的输入框对齐 */
.w-full {
  width: 100%;
}
</style>
