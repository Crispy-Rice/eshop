<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as authApi from '@/api/auth'
import type { Address, AddressInput } from '@/api/auth'
import { isBizError } from '@/api/errors'
import { post } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const addresses = ref<Address[]>([])
const loading = ref(false)
const shopLoading = ref(false)
const dialogVisible = ref(false)
const editingId = ref<string | null>(null)

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

function openCreate(): void {
  editingId.value = null
  form.value = emptyForm()
  dialogVisible.value = true
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
  dialogVisible.value = true
}

async function onSubmit(): Promise<void> {
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
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
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
      <template #header>账号信息</template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="昵称">{{ auth.user?.nickname }}</el-descriptions-item>
        <el-descriptions-item label="手机号">{{ auth.user?.phone }}</el-descriptions-item>
        <el-descriptions-item label="角色">
          <el-tag :type="auth.user?.role === 'buyer' ? 'info' : 'success'">
            {{ auth.user?.role === 'buyer' ? '买家' : '商家' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="信用分">{{ auth.user?.creditScore }}</el-descriptions-item>
      </el-descriptions>

      <div class="shop-row">
        <template v-if="auth.user?.shopId">
          <span class="hint">店铺 ID：{{ auth.user.shopId }}</span>
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

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑地址' : '新增地址'" width="520px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="收货人">
          <el-input v-model="form.receiverName" maxlength="64" />
        </el-form-item>
        <el-form-item label="手机号">
          <el-input v-model="form.phone" maxlength="11" />
        </el-form-item>
        <el-form-item label="省市区">
          <div class="region">
            <el-input v-model="form.province" placeholder="省" />
            <el-input v-model="form.city" placeholder="市" />
            <el-input v-model="form.district" placeholder="区" />
          </div>
        </el-form-item>
        <el-form-item label="详细地址">
          <el-input v-model="form.detail" maxlength="255" />
        </el-form-item>
        <el-form-item label="区划码">
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
  </div>
</template>

<style scoped>
.account {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.shop-row {
  margin-top: 16px;
  display: flex;
  align-items: center;
  gap: 16px;
}

.hint {
  font-size: 13px;
  color: #909399;
}

.region {
  display: flex;
  gap: 8px;
  width: 100%;
}
</style>
