<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import {
  CHARGE_TYPE_TEXT,
  bindSku,
  createTemplate,
  fetchTemplateBinds,
  listExcludeRegions,
  listRegionRules,
  listTemplates,
  replaceExcludeRegions,
  replaceRegionRules,
  updateTemplate,
  type ExcludeRegion,
  type FreightRegionRule,
  type FreightTemplate,
  type RegionRuleInput,
  type SkuBind,
} from '@/api/freight'
import {
  listStock,
  listWarehouses,
  type StockItem,
  type Warehouse,
} from '@/api/inventory'
import { isBizError } from '@/api/errors'
import { formatYuan } from '@/utils/money'

const templates = ref<FreightTemplate[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const saving = ref(false)
const editingId = ref<string | null>(null)
/** 编辑时展示的"受影响 SKU 数"提示 */
const impactNotice = ref('')

const form = reactive({
  name: '',
  chargeType: 1,
  firstUnit: 1000,
  firstPrice: 1000,
  addUnit: 500,
  addPrice: 300,
  freeShipping: false,
  freeThreshold: 0,
  freeNum: 0,
  mergeType: 1,
  status: 1,
})

/** 区域规则与排除区：编辑时在同一个弹窗里维护 */
const regions = ref<RegionRuleInput[]>([])
const excludes = ref<{ regionCode: string; reason?: string }[]>([])

async function load(): Promise<void> {
  loading.value = true
  try {
    templates.value = await listTemplates()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载运费模板失败')
  } finally {
    loading.value = false
  }
}

// ============================================================
// SKU 绑定抽屉
//
// 列表里原先只有一个"已绑定 N 个 SKU"的数字，看不到绑了谁。
// 明细走新增的只读接口；候选 SKU 来自库存列表（它自带 skuId → 商品标题/规格），
// 所以选 SKU 和显示明细名用的是同一份数据，一次请求解决两处。
// ============================================================
const bindVisible = ref(false)
const bindTarget = ref<FreightTemplate | null>(null)
const binds = ref<SkuBind[]>([])
const bindLoading = ref(false)
const bindActing = ref(false)
const warehouses = ref<Warehouse[]>([])
const stockOptions = ref<StockItem[]>([])
const bindForm = reactive({ warehouseId: '', skuId: '', priority: 0 })

async function openBinds(row: FreightTemplate): Promise<void> {
  bindTarget.value = row
  bindVisible.value = true
  bindForm.warehouseId = ''
  bindForm.skuId = ''
  bindForm.priority = 0
  bindLoading.value = true
  try {
    const [bindRows, whs, stock] = await Promise.all([
      fetchTemplateBinds(row.id),
      listWarehouses(),
      listStock({ limit: 100 }),
    ])
    binds.value = bindRows
    warehouses.value = whs
    stockOptions.value = stock.items
    bindForm.warehouseId = whs[0]?.id ?? ''
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载绑定失败')
  } finally {
    bindLoading.value = false
  }
}

async function submitBind(): Promise<void> {
  if (!bindTarget.value) return
  if (!bindForm.skuId || !bindForm.warehouseId) {
    ElMessage.warning('要选仓库和 SKU')
    return
  }
  bindActing.value = true
  try {
    await bindSku({
      skuId: bindForm.skuId,
      templateId: bindTarget.value.id,
      warehouseId: bindForm.warehouseId,
      priority: bindForm.priority,
    })
    ElMessage.success('已绑定')
    binds.value = await fetchTemplateBinds(bindTarget.value.id)
    await load() // 刷新列表里的计数
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '绑定失败')
  } finally {
    bindActing.value = false
  }
}

function openCreate(): void {
  editingId.value = null
  impactNotice.value = ''
  Object.assign(form, {
    name: '',
    chargeType: 1,
    firstUnit: 1000,
    firstPrice: 1000,
    addUnit: 500,
    addPrice: 300,
    freeShipping: false,
    freeThreshold: 0,
    freeNum: 0,
    mergeType: 1,
    status: 1,
  })
  // 新建时预置一条"全国默认" —— 不配它的话其他地区都没规则可用，
  // 后端也会直接拒绝保存
  regions.value = [defaultRegion('0', 1)]
  excludes.value = []
  dialogVisible.value = true
}

