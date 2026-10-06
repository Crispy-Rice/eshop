<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { codeToText, regionData } from 'element-china-area-data'

import { isBizError } from '@/api/errors'
import {
  createWarehouse,
  listWarehouses,
  replaceWarehouseRegions,
  setDefaultWarehouse,
  setWarehouseStatus,
  updateWarehouse,
  type Warehouse,
} from '@/api/inventory'

/**
 * 仓库管理。
 *
 * 多仓之后，"从哪个仓发货"由**收货区划**决定（见 `inventory/routing.py`）：
 * 下面每个仓配的"覆盖区域"就是它的发货范围，命中的按**码越长越具体**择优
 * （深圳 > 广东），都没命中就走**默认仓**。所以默认仓不能停用 —— 它是兜底。
 *
 * ★ 路由**不看库存**：某个仓没货不会自动改从别的仓发，那一单会直接失败并在
 *   结算页告诉买家"从哪个仓发、那里没货"。这是有意的取舍（否则运费会随库存波动）。
 */

/** 「全国兜底」不是真实地区，只能挂在级联最顶层（与运费模板页同一套写法） */
const REGION_ALL = '0'

const allOptions = [{ value: REGION_ALL, label: '全国兜底' }, ...regionData]

/** 覆盖区域：多选 + 可停在省/市/区任意一级，取的是**码**而不是路径 */
const coverProps = {
  multiple: true,
  checkStrictly: true,
  emitPath: false,
  value: 'value',
  label: 'label',
  children: 'children',
} as const

/** 仓库地址：单选、要完整路径（省市区三级都带出来） */
const addressProps = { value: 'value', label: 'label', children: 'children' } as const

const warehouses = ref<Warehouse[]>([])
const loading = ref(false)
const saving = ref(false)

const drawerVisible = ref(false)
/** null = 新建 */
const editingId = ref<string | null>(null)
const addressPath = ref<string[]>([])
const coveredRegions = ref<string[]>([])

const form = reactive({
  name: '',
  regionCode: '',
  province: '',
  city: '',
  district: '',
  detail: '',
  contactName: '',
  contactPhone: '',
})

const isEditing = computed(() => editingId.value !== null)

