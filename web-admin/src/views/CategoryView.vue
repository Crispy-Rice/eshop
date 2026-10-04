<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import {
  MAX_CATEGORY_LEVEL,
  createCategory,
  deleteCategory,
  fetchAdminCategoryTree,
  moveCategory,
  updateCategory,
  type AdminCategory,
} from '@/api/category'
import { isBizError } from '@/api/errors'

const loading = ref(false)
const saving = ref(false)
const tree = ref<AdminCategory[]>([])

// ---------- 新建 ----------
const createVisible = ref(false)
const createParent = ref<AdminCategory | null>(null)
const createFormRef = ref<FormInstance>()
const createForm = reactive({ name: '', sort: 0 })

// ---------- 编辑（改名 / 排序 / 启停） ----------
const editVisible = ref(false)
const editingId = ref<string | null>(null)
const editFormRef = ref<FormInstance>()
const editForm = reactive({ name: '', sort: 0, status: 1 })

const nameRules: FormRules = {
  name: [{ required: true, message: '请填写类目名称', trigger: 'blur' }],
}

// ---------- 移动 ----------
const moveVisible = ref(false)
const moveNode = ref<AdminCategory | null>(null)
const moveTarget = ref<string | null>(null)
const moveToRoot = ref(false)

/** el-tree-select 的字段映射：disabled 用来禁掉不能当上级的节点 */
const treeSelectProps = {
  label: 'name',
  children: 'children',
  disabled: 'disabled',
} as const

interface MoveOption {
  id: string
  name: string
  disabled: boolean
  children: MoveOption[]
}

/**
 * 可选的"新上级"。
 *
 * 两层过滤：排掉被移动的节点自己（它整棵子树都会跟着消失），以及禁掉三级类目
 * （挂到它下面必然超过三级）。后端这两条也都会挡，这里只是让用户不必先撞一次墙。
 */
function toMoveOptions(nodes: AdminCategory[], skipId: string | undefined): MoveOption[] {
  return nodes
    .filter((node) => node.id !== skipId)
    .map((node) => ({
      id: node.id,
      name: node.name,
      disabled: node.level >= MAX_CATEGORY_LEVEL,
      children: toMoveOptions(node.children, skipId),
    }))
}

const moveOptions = computed(() => toMoveOptions(tree.value, moveNode.value?.id))

async function load(): Promise<void> {
  loading.value = true
  try {
    tree.value = await fetchAdminCategoryTree()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载类目失败')
  } finally {
    loading.value = false
  }
}

// ---------- 新建 ----------
function openCreate(parent: AdminCategory | null): void {
  createParent.value = parent
  createForm.name = ''
  createForm.sort = 0
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  const valid = await createFormRef.value?.validate().catch(() => false)
  if (!valid) return

  saving.value = true
  try {
    await createCategory(createForm.name.trim(), createParent.value?.id ?? null, createForm.sort)
    ElMessage.success('类目已新建')
    createVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '新建失败')
  } finally {
    saving.value = false
  }
}

// ---------- 编辑 ----------
function openEdit(node: AdminCategory): void {
  editingId.value = node.id
  editForm.name = node.name
  editForm.sort = node.sort
  editForm.status = node.status
  editVisible.value = true
}

async function submitEdit(): Promise<void> {
  const valid = await editFormRef.value?.validate().catch(() => false)
  if (!valid || editingId.value === null) return

  saving.value = true
  try {
    await updateCategory(editingId.value, {
      name: editForm.name.trim(),
      sort: editForm.sort,
      status: editForm.status,
    })
    ElMessage.success('已保存')
    editVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}

// ---------- 移动 ----------
function openMove(node: AdminCategory): void {
  moveNode.value = node
  moveTarget.value = null
  moveToRoot.value = false
  moveVisible.value = true
}

async function submitMove(): Promise<void> {
  if (moveNode.value === null) return
  if (!moveToRoot.value && moveTarget.value === null) {
    ElMessage.warning('请选择新的上级类目，或勾选"设为一级类目"')
    return
  }

  saving.value = true
  try {
    await moveCategory(moveNode.value.id, moveToRoot.value ? null : moveTarget.value)
    ElMessage.success('已移动')
    moveVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '移动失败')
  } finally {
    saving.value = false
  }
}

