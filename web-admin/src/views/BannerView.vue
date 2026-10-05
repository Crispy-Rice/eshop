<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  BANNER_STATUS_TEXT,
  createBanner,
  deleteBanner,
  listBanners,
  updateBanner,
  type Banner,
} from '@/api/banner'
import { isBizError } from '@/api/errors'
import ImageUploader from '@/components/ImageUploader.vue'
import { onImageError } from '@/utils/placeholder'

/**
 * 首页轮播图管理。
 *
 * 权限是 admin + finance（后端 `/api/admin/banners` 的 AdminDep），和「营销」一致 ——
 * 都是运营投放内容。
 *
 * 排序用**数字字段**，不做拖拽：和类目管理页保持一致的取舍（拖拽会把"换顺序"
 * 和别的语义混在一次交互里）。
 */
const items = ref<Banner[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const saving = ref(false)
const editingId = ref<string | null>(null)

const form = reactive({
  title: '',
  image: '',
  /** 站内路径。空串提交时会转成 null（= 不可点） */
  linkUrl: '',
  sort: 0,
  status: 1,
})

async function load(): Promise<void> {
  loading.value = true
  try {
    items.value = await listBanners()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '加载失败')
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  editingId.value = null
  form.title = ''
  form.image = ''
  form.linkUrl = ''
  form.sort = items.value.length > 0 ? Math.max(...items.value.map((i) => i.sort)) + 10 : 10
  form.status = 1
  dialogVisible.value = true
}

function openEdit(row: Banner): void {
  editingId.value = row.id
  form.title = row.title
  form.image = row.image
  form.linkUrl = row.linkUrl ?? ''
  form.sort = row.sort
  form.status = row.status
  dialogVisible.value = true
}

async function submit(): Promise<void> {
  if (!form.title.trim()) {
    ElMessage.warning('请填写标题')
    return
  }
  if (!form.image) {
    ElMessage.warning('请上传图片')
    return
  }

  const body = {
    title: form.title.trim(),
    image: form.image,
    // 空串统一成 null：后端把空串当"清空链接"，两者都表示"不可点"
    linkUrl: form.linkUrl.trim() || null,
    sort: form.sort,
  }

  saving.value = true
  try {
    if (editingId.value) {
      await updateBanner(editingId.value, { ...body, status: form.status })
      ElMessage.success('已保存')
    } else {
      await createBanner(body)
      ElMessage.success('已新增')
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    // 校验类报错（比如链接不是站内路径）原样弹出来，比"保存失败"有用
    ElMessage.error(isBizError(e) ? e.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function remove(row: Banner): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除「${row.title}」吗？商城首页将不再显示它。`, '删除轮播图', {
      type: 'warning',
      confirmButtonText: '删除',
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return // 用户取消
  }

  try {
    await deleteBanner(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error(isBizError(e) ? e.message : '删除失败')
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="title">轮播图</h2>
      <span class="hint">商城首页顶部自动轮换，排序小的在前</span>
      <div class="spacer" />
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <el-button size="small" type="primary" @click="openCreate">新增轮播图</el-button>
    </div>

    <div class="panel">
      <el-empty v-if="!loading && items.length === 0" description="还没有轮播图">
        <p class="empty-hint">新增一张，商城首页立刻就会出现</p>
      </el-empty>

      <el-table v-else v-loading="loading" :data="items">
        <el-table-column label="图片" width="150">
          <template #default="{ row }">
            <img class="thumb" :src="row.image" :alt="row.title" @error="onImageError" />
          </template>
        </el-table-column>

        <el-table-column label="标题" min-width="160">
          <template #default="{ row }">
            <div class="cell-title">{{ row.title }}</div>
          </template>
        </el-table-column>

        <el-table-column label="跳转" min-width="200">
          <template #default="{ row }">
            <span v-if="row.linkUrl" class="tnum link">{{ row.linkUrl }}</span>
            <span v-else class="muted">不可点</span>
          </template>
        </el-table-column>

        <el-table-column label="排序" width="80" align="right">
          <template #default="{ row }">
            <span class="tnum">{{ row.sort }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 1 ? 'success' : 'info'" size="small" disable-transitions>
              {{ BANNER_STATUS_TEXT[row.status] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="130" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑轮播图' : '新增轮播图'"
      width="560px"
    >
      <el-form :model="form" label-width="90px">
        <el-form-item label="图片">
          <div class="image-row">
            <ImageUploader v-model="form.image" biz="banners" />
            <span class="inline-hint">建议宽幅图（如 1200×360），长边超 1280 会自动压缩</span>
          </div>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="form.title" maxlength="64" class="w-full" placeholder="运营自己看的名字" />
        </el-form-item>
        <el-form-item label="跳转">
          <el-input v-model="form.linkUrl" class="w-full" placeholder="站内路径，如 /products/123 或 /?categoryId=1" />
          <div class="field-hint">只能填站内路径（以 / 开头），留空表示这张图不可点</div>
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="form.sort" :controls="false" />
          <span class="inline-hint">数字小的排在前面</span>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch
            v-model="form.status"
            :active-value="1"
            :inactive-value="2"
            active-text="启用"
            inactive-text="停用"
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
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
  margin: 0;
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

.thumb {
  width: 120px;
  height: 40px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--media-bg);
}

.cell-title {
  font-size: var(--text-base);
  color: var(--color-text);
}

.muted {
  color: var(--color-text-placeholder);
}

.link {
  color: var(--color-text-secondary);
}

.w-full {
  width: 100%;
}

.image-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.inline-hint {
  margin-left: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}

.field-hint {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-placeholder);
}
</style>