function defaultRegion(code: string, level: number): RegionRuleInput {
  return {
    regionCode: code,
    regionLevel: level,
    firstUnit: form.firstUnit,
    firstPrice: form.firstPrice,
    addUnit: form.addUnit,
    addPrice: form.addPrice,
    freeShipping: false,
    enabled: true,
    priority: 0,
  }
}

async function openEdit(tpl: FreightTemplate): Promise<void> {
  editingId.value = tpl.id
  impactNotice.value = ''
  Object.assign(form, {
    name: tpl.name,
    chargeType: tpl.chargeType,
    firstUnit: tpl.firstUnit,
    firstPrice: tpl.firstPrice,
    addUnit: tpl.addUnit,
    addPrice: tpl.addPrice,
    freeShipping: tpl.freeShipping,
    freeThreshold: tpl.freeThreshold,
    freeNum: tpl.freeNum,
    mergeType: tpl.mergeType,
    status: tpl.status,
  })
  dialogVisible.value = true
  try {
    const [rs, es] = [await listRegionRules(tpl.id), await listExcludeRegions(tpl.id)]
    regions.value = rs.map((r: FreightRegionRule) => ({
      regionCode: r.regionCode,
      regionLevel: r.regionLevel,
      firstUnit: r.firstUnit,
      firstPrice: r.firstPrice,
      addUnit: r.addUnit,
      addPrice: r.addPrice,
      freeShipping: r.freeShipping,
      enabled: r.enabled,
      priority: r.priority,
    }))
    excludes.value = es.map((e: ExcludeRegion) => ({
      regionCode: e.regionCode,
      reason: e.reason ?? undefined,
    }))
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载模板明细失败')
  }
}

function addRegion(): void {
  regions.value.push(defaultRegion('', 1))
}

function removeRegion(index: number): void {
  regions.value.splice(index, 1)
}

function addExclude(): void {
  excludes.value.push({ regionCode: '', reason: '暂不配送' })
}

function removeExclude(index: number): void {
  excludes.value.splice(index, 1)
}

