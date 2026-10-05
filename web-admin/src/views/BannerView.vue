<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
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
import { fetchCategoryTree, type Category } from '@/api/category'
import { fetchPublicSpu, searchProducts } from '@/api/product'
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
 *
 * ★ 跳转**不让运营手填路径**，改成"选一个商品 / 选一个类目"。
 *   手填 `/products/9900…` 这种雪花 ID，运营根本不知道该填什么，填错了还是死链。
 */
const items = ref<Banner[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const saving = ref(false)
const editingId = ref<string | null>(null)

/** 跳转目标。none=不可点；custom 是留给非商品/类目页面的逃生口 */
type LinkType = 'none' | 'product' | 'category' | 'custom'

const form = reactive({
  title: '',
  image: '',
  linkType: 'none' as LinkType,
  /** 商品 id / 类目 id；custom 时就是原样路径 */
  linkValue: '',
  sort: 0,
  status: 1,
})

// 商品选择器的候选。按关键词远程搜（接口是公开的 /search，只回在售商品）
const productOptions = ref<{ id: string; title: string }[]>([])
const productLoading = ref(false)
const categories = ref<Category[]>([])

async function searchProduct(keyword: string): Promise<void> {
  productLoading.value = true
  try {
    const res = await searchProducts({ keyword: keyword || undefined, limit: 20 })
    productOptions.value = res.items.map((i) => ({ id: i.id, title: i.title }))
  } catch {
    productOptions.value = []
  } finally {
    productLoading.value = false
  }
}

/** 站内路径 →（类型, 值）。认不出来的一律算"自定义"，不丢数据 */
function parseLink(url: string | null): { linkType: LinkType; linkValue: string } {
  if (!url) return { linkType: 'none', linkValue: '' }
  const product = /^\/products\/(\d+)$/.exec(url)
  if (product?.[1]) return { linkType: 'product', linkValue: product[1] }
  const category = /^\/\?categoryId=(\d+)$/.exec(url)
  if (category?.[1]) return { linkType: 'category', linkValue: category[1] }
  return { linkType: 'custom', linkValue: url }
}

/** （类型, 值）→ 站内路径。选不出东西时当作不可点，而不是存一个半截路径 */
function buildLink(): string | null {
  if (form.linkType === 'none' || !form.linkValue) return null
  if (form.linkType === 'product') return `/products/${form.linkValue}`
  if (form.linkType === 'category') return `/?categoryId=${form.linkValue}`
  return form.linkValue
}

const LINK_TYPE_TEXT: Record<LinkType, string> = {
  none: '不可点',
  product: '商品',
  category: '类目',
  custom: '自定义',
}

/** 表格里那一列的分类标签，比一长串 `/products/9900…` 好读 */
function linkTypeOf(url: string | null): LinkType {
  return parseLink(url).linkType
}

const linkHint = computed(() => {
  switch (form.linkType) {
    case 'none':
      return '这张图不可点，纯展示'
    case 'product':
      return '点击后打开该商品详情页（只能选在售商品）'
    case 'category':
      return '点击后回到首页并筛选该类目（含它的子类目）'
    default:
      return '只能填站内路径（以 / 开头）'
  }
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
  form.linkType = 'none'
  form.linkValue = ''
  form.sort = items.value.length > 0 ? Math.max(...items.value.map((i) => i.sort)) + 10 : 10
  form.status = 1
  dialogVisible.value = true
}

async function openEdit(row: Banner): Promise<void> {
  editingId.value = row.id
  form.title = row.title
  form.image = row.image
  const parsed = parseLink(row.linkUrl)
  form.linkType = parsed.linkType
  form.linkValue = parsed.linkValue
  form.sort = row.sort
  form.status = row.status
  dialogVisible.value = true

  // 选中的商品要能显示出名字，否则下拉里只挂着一个雪花 ID
  if (parsed.linkType === 'product' && !productOptions.value.some((p) => p.id === parsed.linkValue)) {
    try {
      const spu = await fetchPublicSpu(parsed.linkValue)
      productOptions.value = [...productOptions.value, { id: spu.id, title: spu.title }]
    } catch {
      // 商品可能已下架/删除，那就只显示 id，别为它挡住弹窗
    }
  }
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
  if (form.linkType !== 'none' && !form.linkValue.trim()) {
    ElMessage.warning('请选择跳转目标，或把跳转改成「不跳转」')
    return
  }

  const body = {
    title: form.title.trim(),
    image: form.image,
    linkUrl: buildLink(),
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

onMounted(async () => {
  await load()
  // 类目树给"跳转=类目"用；商品先拉一页在售的当默认候选（选不完的靠远程搜）
  try {
    categories.value = await fetchCategoryTree()
  } catch {
    categories.value = []
  }
  await searchProduct('')
})
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

        <el-table-column label="跳转" width="110">
          <template #default="{ row }">
            <!-- 只出类型，不出路径：`/products/99005…` 对运营是噪音，
                 真要核对就去编辑弹窗里看选择器回显。
                 有跳转的给柔和底胶囊，没有的退成灰字 —— 一眼能分出哪些图可点 -->
            <el-tag v-if="row.linkUrl" size="small" round disable-transitions>
              {{ LINK_TYPE_TEXT[linkTypeOf(row.linkUrl)] }}
            </el-tag>
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
          <el-input v-model="form.title" maxlength="64" class="w-full" />
        </el-form-item>
        <el-form-item label="跳转">
          <div class="link-field">
            <el-radio-group v-model="form.linkType">
              <el-radio-button value="none">不跳转</el-radio-button>
              <el-radio-button value="product">商品</el-radio-button>
              <el-radio-button value="category">类目</el-radio-button>
              <el-radio-button value="custom">自定义</el-radio-button>
            </el-radio-group>

            <el-select
              v-if="form.linkType === 'product'"
              v-model="form.linkValue"
              filterable
              remote
              reserve-keyword
              :remote-method="searchProduct"
              :loading="productLoading"
              placeholder="输入商品名搜索"
              class="w-full"
            >
              <el-option v-for="p in productOptions" :key="p.id" :label="p.title" :value="p.id" />
            </el-select>

            <el-tree-select
              v-else-if="form.linkType === 'category'"
              v-model="form.linkValue"
              :data="categories"
              :props="{ label: 'name', children: 'children' }"
              node-key="id"
              check-strictly
              :render-after-expand="false"
              placeholder="选一个类目"
              class="w-full"
            />

            <el-input
              v-else-if="form.linkType === 'custom'"
              v-model="form.linkValue"
              class="w-full"
              placeholder="站内路径，如 /coupons"
            />
          </div>
          <div class="field-hint">{{ linkHint }}</div>
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
  font-size: var(--text-sm);
  color: var(--color-text-placeholder);
}

/* 跳转弹窗里那一组：类型单选在上，选择器在下，都占满宽度 */
.link-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  width: 100%;
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
