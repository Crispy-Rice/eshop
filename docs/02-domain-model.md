# 02 商品域模型：SPU + SKU

## 1. 概念定义

| 概念 | 全称 | 含义 | 举例 |
|---|---|---|---|
| **SPU** | Standard Product Unit | 标准产品单元，**抽象产品**，商品的通用描述。用户搜索、浏览、分享、评价挂载在这一层 | "iPhone 16 Pro" |
| **SKU** | Stock Keeping Unit | 库存量单位，**最小售卖单元**。有独立价格、库存、封面图、物流重量，是交易和库存的最小对象 | "iPhone 16 Pro 256G 黑色" |
| **类目** | Category | 三级类目树，决定属性模板 | 数码 > 手机 > 手机 |
| **属性** | Attribute | 分为**规格属性**（决定 SKU）和**参数属性**（仅描述） | 规格：颜色/容量；参数：屏幕尺寸 |
| **规格组合** | Spec Combination | 规格属性取值的笛卡尔积，每个组合对应一个 SKU | (黑色,256G) |

**核心原则**：
- **购物车、订单、库存、价格、运费，全部挂在 SKU 上**。SPU 只负责展示聚合。
- SPU 层可以有一个"展示价区间"（由所有在售 SKU 的 min/max 计算），但**这个价格永远不能用于下单**。

## 2. 数据模型

### 2.1 表结构

```
category（类目）
  └── spu（商品）
        ├── spu_attr（SPU 参数属性值，渲染详情页参数表）
        ├── spec_group（规格组：颜色、容量）
        │     └── spec_value（规格值：黑/白/256G/512G）
        ├── sku（最小售卖单元）
        │     ├── sku_spec（SKU ↔ 规格值 的关联，如 SKU#1001 → (黑色,256G)）
        │     └── sku_stock（SKU 在各仓库的库存）
        ├── spu_image（图片）
        └── spu_detail（富文本详情）
```

### 2.2 SPU 表