async function save(): Promise<void> {
  saving.value = true
  try {
    const payload = { ...form }
    if (editingId.value) {
      const result = await updateTemplate(editingId.value, payload)
      impactNotice.value = result.notice
      ElMessage.success(`模板已更新（影响 ${result.affectedSkuCount} 个 SKU）`)
    } else {
      const created = await createTemplate(payload)
      editingId.value = created.id
      ElMessage.success('模板已创建')
    }

    const tplId = editingId.value
    // 区域与排除区一起提交，保证"必须有一条全国默认"的校验能看到完整的那组规则
    await replaceRegionRules(
      tplId,
      regions.value.filter((r) => r.regionCode.trim() !== ''),
    )
    await replaceExcludeRegions(
      tplId,
      excludes.value.filter((e) => e.regionCode.trim() !== ''),
    )
    await load()
  } catch (e) {
    // 配置校验的报错要如实展示 —— 尤其是"必须保留一条全国默认"
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">运费模板</h2>
      <span class="hint">同一仓库发出只收一次首重；不同仓库分别计费</span>
      <div class="spacer" />
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <el-button size="small" type="primary" @click="openCreate">新建模板</el-button>
    </div>

    <div class="panel">
      <el-empty v-if="!loading && templates.length === 0" description="还没有运费模板">
        <p class="empty-hint">商品上架前必须绑定运费模板，否则无法下单</p>
      </el-empty>

      <el-table v-else v-loading="loading" :data="templates">
        <el-table-column label="模板" min-width="200">
          <template #default="{ row }">
            <div class="cell-title">{{ row.name }}</div>
            <div class="cell-sub">已绑定 {{ row.boundSkuCount }} 个 SKU</div>
          </template>
        </el-table-column>

        <el-table-column label="计费方式" width="110">
          <template #default="{ row }">
            <el-tag size="small" disable-transitions>{{ CHARGE_TYPE_TEXT[row.chargeType] }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="首重" width="150">
          <template #default="{ row }">
            <span class="tnum">
              {{ row.chargeType === 1 ? `${row.firstUnit}g` : `${row.firstUnit} 件` }}
              / ¥{{ formatYuan(row.firstPrice) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="续重" width="170">
          <template #default="{ row }">
            <span class="tnum">
              {{ row.chargeType === 1 ? `${row.addUnit}g` : `${row.addUnit} 件` }}
              / ¥{{ formatYuan(row.addPrice) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="包邮" width="140">
          <template #default="{ row }">
            <el-tag v-if="row.freeShipping" type="success" size="small" disable-transitions>
              全场包邮
            </el-tag>
            <span v-else-if="row.freeThreshold > 0" class="tnum muted">
              满 ¥{{ formatYuan(row.freeThreshold) }}
            </span>
            <span v-else-if="row.freeNum > 0" class="muted">满 {{ row.freeNum }} 件</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 1 ? 'success' : 'info'" size="small" disable-transitions>
              {{ row.status === 1 ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="170" align="right">
          <template #default="{ row }">
            <!-- 绑定入口放在操作列："已绑定 N 个 SKU" 那行是状态，不是按钮。
                 之前把它做成可点的副标题，商家根本找不到绑定的地方。 -->
            <el-button link type="primary" @click="openBinds(row)">绑定商品</el-button>
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑运费模板' : '新建运费模板'"
      width="760px"
    >
      <el-alert v-if="impactNotice" type="warning" :closable="false" class="mb">
        {{ impactNotice }}
      </el-alert>

      <el-form :model="form" label-width="96px" class="form">
        <el-form-item label="模板名">
          <el-input v-model="form.name" maxlength="64" class="w-240" />
        </el-form-item>
        <el-form-item label="计费方式">
          <el-radio-group v-model="form.chargeType">
            <el-radio-button :value="1">按重量</el-radio-button>
            <el-radio-button :value="2">按件数</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item :label="form.chargeType === 1 ? '首重（克）' : '首件数'">
          <el-input-number v-model="form.firstUnit" :min="0" :controls="false" class="w-160" />
          <span class="inline-hint">主地区以外没配规则的走这里</span>
        </el-form-item>
        <el-form-item label="首重价（分）">
          <el-input-number v-model="form.firstPrice" :min="0" :controls="false" class="w-160" />
          <span class="inline-hint">{{ formatYuan(form.firstPrice) }} 元</span>
        </el-form-item>
        <el-form-item :label="form.chargeType === 1 ? '续重（克）' : '续件数'">
          <el-input-number v-model="form.addUnit" :min="1" :controls="false" class="w-160" />
        </el-form-item>
        <el-form-item label="续重价（分）">
          <el-input-number v-model="form.addPrice" :min="0" :controls="false" class="w-160" />
          <span class="inline-hint">{{ formatYuan(form.addPrice) }} 元</span>
        </el-form-item>

        <el-form-item label="包邮">
          <el-checkbox v-model="form.freeShipping">全场包邮</el-checkbox>
        </el-form-item>
        <el-form-item label="满额包邮（分）">
          <el-input-number v-model="form.freeThreshold" :min="0" :controls="false" class="w-160" />
          <span class="inline-hint">0 = 不参与</span>
        </el-form-item>
        <el-form-item label="满件包邮">
          <el-input-number v-model="form.freeNum" :min="0" :controls="false" class="w-160" />
          <span class="inline-hint">0 = 不参与</span>
        </el-form-item>
        <el-form-item v-if="editingId" label="状态">
          <el-radio-group v-model="form.status">
            <el-radio-button :value="1">启用</el-radio-button>
            <el-radio-button :value="2">停用</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <el-divider>区域规则</el-divider>
      <p class="section-hint">
        ★ 配置了区域规则就**必须保留一条「全国默认」**（区域码填 <code>0</code>），
        否则其他地区一条规则都匹配不到，用户会被"该地区不配送"拦住。
      </p>

      <el-table :data="regions" size="small" class="mini-table">
        <el-table-column label="区域码" width="140">
          <template #default="{ row }">
            <el-input v-model="row.regionCode" size="small" placeholder="0 或 310115" />
          </template>
        </el-table-column>
        <el-table-column label="层级" width="110">
          <template #default="{ row }">
            <el-select v-model="row.regionLevel" size="small">
              <el-option :value="1" label="省" />
              <el-option :value="2" label="市" />
              <el-option :value="3" label="区" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="首重" width="110">
          <template #default="{ row }">
            <el-input-number v-model="row.firstUnit" size="small" :min="0" :controls="false" />
          </template>
        </el-table-column>
        <el-table-column label="首重价(分)" width="120">
          <template #default="{ row }">
            <el-input-number v-model="row.firstPrice" size="small" :min="0" :controls="false" />
          </template>
        </el-table-column>
        <el-table-column label="续重" width="110">
          <template #default="{ row }">
            <el-input-number v-model="row.addUnit" size="small" :min="1" :controls="false" />
          </template>
        </el-table-column>
        <el-table-column label="续重价(分)" width="120">
          <template #default="{ row }">
            <el-input-number v-model="row.addPrice" size="small" :min="0" :controls="false" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70">
          <template #default="{ $index }">
            <el-button link type="danger" @click="removeRegion($index)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button size="small" class="mt" @click="addRegion">添加区域规则</el-button>

      <el-divider>不发货区域</el-divider>
      <el-table :data="excludes" size="small" class="mini-table">
        <el-table-column label="区域码" width="180">
          <template #default="{ row }">
            <el-input v-model="row.regionCode" size="small" placeholder="如 65（新疆）" />
          </template>
        </el-table-column>
        <el-table-column label="原因">
          <template #default="{ row }">
            <el-input v-model="row.reason" size="small" maxlength="64" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70">
          <template #default="{ $index }">
            <el-button link type="danger" @click="removeExclude($index)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button size="small" class="mt" @click="addExclude">添加不发货区域</el-button>

      <template #footer>
        <el-button @click="dialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- ============ SKU 绑定 ============ -->
    <el-drawer
      v-model="bindVisible"
      :title="`绑定商品 · ${bindTarget?.name ?? ''}`"
      size="660px"
    >
      <div v-loading="bindLoading" class="bind-body">
        <h4 class="bind-h">新增绑定</h4>
        <el-form :model="bindForm" label-width="90px">
          <el-form-item label="发货仓库">
            <el-select v-model="bindForm.warehouseId" style="width: 100%">
              <el-option v-for="w in warehouses" :key="w.id" :label="w.name" :value="w.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="商品 SKU">
            <el-select
              v-model="bindForm.skuId"
              filterable
              style="width: 100%"
              placeholder="按商品标题或规格搜索"
            >
              <el-option
                v-for="s in stockOptions"
                :key="s.skuId"
                :label="`${s.spuTitle} · ${s.specText}`"
                :value="s.skuId"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="优先级">
            <el-input-number v-model="bindForm.priority" :controls="false" />
            <span class="inline-hint">同一 SKU 绑多个模板时，大的生效</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="bindActing" @click="submitBind">
              绑定
            </el-button>
          </el-form-item>
        </el-form>

        <h4 class="bind-h">已绑定（{{ binds.length }}）</h4>
        <el-empty
          v-if="binds.length === 0"
          description="这个模板还没有绑定任何 SKU"
          :image-size="60"
        />
        <el-table v-else :data="binds" size="small">
          <el-table-column label="商品" min-width="240">
            <template #default="{ row }">
              <div class="cell-title">{{ row.spuTitle }}</div>
              <div class="cell-sub">{{ row.specText }} · {{ row.skuCode }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="warehouseName" label="仓库" width="110" />
          <el-table-column label="优先级" width="80" align="right">
            <template #default="{ row }">
              <span class="tnum">{{ row.priority }}</span>
            </template>
          </el-table-column>
        </el-table>

        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="当前不支持解绑：绑错了只能改库，解绑接口后续再补"
        />
      </div>
    </el-drawer>
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
  align-items: baseline;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.hint {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.spacer {
  flex: 1;
}

.panel {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  padding: var(--space-1) 0;
}

.panel :deep(.el-empty) {
  padding: var(--space-8) 0;
}

.empty-hint {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text-placeholder);
}

.cell-title {
  font-size: var(--text-base);
  color: var(--color-text);
}

.cell-sub {
  margin-top: 2px;
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.muted {
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}

.form {
  margin-bottom: var(--space-2);
}

.w-160 {
  width: 160px;
}

.w-240 {
  width: 240px;
}

.inline-hint {
  margin-left: var(--space-3);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.section-hint {
  margin-bottom: var(--space-3);
  padding: var(--space-3);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  line-height: var(--leading-normal);
}

.section-hint code {
  padding: 0 4px;
  border-radius: var(--radius-xs);
  background: var(--color-bg-surface);
  color: var(--color-accent);
}

.mini-table {
  width: 100%;
}

.mt {
  margin-top: var(--space-3);
}

.mb {
  margin-bottom: var(--space-4);
}

/* ---------- 绑定抽屉 ---------- */

.bind-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.bind-h {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}
</style>
