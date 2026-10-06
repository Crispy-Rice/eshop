# 17 前端设计系统与开发约定

> 本文档定义两个前端应用的视觉规范，以及**写新页面时必须遵守的 token 约定**。
> 约定部分是硬性的——违反它会让"大促可换肤"这个能力作废。
> 配色本身在代码里（`tokens.css`），本文不重复罗列色值，只讲规则和为什么。

## 1. 设计方向

| 维度 | 定位 |
|---|---|
| **主风格** | 现代中性模块化货架风 |
| **前台** `web-mall` | 货架式 + 卡片化 + 可换肤营销层 |
| **后台** `web-admin` | 企业级中后台极简高效风 |
| **核心原则** | 商品优先、效率优先、模块化、可扩展、大促可换肤 |

**"商品优先"** 的视觉落点：整页只有一个强色——交易红 `--color-price`，只用在价格上。其余全是中性灰阶，商品图和价格自然成为视线焦点。

**"中性"** 的落点：灰阶用**无色相**取值（RGB 三通道相等），而不是 Element Plus 默认那套偏蓝的 `#303133` / `#606266` / `#dcdfe6`。这是整套风格的地基，不要往灰阶里掺蓝色。

## 2. 三层 token 架构

`web-mall/src/assets/tokens.css` 与 `web-admin/src/assets/tokens.css` 结构相同，都是三层：

```
Layer 1  原始色板 / 尺度（primitives）   --n-100、--space-3、--radius-lg、--text-sm …
         ↓ 组件**永远不要**直接引用这一层
Layer 2  语义 token                      --color-text、--color-bg-surface、--color-accent …
         ↓ 组件只引用这一层；换肤只改这一层
Layer 3  Element Plus 桥接                --el-color-primary、--el-text-color-regular …
         ↓ EP 组件自动跟随主题，不用逐个改组件
```

**为什么分三层**：Layer 1 是"有哪些颜色"，Layer 2 是"这个颜色用来干什么"，Layer 3 是"让第三方组件也听话"。中间隔一层，才能做到换肤时只动 Layer 2、而所有引用语义 token 的组件（包括 Element Plus）自动跟着变。

语义 token 分组：

| 组 | 变量 | 说明 |
|---|---|---|
| 表面 | `--color-bg-page` / `-surface` / `-subtle` / `-inset` / `-hover` | 靠底色层次分区，而不是到处加边框 |
| 描边 | `--color-border` / `-strong` | |
| 文字 | `--color-text` / `-secondary` / `-tertiary` / `-placeholder` / `-inverse` | |
| 强调 | `--color-accent` / `-rgb` / `-hover` / `-active` / `-soft` / `-contrast` | 换肤的主战场 |
| 交易 | `--color-price` / `-soft` | 前台跟随皮肤；后台固定红色 |
| 状态 | `--color-success|warning|danger|info` 各配 `-soft` | 标签底色用 `-soft` |
| 营销 | `--promo-hero-bg` / `-hero-text` / `-badge-bg` / `-badge-text` | 仅前台，营销带专属 |

## 3. 换肤机制（仅前台）

换肤 = 切 `<html data-theme="...">`。**组件零改动、零重渲染**——只是 CSS 变量重新求值。

| 皮肤 id | 名称 | 谁在用 |
|---|---|---|
| `neutral` | 默认·中性 | 没有大促时的常态 |
| `promo-618` | 618 大促 | 运营在后台启用 |
| `promo-double11` | 双 11 | 同上 |
| `promo-spring` | 年货节 | 同上 |

### 3.1 皮肤由**运营**启用，不是用户偏好

启用权在**运营**：后台「营销中心 → 站点设置」选一套并保存，**全站立刻生效** ——
所有访客下次加载都用它。买家端**没有切换器**（皮肤全归运营后，用户没有可选项）。

- 值存在 `promotion.site_theme`（**单行**配置，`CHECK (id = 1)`），
  买家端启动时读 `GET /api/site-theme` 并覆盖本地缓存。