async function load(): Promise<void> {
  loading.value = true
  try {
    warehouses.value = await listWarehouses()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载仓库失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 区划码 → 中文名。码是前缀结构，但展示直接用码表查更省事 */
function regionText(code: string): string {
  if (code === REGION_ALL) return '全国兜底'
  return codeToText[code] ?? code
}

function addressText(w: Warehouse): string {
  const area = [w.province, w.city, w.district].filter(Boolean).join(' ')
  const parts = [area, w.detail].filter(Boolean)
  return parts.length > 0 ? parts.join(' ') : '未填写'
}

/** 地址级联选中 → 同时把省市区名与最细的码写进表单 */
function onAddressPick(codes: unknown): void {
  const path = Array.isArray(codes) ? (codes as string[]) : []
  addressPath.value = path
  form.regionCode = path[path.length - 1] ?? ''
  form.province = path[0] ? (codeToText[path[0]] ?? '') : ''
  form.city = path[1] ? (codeToText[path[1]] ?? '') : ''
  form.district = path[2] ? (codeToText[path[2]] ?? '') : ''
}

/** 已有的码 → 级联路径（回显） */
function pathOf(code: string): string[] {
  if (!code) return []
  if (code.length === 6) return [code.slice(0, 2), code.slice(0, 4), code]
  if (code.length === 4) return [code.slice(0, 2), code]
  return [code]
}

function openCreate(): void {
  editingId.value = null
  Object.assign(form, {
    name: '',
    regionCode: '',
    province: '',
    city: '',
    district: '',
    detail: '',
    contactName: '',
    contactPhone: '',
  })
  addressPath.value = []
  coveredRegions.value = []
  drawerVisible.value = true
}

function openEdit(w: Warehouse): void {
  editingId.value = w.id
  Object.assign(form, {
    name: w.name,
    regionCode: w.regionCode,
    province: w.province,
    city: w.city,
    district: w.district,
    detail: w.detail,
    contactName: w.contactName,
    contactPhone: w.contactPhone,
  })
  addressPath.value = pathOf(w.regionCode)
  coveredRegions.value = w.rules.map((r) => r.regionCode)
  drawerVisible.value = true
}

async function onSave(): Promise<void> {
  if (!form.name.trim()) {
    ElMessage.warning('请填仓库名称')
    return
  }
  saving.value = true
  try {
    if (editingId.value === null) {
      const created = await createWarehouse({ ...form })
      // ★ 建仓会顺手给该店全部 SKU 在这个仓补 0 库存行 —— 提醒他下一步做什么
      ElMessage.success(
        `已建仓，并为 ${created.stockedSkus} 个规格预建了库存行 —— 去「库存」页给这个仓填数量`,
      )
      await replaceWarehouseRegions(created.id, coveredRegions.value)
    } else {
      await updateWarehouse(editingId.value, { ...form })
      // 覆盖区域是**整体替换**：提交什么就是什么
      await replaceWarehouseRegions(editingId.value, coveredRegions.value)
      ElMessage.success('已保存')
    }
    drawerVisible.value = false
    await load()
  } catch (e) {
    // 「这个地区已经分给别的仓了」就属于这里 —— 后端会点名是哪个地区、归谁
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function onSetDefault(w: Warehouse): Promise<void> {
  try {
    await setDefaultWarehouse(w.id)
    ElMessage.success(`已把「${w.name}」设为默认仓`)
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '设置失败')
  }
}

async function onToggleStatus(w: Warehouse): Promise<void> {
  const next = w.status === 1 ? 2 : 1
  if (next === 2) {
    try {
      await ElMessageBox.confirm(
        `停用后新订单不会再从这个仓发货，但已经落在它上面的订单照常发货。确定停用「${w.name}」吗？`,
        '停用仓库',
        { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  try {
    await setWarehouseStatus(w.id, next)
    ElMessage.success(next === 2 ? '已停用' : '已启用')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '操作失败')
  }
}
</script>

<template>
  <div class="page">
    <div class="head">
      <div>
        <h2 class="title">仓库</h2>
        <p class="lead">
          买家下单时按收货地址所在的地区决定从哪个仓发货：命中「覆盖区域」的仓发货，
          都没命中就走默认仓。区域可以配到省 / 市 / 区任意一级，越具体越优先
          （「深圳」比「广东」优先）。那个仓没货时会自动从别的仓发
          （先看默认仓，再按顺序找其他启用的仓），所以一个地区不必只靠一个仓。
        </p>
      </div>
      <el-button type="primary" @click="openCreate">新建仓库</el-button>
    </div>

    <el-table v-loading="loading" :data="warehouses" size="small">
      <el-table-column label="仓库" min-width="160">
        <template #default="{ row }">
          <span class="name">{{ row.name }}</span>
          <el-tag v-if="row.isDefault" size="small" type="success" class="tag">默认</el-tag>
          <el-tag v-else-if="row.status === 2" size="small" type="info" class="tag">已停用</el-tag>
        </template>
      </el-table-column>

      <el-table-column label="地址" min-width="240">
        <template #default="{ row }">{{ addressText(row) }}</template>
      </el-table-column>

      <el-table-column label="联系人" min-width="150">
        <template #default="{ row }">
          <span v-if="row.contactName || row.contactPhone">
            {{ row.contactName }}<span v-if="row.contactPhone"> · {{ row.contactPhone }}</span>
          </span>
          <span v-else class="muted">未填写</span>
        </template>
      </el-table-column>

      <el-table-column label="覆盖区域" min-width="220">
        <template #default="{ row }">
          <template v-if="row.rules.length > 0">
            <el-tag
              v-for="r in row.rules"
              :key="r.id"
              size="small"
              effect="plain"
              class="tag"
            >
              {{ regionText(r.regionCode) }}
            </el-tag>
          </template>
          <span v-else class="muted">只发默认（全国兜底走默认仓）</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="230" align="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button
            v-if="!row.isDefault"
            link
            type="primary"
            :disabled="row.status === 2"
            @click="onSetDefault(row)"
          >
            设为默认
          </el-button>
          <el-tooltip
            v-if="row.isDefault"
            content="默认仓不能停用：收货地址没命中任何区域规则时靠它兜底"
            placement="top"
          >
            <el-button link disabled>停用</el-button>
          </el-tooltip>
          <el-button v-else link type="primary" @click="onToggleStatus(row)">
            {{ row.status === 1 ? '停用' : '启用' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="drawerVisible" :title="isEditing ? '编辑仓库' : '新建仓库'" size="460px">
      <el-form :model="form" label-width="88px">
        <el-form-item label="仓库名称">
          <el-input v-model="form.name" maxlength="64" placeholder="如：华南仓" />
        </el-form-item>

        <el-form-item label="所在地区">
          <el-cascader
            class="w-full"
            :model-value="addressPath"
            :options="regionData"
            :props="addressProps"
            placeholder="选省 / 市 / 区"
            @change="onAddressPick"
          />
        </el-form-item>

        <el-form-item label="详细地址">
          <el-input v-model="form.detail" maxlength="255" placeholder="街道门牌" />
        </el-form-item>

        <el-form-item label="联系人">
          <el-input v-model="form.contactName" maxlength="64" />
        </el-form-item>

        <el-form-item label="联系电话">
          <el-input v-model="form.contactPhone" maxlength="20" />
        </el-form-item>

        <el-form-item label="覆盖区域">
          <el-cascader
            v-model="coveredRegions"
            class="w-full"
            :options="allOptions"
            :props="coverProps"
            placeholder="留空 = 只发默认（即没有命中其他仓的地区）"
          />
          <p class="hint">
            可以选多个，省 / 市 / 区任意一级都行。一个地区只能由一个仓发货 ——
            已经被别的仓占着的地方，保存时会提示是哪一个。
          </p>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="drawerVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.page {
  padding: 0;
}

.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.title {
  margin: 0 0 var(--space-2);
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.lead {
  max-width: 62em;
  font-size: var(--text-base);
  line-height: var(--leading-normal);
  color: var(--color-text-secondary);
}

.name {
  font-weight: var(--weight-medium);
}

.tag {
  margin-left: var(--space-1);
}

.muted {
  color: var(--color-text-tertiary);
}

.w-full {
  width: 100%;
}

.hint {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  line-height: var(--leading-normal);
  color: var(--color-text-tertiary);
}
</style>