```sql
CREATE TABLE `spu` (
  `id`            BIGINT       NOT NULL COMMENT 'SPU ID',
  `shop_id`       BIGINT       NOT NULL COMMENT '店铺 ID',
  `category_id`   BIGINT       NOT NULL COMMENT '末级类目 ID',
  `brand_id`      BIGINT       DEFAULT NULL,
  `title`         VARCHAR(120) NOT NULL COMMENT '商品标题',
  `sub_title`     VARCHAR(255) DEFAULT NULL COMMENT '副标题/卖点',
  `main_image`    VARCHAR(255) NOT NULL COMMENT '主图（列表页用）',
  `price_min`     BIGINT       NOT NULL DEFAULT 0 COMMENT '展示最低价（分，冗余，由SKU算）',
  `price_max`     BIGINT       NOT NULL DEFAULT 0 COMMENT '展示最高价（分）',
  `total_sold`    INT          NOT NULL DEFAULT 0 COMMENT '累计销量（冗余）',
  `review_count`  INT          NOT NULL DEFAULT 0 COMMENT '评价数（冗余）',
  `avg_score`     DECIMAL(3,2) NOT NULL DEFAULT 5.00 COMMENT '平均评分',
  `status`        TINYINT      NOT NULL DEFAULT 1 COMMENT '1草稿 2上架 3下架 4违规下架',
  `sort_weight`   INT          NOT NULL DEFAULT 0 COMMENT '排序权重',
  `created_at`    DATETIME(3)  NOT NULL,
  `updated_at`    DATETIME(3)  NOT NULL,
  `deleted`       TINYINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_shop_status` (`shop_id`, `status`),
  KEY `idx_category_status` (`category_id`, `status`)
) ENGINE=InnoDB COMMENT='标准产品单元（抽象商品）';
```

### 2.3 SKU 表——交易核心

```sql
CREATE TABLE `sku` (
  `id`             BIGINT       NOT NULL COMMENT 'SKU ID',
  `spu_id`         BIGINT       NOT NULL,
  `shop_id`        BIGINT       NOT NULL COMMENT '冗余，下单/拆单时免join',
  `sku_code`       VARCHAR(64)  NOT NULL COMMENT '商家编码',
  `spec_text`      VARCHAR(255) NOT NULL COMMENT '规格摘要，如"黑色;256G"，用于订单快照',
  -- ★ 最小售卖单元的四要素
  `price`          BIGINT       NOT NULL COMMENT '★ 单价（分）',
  `cover_image`    VARCHAR(255) NOT NULL COMMENT '★ 封面图（切换规格时展示）',
  `weight_g`       INT          NOT NULL COMMENT '★ 物流重量（克）',
  -- 库存不在这张表（分离到 sku_stock + Redis）
  `status`         TINYINT      NOT NULL DEFAULT 1 COMMENT '1上架 2下架',
  `version`        INT          NOT NULL DEFAULT 0,
  `created_at`     DATETIME(3)  NOT NULL,
  `updated_at`     DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_spu_code` (`spu_id`, `sku_code`),
  KEY `idx_spu` (`spu_id`, `status`)
) ENGINE=InnoDB COMMENT='最小售卖单元';
```

> **价格放在 SKU 不在 SPU**：同一个 SPU 下"256G"和"512G"价格不同是常态。
> **重量放在 SKU 不在 SPU**：颜色不同不影响重量，但容量/规格不同会。运费计算读这里（见 [06-freight](06-freight.md)）。

**为什么库存不进 `sku` 表？**

| 维度 | 放 sku 表 | 分离到 sku_stock |
|---|---|---|
| 多仓 | 需要加 `warehouse_id` 变成多行，破坏主键语义 | 天然 (sku_id, warehouse_id) 复合主键 |
| 高频更新 | 商品信息（标题/图）和库存更新争抢同一行锁 | 库存行独立，商品信息可缓存 |
| 分库 | 商品库和库存库可以物理分离 | 各自独立扩容 |

### 2.4 库存表（对应的 DB 侧）

```sql
CREATE TABLE `sku_stock` (
  `sku_id`        BIGINT      NOT NULL,
  `warehouse_id`  BIGINT      NOT NULL COMMENT '仓库 ID',
  `total`         INT         NOT NULL DEFAULT 0 COMMENT '总库存 = 可用 + 预占 + 锁定',
  `available`     INT         NOT NULL DEFAULT 0 COMMENT '可售',
  `locked`        INT         NOT NULL DEFAULT 0 COMMENT '已下单未支付（预占）',
  `frozen`        INT         NOT NULL DEFAULT 0 COMMENT '已支付待发货（实扣）',
  `version`       INT         NOT NULL DEFAULT 0,
  `updated_at`    DATETIME(3) NOT NULL,
  PRIMARY KEY (`sku_id`, `warehouse_id`),
  KEY `idx_warehouse` (`warehouse_id`)
) ENGINE=InnoDB COMMENT='SKU 分仓库存';
```

**恒等式（用于对账校验）**：`total = available + locked + frozen`。任何时刻这个等式不成立，就是 Bug，对账任务会告警。

### 2.5 规格建模

```sql
CREATE TABLE `spec_group` (
  `id`      BIGINT       NOT NULL,
  `spu_id`  BIGINT       NOT NULL,
  `name`    VARCHAR(32)  NOT NULL COMMENT '如"颜色"',
  `sort`    INT          NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_spu` (`spu_id`)
);