- 皮肤的中文名与白名单只有**后端一份**（`promotion/models.py` 的 `SITE_SKINS`）：
  后台的选择器直接用接口下发的 `options`，商城的 `theme/themes.ts` 只负责配色。
- ★ **皮肤只管外观。** 它曾经还带着营销文案（「跨店每满 300 减 50」这类**优惠承诺**），
  于是用户切一下皮肤就看到并不存在的活动。那些文案已从皮肤定义里删掉，
  顶部那条活动公告现在读的是**真实进行中的平台级活动**（`GET /api/promotions/active`）。

### 3.2 加一套新皮肤

三步，**不需要碰任何组件**：

1. `tokens.css` 加一个 `[data-theme="promo-xxx"]` 块，覆盖 Layer 2 里的营销相关 token（强调色、交易色、营销带、页面底色）。
2. `src/theme/themes.ts` 的 `THEMES` 加一条（只有 `id` 与 `themeColor`）。
3. 后端 `promotion/models.py` 的 `SITE_SKINS` 登记一行（id + 中文名）—— 后台的选择器与接口的校验正则都读它。

**只覆盖"气质"类 token**：灰阶、间距、圆角、字号**不要**在皮肤块里改——换肤换的是气质，不是结构。改了这些，货架排布会在不同皮肤下错位。

### 3.3 首屏不闪（FOUC）

`web-mall/index.html` 里有段内联脚本，在样式生效前把 `localStorage` 里的皮肤读回 `data-theme`。否则刷新时会先按默认主题渲染一帧再跳变，肉眼可见闪一下。

> ★ 这段内联脚本用的 key `'eshop.theme'` 必须与 `src/composables/useTheme.ts` 的 `STORAGE_KEY` 一致，改一处要同步改另一处。
>
> ★ 它的角色已经变了：现在是**缓存**（"上次从后端读到的皮肤"），不再是用户偏好 —— 没有任何用户写入的入口。
>
> ★ 已知代价：**运营刚改完皮肤时，还没刷新过的人会看到一次闪动**（缓存里是旧值，拉到新值后切过去）。缓存命中时不闪，所以只有"改皮肤后的第一次访问"会遇到。

## 4. 前台规范：货架式

「货架感」来自**严格的行列对齐**，不是装饰。三个条件缺一不可：

1. **图片区锁比例** — `aspect-ratio: 1 / 1`，列宽变化时所有图仍然等高。
2. **标题固定两行高度** — `height: calc(var(--text-base) * var(--leading-snug) * 2)` + `-webkit-line-clamp: 2`。
3. **价格压到底部** — `margin-top: auto`，让价格和它下面的元信息跨卡片对齐在同一基线。

效果：即使标题长度不同、有的显示单价有的显示价格区间（占的行数不同），整排卡片的价格行和底部信息仍然是一条直线。

其余约定：

- **货架是一个共享组件** `components/ProductGrid.vue`：搜索页与店铺页共用同一份网格与卡片。
  卡片那几十行里全是"货架感"的讲究，复制第二份就等着腐烂。
  页大小也由它算（列数 × 行数，列数只有它自己量得到），见 `composables/useShelf.ts`。
- 商品卡用**自定义 `<a class="card">`**，不用 `el-card`——需要控制 `overflow:hidden` + 图片缩放 + `margin-top:auto`，`el-card` 的 body 结构会挡路。
- ★ **卡片里不能放链接**。卡片整体已经是 `<a>`，再嵌一个（比如"店名 → 店铺页"）是无效 HTML，
  浏览器会把它拆开、行为不可预测。店铺入口因此只在**商品详情页**（那块店铺信息现在可点）。
- 价格加 `.tnum`（`font-variant-numeric: tabular-nums`），数字等宽，扫一排价格时位数不会左右跳。
- 图片挂载 `@error="onImageError"`，失败回退到内联 SVG 占位。
- hover 只做**轻抬升 + 边框加深 + 图放大 1.03**，不用重投影。