// ---------- 删除 ----------
async function onDelete(node: AdminCategory): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除类目「${node.name}」吗？`, '删除类目', { type: 'warning' })
  } catch {
    return // 用户取消
  }

  try {
    await deleteCategory(node.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    // ★ 原样弹后端文案。"该类目下还有 3 件商品，无法删除"比"删除失败"有用得多 ——
    //   运营看到就知道下一步该去干什么。
    ElMessage.error(isBizError(e) ? e.message : '删除失败')
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">商品类目</h2>
      <span class="hint">最多三级；商品只能挂在末级类目下</span>
      <div class="spacer" />
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <el-button size="small" type="primary" @click="openCreate(null)">新建一级类目</el-button>
    </div>

    <div v-loading="loading" class="panel">
      <el-empty v-if="!loading && tree.length === 0" description="还没有类目">
        <p class="empty-hint">商家发布商品时要选末级类目，先把树建起来</p>
      </el-empty>

      <el-tree
        v-else
        class="cat-tree"
        :data="tree"
        node-key="id"
        default-expand-all
        :expand-on-click-node="false"
        :indent="20"
      >
        <template #default="{ data }">
          <div class="node">
            <span class="node-name" :class="{ off: data.status !== 1 }">{{ data.name }}</span>
            <el-tag v-if="data.status !== 1" type="info" size="small" disable-transitions>停用</el-tag>
            <span v-if="data.spuCount > 0" class="node-count tnum">{{ data.spuCount }} 件商品</span>

            <div class="node-actions">
              <el-button
                v-if="data.level < MAX_CATEGORY_LEVEL"
                link
                type="primary"
                size="small"
                @click.stop="openCreate(data)"
              >
                新增子类目
              </el-button>
              <el-button link type="primary" size="small" @click.stop="openEdit(data)">编辑</el-button>
              <el-button link type="primary" size="small" @click.stop="openMove(data)">移动</el-button>
              <el-button link type="danger" size="small" @click.stop="onDelete(data)">删除</el-button>
            </div>
          </div>
        </template>
      </el-tree>
    </div>

    <!-- 新建 -->
    <el-dialog
      v-model="createVisible"
      :title="createParent ? `在「${createParent.name}」下新建子类目` : '新建一级类目'"
      width="440px"
      :close-on-click-modal="false"
    >
      <el-form ref="createFormRef" :model="createForm" :rules="nameRules" label-width="72px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="createForm.name" maxlength="32" show-word-limit />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="createForm.sort" :controls="false" class="w120" />
          <span class="form-hint">数字越小越靠前</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">保存</el-button>
      </template>
    </el-dialog>

    <!-- 编辑 -->
    <el-dialog
      v-model="editVisible"
      title="编辑类目"
      width="440px"
      :close-on-click-modal="false"
    >
      <el-form ref="editFormRef" :model="editForm" :rules="nameRules" label-width="72px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="editForm.name" maxlength="32" show-word-limit />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="editForm.sort" :controls="false" class="w120" />
          <span class="form-hint">数字越小越靠前</span>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch
            v-model="editForm.status"
            :active-value="1"
            :inactive-value="2"
            active-text="启用"
            inactive-text="停用"
          />
          <span class="form-hint">停用后商城筛选与商家选类目都看不到它</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 移动 -->
    <el-dialog v-model="moveVisible" title="移动类目" width="460px" :close-on-click-modal="false">
      <el-form label-width="72px">
        <el-form-item label="类目">
          <span class="node-name">{{ moveNode?.name }}</span>
        </el-form-item>
        <el-form-item label="新上级">
          <el-tree-select
            v-model="moveTarget"
            class="w-full"
            :data="moveOptions"
            :props="treeSelectProps"
            node-key="id"
            check-strictly
            default-expand-all
            clearable
            :disabled="moveToRoot"
            placeholder="选择新的上级类目"
          />
        </el-form-item>
        <el-form-item label=" ">
          <el-checkbox v-model="moveToRoot">设为一级类目（不挂在任何类目下）</el-checkbox>
        </el-form-item>
      </el-form>
      <p class="dialog-note">移动会连同它的全部子类目一起搬走，商城一侧立刻生效。</p>
      <template #footer>
        <el-button @click="moveVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitMove">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
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

/* --------------------------------------------------------------------------
 * 类目树
 *
 * 行内操作常显而不是 hover 才出现：这三四个动作是这一页的全部功能，
 * 藏起来会让"这页能干什么"变得不明显。
 * ------------------------------------------------------------------------*/

.cat-tree {
  padding: var(--space-2) var(--space-3) var(--space-3);
}

.cat-tree :deep(.el-tree-node__content) {
  height: 34px;
  border-radius: var(--radius-sm);
}

.node {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding-right: var(--space-2);
}

.node-name {
  font-size: var(--text-base);
  color: var(--color-text);
}

/* 停用的类目还留在树上，但视觉上退到后面 */
.node-name.off {
  color: var(--color-text-placeholder);
}

.node-count {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.node-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  flex: 0 0 auto;
}

/* Element Plus 给相邻按钮加了 margin-left，这里改用 gap 控制间距 */
.node-actions :deep(.el-button) {
  margin-left: 0;
}

.w120 {
  width: 120px;
}

.w-full {
  width: 100%;
}

.form-hint {
  margin-left: var(--space-2);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.dialog-note {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}
</style>