CREATE TABLE `spec_value` (
  `id`       BIGINT      NOT NULL,
  `group_id` BIGINT      NOT NULL,
  `value`    VARCHAR(32) NOT NULL COMMENT '如"暗夜黑"',
  `image`    VARCHAR(255) DEFAULT NULL COMMENT '色块图，前端渲染规格选择器',
  `sort`     INT         NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_group` (`group_id`)
);

-- SKU 与规格值的多对多关联
CREATE TABLE `sku_spec` (
  `sku_id`        BIGINT NOT NULL,
  `spec_group_id` BIGINT NOT NULL,
  `spec_value_id` BIGINT NOT NULL,
  PRIMARY KEY (`sku_id`, `spec_group_id`),
  KEY `idx_value` (`spec_value_id`)
);
```

`PRIMARY KEY (sku_id, spec_group_id)` 保证**一个 SKU 在每个规格组下只能有一个取值**——这个约束能在数据库层面挡住"一个 SKU 同时是黑色和白色"这类脏数据。

## 3. 规格选择器的典型问题：无效组合

**问题**：SPU "iPhone" 有颜色 {黑, 白} 和容量 {256G, 512G}，商家只上架了 3 个 SKU（黑色256G、黑色512G、白色256G）——"白色 512G"不存在。前端不能让用户选到这个组合。

**解法**：SPU 详情接口返回**每个规格值对应的可用 SKU ID 集合**，前端做可点击性推导。

```json
{
  "spuId": 9001,
  "title": "iPhone 16 Pro",
  "specGroups": [
    { "id": 1, "name": "颜色", "values": [
        {"id": 11, "value": "暗夜黑", "image": "//cdn/black.png"},
        {"id": 12, "value": "原色钛", "image": "//cdn/natural.png"}
    ]},
    { "id": 2, "name": "容量", "values": [
        {"id": 21, "value": "256G"},
        {"id": 22, "value": "512G"}
    ]}
  ],
  "skus": [
    { "skuId": 1001, "specValueIds": [11, 21], "price": 799900,
      "coverImage": "//cdn/1001.png", "weightG": 199,
      "stockStatus": "IN_STOCK", "stockHint": "现货", "freightTemplateId": 55 },
    { "skuId": 1002, "specValueIds": [11, 22], "price": 899900,
      "coverImage": "//cdn/1002.png", "weightG": 199,
      "stockStatus": "LOW_STOCK", "stockHint": "仅剩 3 件", "freightTemplateId": 55 },
    { "skuId": 1003, "specValueIds": [12, 21], "price": 799900,
      "coverImage": "//cdn/1003.png", "weightG": 199,
      "stockStatus": "OUT_OF_STOCK", "stockHint": "缺货", "freightTemplateId": 55 }
  ]
}
```

前端算法（O(已选值数 × 未选组值数)）：

```
对于规格组 g 的某个取值 v：
  若 存在 SKU 同时包含 v 和所有已选中的值 → v 可选
  否则 v 置灰
若"已选中的值"本身导致某组合消失 → 取消该选中项或提示
```

**关键点**：`stockStatus` 只用于 UI 提示和拦截，**不参与下单判定**。用户可能花 5 分钟挑规格，最终以下单时的库存为准（见 [11](11-price-consistency.md) 的库存一致性）。

## 4. 购物车走 SKU

```sql
CREATE TABLE `cart_item` (
  `id`           BIGINT      NOT NULL,
  `user_id`      BIGINT      NOT NULL,
  `shop_id`      BIGINT      NOT NULL,
  `sku_id`       BIGINT      NOT NULL,
  `spu_id`       BIGINT      NOT NULL,
  -- 加购时的价格快照，仅用于展示"降价提醒"，不参与结算
  `price_snapshot` BIGINT    NOT NULL COMMENT '加购时价格（分）',
  `num`          INT         NOT NULL,
  `selected`     TINYINT     NOT NULL DEFAULT 1 COMMENT '是否勾选',
  `source`       TINYINT     NOT NULL DEFAULT 1 COMMENT '1详情页 2列表页 3活动页',
  `created_at`   DATETIME(3) NOT NULL,
  `updated_at`   DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_sku` (`user_id`, `sku_id`) COMMENT '同一SKU只能有一条，加购是累加num',
  KEY `idx_user_shop` (`user_id`, `shop_id`)
) ENGINE=InnoDB COMMENT='购物车项，粒度=SKU';
```

**设计要点**：

1. **唯一索引 `(user_id, sku_id)`**：同一 SKU 重复加购走 `num = num + N`，避免购物车出现两行一样的商品。用 `INSERT ... ON DUPLICATE KEY UPDATE num = num + VALUES(num)` 实现原子加购。
2. **购物车粒度是 SKU，不是 SPU**：用户在详情页切换规格后加购，加的是当前选中的 SKU。
3. **购物车数量上限**：单 SKU 最多 200 件，整车最多 100 个 SKU（防刷）。写入前先校验。
4. **不存库存可售量**：购物车只存用户意图，库存校验在结算页做。购物车列表返回 `status`（有效/失效/无货/下架）由实时查询填充。
5. **价格快照的作用**：加购价 vs 当前价对比，展示"较加购时降 ¥50"。结算时**一定重新查当前价**。

### 购物车列表接口的坑：N+1 查询

购物车 50 个 SKU，不能循环查商品和库存。做法：

```
1. 批量查 cart_item（1次）
2. 收集 skuIds → 批量查 sku + spu（1次，或走本地缓存）
3. 收集 skuIds → 批量查 Redis 库存（1次 mget / pipeline）
4. 按 shop_id 分组返回（前端按店铺分块展示）
```

商品信息用**多级缓存**：Caffeine（本地 5s）→ Redis（60s）→ DB。商品改价时通过 MQ 广播失效。

## 5. 下单时的商品快照

订单一旦创建，**必须冻结当时的商品信息**。原因：商家三天后改了标题/图/价格，历史订单不能跟着变（否则退款金额算不清，用户看到的和买到的不一样）。

```sql
CREATE TABLE `order_item` (
  `id`                BIGINT       NOT NULL,
  `order_sub_no`      VARCHAR(32)  NOT NULL COMMENT '子单号',
  `order_main_no`     VARCHAR(32)  NOT NULL COMMENT '母单号',
  `spu_id`            BIGINT       NOT NULL,
  `sku_id`            BIGINT       NOT NULL,
  -- ★★★ 快照字段：写入后永不变更 ★★★
  `spu_title_snap`    VARCHAR(120) NOT NULL COMMENT '下单时商品标题',
  `sku_spec_snap`     VARCHAR(255) NOT NULL COMMENT '下单时规格，如"暗夜黑;256G"',
  `cover_image_snap`  VARCHAR(255) NOT NULL COMMENT '下单时封面图',
  `unit_price_snap`   BIGINT       NOT NULL COMMENT '下单时单价（分）',
  `weight_g_snap`     INT          NOT NULL COMMENT '下单时重量（克），退款运费计算用',
  `num`               INT          NOT NULL,
  `item_amount`       BIGINT       NOT NULL COMMENT '= unit_price_snap * num，行小计',
  -- 优惠分摊（见 05-promotion-engine）
  `discount_amount`   BIGINT       NOT NULL DEFAULT 0 COMMENT '本行承担的总优惠（分）',
  `coupon_amount`     BIGINT       NOT NULL DEFAULT 0 COMMENT '其中券优惠',
  `promo_amount`      BIGINT       NOT NULL DEFAULT 0 COMMENT '其中活动优惠',
  `point_amount`      BIGINT       NOT NULL DEFAULT 0 COMMENT '其中积分抵扣',
  `pay_amount`        BIGINT       NOT NULL COMMENT '= item_amount - discount_amount',
  -- 退款进度（用于部分退计算）
  `refunded_num`      INT          NOT NULL DEFAULT 0 COMMENT '已退数量',
  `refunded_amount`   BIGINT       NOT NULL DEFAULT 0 COMMENT '已退金额（含优惠还原）',
  `item_status`       TINYINT      NOT NULL DEFAULT 1 COMMENT '1正常 2退款中 3已退款 4换货中',
  `warehouse_id`      BIGINT       NOT NULL COMMENT '发货仓，下单时定，运费计算用',
  PRIMARY KEY (`id`),
  KEY `idx_sub` (`order_sub_no`),
  KEY `idx_main` (`order_main_no`),
  KEY `idx_sku` (`sku_id`)
) ENGINE=InnoDB COMMENT='订单商品项（快照）';
```

**快照字段命名统一加 `_snap` 后缀**，代码规范里强制：所有读 `order_item` 的地方禁止 join `spu`/`sku` 取实时数据。这条规则的价值：订单详情页、退款计算、对账报表全部依赖快照，一旦混用实时数据，历史数据会漂移。

## 6. 商品上下架对已有订单的影响

| 操作 | 对已有订单 | 实现 |
|---|---|---|
| 下架 SKU | **无影响**，已下单的照常发货 | 只改 `sku.status`，订单读快照 |
| 删除 SPU | **软删**，`deleted=1` | 物理删除会破坏订单外键语义 |
| 改价 | 对已下单无影响 | 订单用 `unit_price_snap` |
| 改规格 | 已下单订单保存 `sku_spec_snap` | 历史订单展示旧规格文案 |
| 拆库存仓 | 新订单按新规则 | 老订单保留 `warehouse_id` |

## 7. 商品搜索（ES 映射要点）

ES 里以 **SPU 为文档**，SKU 作为 `nested` 字段：

```json
{
  "spuId": 9001,
  "title": "iPhone 16 Pro",
  "categoryPath": ["数码","手机","手机"],
  "priceMin": 799900,
  "priceMax": 899900,
  "totalSold": 12034,
  "attrs": {"品牌":"Apple","屏幕尺寸":"6.3英寸"},
  "skus": [
    {"skuId":1001,"specs":{"颜色":"暗夜黑","容量":"256G"},"price":799900},
    {"skuId":1002,"specs":{"颜色":"暗夜黑","容量":"512G"},"price":899900}
  ]
}
```

- 用户搜索"256G 黑色 iPhone"时，用 `nested` 查询匹配 SKU 规格，但**返回 SPU 卡片**。
- 筛选价格区间用 `priceMin/priceMax` 与区间做交叉判断。
- `totalSold` 排序字段用 `_function_score` 或直接累加，避免每次搜索都实时聚合。

## 8. 类目与属性

- 类目树三层（一级/二级/三级），第三级绑定**属性模板**：发布商品时，前端根据 `category_id` 拉取该类的属性定义，动态渲染表单。
- 属性分两类：
  - `is_spec = 1`：规格属性 → 生成 SKU 的维度（颜色/容量/尺码）
  - `is_spec = 0`：参数属性 → 只展示（材质/产地/保修期）
- **规格属性数量上限**：单 SPU 最多 3 个规格组，每组最多 20 个取值，SKU 总数上限 200（超出前端提示"规格组合过多"）。这个限制是为了防止笛卡尔积爆炸导致 SKU 表膨胀和规格选择器卡死。

## 9. 商品发布流程

```
1. 选择类目 → 拉取属性模板
2. 填写 SPU 通用信息（标题、主图、参数属性）
3. 添加规格组和规格值 → 系统生成 SKU 编辑表格（笛卡尔积）
4. 逐行填写 SKU：价格、封面图、重量、商编、初始库存、发货仓
5. 校验：
   - 每个 SKU 价格 > 0，重量 > 0
   - 至少 1 个 SKU
   - 主图/封面图必填
   - 标题违禁词过滤
6. 保存为草稿 → 提交审核 → 平台通过 → 上架（status=2）
7. 上架时：写 sku_stock 初始化库存 + 同步库存到 Redis + 写 ES 索引
```

**第 7 步的三写必须幂等**：重复上架不能造成库存翻倍。做法是以 `(sku_id, warehouse_id)` 为主键做 `INSERT ... ON DUPLICATE KEY UPDATE`，Redis 库存用 `SET`（覆盖，不是 INCR）。