## 5. 后台规范：极简高效

后台**不上换肤**（效率优先）。与前台共用同一套灰阶，但**尺度更紧、圆角更小**：

| 维度 | 前台 | 后台 |
|---|---|---|
| 基准字号 | 14px | **13px** |
| 圆角 `--radius-lg`（卡片也用这个） | 10px | **6px** |
| 页面底色 | `--n-50` | **`--n-100`**（更深，靠底色分出面板） |
| 头部高度 | 60px | 56px |

其余：

- **分层靠底色，不靠阴影**。页面底 `--n-100`、面板白，一块一块分得清，视觉噪声低。
- **状态标签用柔和底**（`--color-*-soft` + 同色文字），不用实心色块——一屏几十个标签时实心块会很吵。
- **动作永远在右边**，工具栏左侧放筛选（分段控件）、右侧放主操作。
- 金额列加 `.tnum` 并右对齐。

## 6. 开发约定（硬性）

### 6.1 颜色/间距/圆角一律走语义 token

```vue
<!-- ✅ -->
<style scoped>
.card { padding: var(--space-4); border: 1px solid var(--color-border); border-radius: var(--card-radius); }
.price { color: var(--color-price); }
</style>

<!-- ❌ 硬编码：换肤时这里不会变 -->
<style scoped>
.card { padding: 16px; border: 1px solid #e4e4e7; border-radius: 10px; }
.price { color: #d93025; }
</style>
```

**只有两处允许写死色值**，因为它们的消费方拿不到 CSS 变量：

| 位置 | 为什么写死 | 同步要求 |
|---|---|---|
| `utils/placeholder.ts` | 色值要拼进 data-URI 内联 SVG，那是个**独立文档**，拿不到页面的 CSS 变量 | 对齐 `--n-100`（底）/ `--n-400`（字）。**不跟随皮肤**——占位图本就该是中性灰 |
| `theme/themes.ts` | `swatch`（切换器色点）和 `themeColor`（浏览器地址栏）由 **JS 消费**，不是 CSS | 必须与该主题在 `tokens.css` 里的 `--color-accent` / `--color-bg-page` **手工保持一致** |

> ★ 这两处是唯一会随配色改动而"腐烂"的地方。改 `tokens.css` 里的强调色或页面底色时，**顺手检查 `themes.ts`**，否则切换器上的色点会和实际皮肤对不上。

### 6.2 导入顺序不能改

```ts
// main.ts
import 'element-plus/dist/index.css'   // 1. EP 先
import './assets/tokens.css'           // 2. 我们的 token 后
import './assets/base.css'             // 3. 最后
```

两边都在 `:root` 上定义 `--el-*` 变量，优先级相同，靠"后者胜"覆盖。**顺序反了主题完全不生效**。

### 6.3 两个应用不共享 token 文件

前后台的密度、圆角、页面底色本来就不同，抽公共包会让两边互相迁就。改配色时**记得两个文件都看一眼**，保持灰阶一致即可。

### 6.4 新增页面检查清单

- [ ] 没有硬编码色值——跑一次 §9 的扫描，`.vue` 文件不应出现在结果里（§6.1 的两个白名单除外）
- [ ] 没有硬编码像素间距/圆角（用 `--space-*` / `--radius-*`）
- [ ] 金额用 `.tnum`
- [ ] 图片有 `@error="onImageError"`
- [ ] 前台页面确认在四套皮肤下都正常
- [ ] 检查窄屏（375px / 768px）不错版、无横向滚动

## 7. 已踩过的坑

| 坑 | 现象 | 原因与做法 |
|---|---|---|
| `--el-color-primary-rgb` | 按钮外发光、tag 底色是错的（还是 EP 默认蓝） | EP 用 `rgba(var(--el-color-primary-rgb), .1)` 计算，这个变量要的是**裸三元组** `225, 37, 27`，不是颜色值。`color-mix` 推不出来，**每个主题必须显式写一行 `--color-accent-rgb`** |
| `--el-table-*` 打在 `:root` 无效 | 表头底色还是白的 | EP 把表格变量定义在 `.el-table` **元素**上（组件作用域），`:root` 那层会被盖掉。必须写在 `base.css` 的 `.el-table` 块里 |
| `color-mix()` | — | 需要 Chrome 111+。本项目是本地演示、现代浏览器，可用；若要支持旧浏览器需改成预计算色阶 |
| 自定义属性读出来是字面量 | `getPropertyValue('--el-color-primary-light-3')` 返回 `color-mix(...)` 字符串 | 自定义属性是**惰性求值**的，正常现象。要验证是否生效，挂个元素设 `background: var(--...)` 再读 computed style |
| 换肤瞬间读色值读到旧值 | 切主题后立刻读 computed style，得到的是旧颜色 | `base.css` 里给切换过程挂了 280ms 过渡（`html.theme-switching`），读到的是插值中间值。等过渡结束再读 |

## 8. 相关文件

```
web-mall/
├── index.html                      # data-theme 默认值 + 防 FOUC 内联脚本
└── src/
    ├── assets/tokens.css           # 三层 token + 4 个皮肤块  ← 改配色看这里
    ├── assets/base.css             # reset、排版、EP 组件质感
    ├── theme/themes.ts             # 皮肤注册表（元数据 + 营销带文案）
    ├── composables/useTheme.ts     # 主题状态 / 持久化 / 写 data-theme
    ├── composables/useShelf.ts     # 货架页大小（列数 × 行数）与首屏触发
    ├── components/ProductGrid.vue  # 货架：网格 + 卡片 + 加载更多（搜索页 / 店铺页共用）
    ├── components/PromoStrip.vue   # 可换肤营销带
    └── views/                      # 页面（只引用语义 token）

web-admin/
└── src/
    ├── assets/tokens.css           # 同结构，尺度更紧、单主题
    └── assets/base.css             # 含 .el-table 作用域桥接
```

## 9. 验证

改完样式后跑：

```bash
npm.cmd run type-check; npm.cmd run lint
```

两个应用都要跑。然后在浏览器里实际看——**类型检查和 lint 验证不了观感**：

- 前台：在后台逐个切换四种皮肤 → **商城任意页面**都跟着变；刷新后仍是运营选的那套（缓存生效）；没有进行中的平台活动时，顶部公告带**整条不出现**
- 前台：货架排布是否整排对齐（不同标题长度、单价 vs 价格区间混排）
- 后台：表格密度、状态标签、规格编辑器
- 375px / 768px / 1440px 三档宽度

确认没有残留硬编码色值——**搜所有十六进制色值**，然后对照白名单，比搜某几个已知的旧色值可靠（旧色值搜不出新写错的颜色）：

```powershell
Get-ChildItem -Recurse -Include *.vue,*.css,*.ts web-mall/src,web-admin/src |
  Select-String -Pattern '#[0-9a-fA-F]{3,8}\b' |
  Select-Object -ExpandProperty Path -Unique
```

Git Bash / WSL 下等价写法：

```bash
grep -rlE '#[0-9a-fA-F]{3,8}\b' --include='*.vue' --include='*.css' --include='*.ts' web-mall/src web-admin/src
```

**预期只有这 5 个文件命中**（见 §6.1）：

```
web-mall/src/assets/tokens.css      ← 色值定义本身
web-mall/src/theme/themes.ts        ← 切换器色点 / 地址栏颜色
web-mall/src/utils/placeholder.ts   ← data-URI 内联 SVG
web-admin/src/assets/tokens.css
web-admin/src/utils/placeholder.ts
```

多出任何 `.vue` 文件就是有硬编码色值漏进页面了，回去改成语义 token。

## 10. 不在范围内

- **后台侧边栏改造**：后台保留顶部导航 + 1200px 居中，是已确认的取舍。
- **暗色模式**：需要单独调商品图与价格的可读性，未做。
- **共享 token 包**：见 §6.3。
