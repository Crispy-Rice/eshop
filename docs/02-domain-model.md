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
CREATE TABLE product.spu (
  id            BIGINT        PRIMARY KEY,                 -- 雪花 ID
  shop_id       BIGINT        NOT NULL,                    -- 店铺 ID
  category_id   BIGINT        NOT NULL,                    -- 末级类目 ID
  brand_id      BIGINT,
  title         VARCHAR(120)  NOT NULL,
  sub_title     VARCHAR(255),                              -- 副标题/卖点
  main_image    VARCHAR(255)  NOT NULL,                    -- 主图（列表页用）
  price_min     BIGINT        NOT NULL DEFAULT 0,          -- 展示最低价（分，冗余，由 SKU 算）
  price_max     BIGINT        NOT NULL DEFAULT 0,          -- 展示最高价（分）
  total_sold    INT           NOT NULL DEFAULT 0,          -- 累计销量（冗余）
  review_count  INT           NOT NULL DEFAULT 0,
  avg_score     NUMERIC(3,2)  NOT NULL DEFAULT 5.00,
  status        SMALLINT      NOT NULL DEFAULT 1,          -- 1草稿 2上架 3下架 4违规下架
  sort_weight   INT           NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  deleted       BOOLEAN       NOT NULL DEFAULT false
);
CREATE INDEX idx_spu_shop_status     ON product.spu (shop_id, status);
CREATE INDEX idx_spu_category_status ON product.spu (category_id, status);
-- 标题模糊搜索（见 §7）
CREATE INDEX idx_spu_title_trgm ON product.spu USING gin (title gin_trgm_ops);
COMMENT ON TABLE product.spu IS '标准产品单元（抽象商品）';
```

> PostgreSQL 没有 `TINYINT`，状态类字段统一用 `SMALLINT`；`COMMENT` 不能写在列定义里，示例中用行尾注释说明，实际迁移脚本里用 `COMMENT ON COLUMN`。DDL 约定详见 [13-schema](13-schema.md) §0。

### 2.3 SKU 表——交易核心

```sql
CREATE TABLE product.sku (
  id            BIGINT        PRIMARY KEY,
  spu_id        BIGINT        NOT NULL,
  shop_id       BIGINT        NOT NULL,                    -- 冗余，下单/拆单时免 join
  sku_code      VARCHAR(64)   NOT NULL,                    -- 商家编码
  spec_text     VARCHAR(255)  NOT NULL,                    -- 规格摘要，如"黑色;256G"，用于订单快照
  -- ★ 最小售卖单元的四要素
  price         BIGINT        NOT NULL CHECK (price > 0),  -- ★ 单价（分）
  cover_image   VARCHAR(255)  NOT NULL,                    -- ★ 封面图（切换规格时展示）
  weight_g      INT           NOT NULL CHECK (weight_g > 0), -- ★ 物流重量（克）
  -- 库存不在这张表（分离到 inventory.sku_stock + Redis）
  status        SMALLINT      NOT NULL DEFAULT 1,          -- 1上架 2下架
  version       INT           NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT uk_sku_spu_code UNIQUE (spu_id, sku_code)
);
CREATE INDEX idx_sku_spu ON product.sku (spu_id, status);
COMMENT ON TABLE product.sku IS '最小售卖单元';
```

> **价格放在 SKU 不在 SPU**：同一个 SPU 下"256G"和"512G"价格不同是常态。
> **重量放在 SKU 不在 SPU**：颜色不同不影响重量，但容量/规格不同会。运费计算读这里（见 [06-freight](06-freight.md)）。

**为什么库存不进 `sku` 表？**

| 维度 | 放 sku 表 | 分离到 sku_stock |
|---|---|---|
| 多仓 | 需要加 `warehouse_id` 变成多行，破坏主键语义 | 天然 (sku_id, warehouse_id) 复合主键 |
| 高频更新 | 商品信息（标题/图）和库存更新争抢同一行锁 | 库存行独立，商品信息可缓存 |
| 模块边界 | 商品模块和库存模块共用一张表，边界模糊 | 各自独立 schema，日后拆服务无需迁表 |

### 2.4 库存表（对应的 DB 侧）

```sql
CREATE TABLE inventory.sku_stock (
  sku_id        BIGINT      NOT NULL,
  warehouse_id  BIGINT      NOT NULL,
  total         INT         NOT NULL DEFAULT 0,   -- 总库存 = 可售 + 预占 + 实扣
  available     INT         NOT NULL DEFAULT 0,   -- 可售
  locked        INT         NOT NULL DEFAULT 0,   -- 已下单未支付（预占）
  frozen        INT         NOT NULL DEFAULT 0,   -- 已支付待发货（实扣）
  version       INT         NOT NULL DEFAULT 0,
  updated_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  PRIMARY KEY (sku_id, warehouse_id),
  -- ★ 数据库层面的硬约束：任何 Bug 导致负库存或恒等式被破坏，事务直接失败
  CONSTRAINT ck_stock_non_negative CHECK (available >= 0 AND locked >= 0 AND frozen >= 0),
  CONSTRAINT ck_stock_identity     CHECK (total = available + locked + frozen)
);
CREATE INDEX idx_sku_stock_warehouse ON inventory.sku_stock (warehouse_id);
COMMENT ON TABLE inventory.sku_stock IS 'SKU 分仓库存';
```

**恒等式**：`total = available + locked + frozen`。PostgreSQL 的 `CHECK` 约束会在每次写入时校验它，等式被破坏的写入会直接报错回滚；对账任务仍保留这项检查作为监控（见 [13](13-schema.md) §10）。

### 2.5 规格建模

```sql
CREATE TABLE product.spec_group (
  id      BIGINT       PRIMARY KEY,
  spu_id  BIGINT       NOT NULL,
  name    VARCHAR(32)  NOT NULL,           -- 如"颜色"
  sort    INT          NOT NULL DEFAULT 0
);
CREATE INDEX idx_spec_group_spu ON product.spec_group (spu_id);

CREATE TABLE product.spec_value (
  id       BIGINT       PRIMARY KEY,
  group_id BIGINT       NOT NULL REFERENCES product.spec_group (id),
  value    VARCHAR(32)  NOT NULL,          -- 如"暗夜黑"
  image    VARCHAR(255),                   -- 色块图，前端渲染规格选择器
  sort     INT          NOT NULL DEFAULT 0
);
CREATE INDEX idx_spec_value_group ON product.spec_value (group_id);

-- SKU 与规格值的多对多关联
CREATE TABLE product.sku_spec (
  sku_id        BIGINT NOT NULL REFERENCES product.sku (id),
  spec_group_id BIGINT NOT NULL REFERENCES product.spec_group (id),
  spec_value_id BIGINT NOT NULL REFERENCES product.spec_value (id),
  PRIMARY KEY (sku_id, spec_group_id)
);
CREATE INDEX idx_sku_spec_value ON product.sku_spec (spec_value_id);
```

> 同一模块（schema）内部可以用外键保证引用完整性；**跨模块不建外键**（如 `order_item.sku_id` 不引用 `product.sku`），保持模块可拆分。

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
CREATE TABLE cart.cart_item (
  id             BIGINT      PRIMARY KEY,
  user_id        BIGINT      NOT NULL,
  shop_id        BIGINT      NOT NULL,
  sku_id         BIGINT      NOT NULL,
  spu_id         BIGINT      NOT NULL,
  -- 加购时的价格快照，仅用于展示"降价提醒"，不参与结算
  price_snapshot BIGINT      NOT NULL,                 -- 加购时价格（分）
  num            INT         NOT NULL CHECK (num BETWEEN 1 AND 200),
  selected       BOOLEAN     NOT NULL DEFAULT true,
  source         SMALLINT    NOT NULL DEFAULT 1,       -- 1详情页 2列表页 3活动页
  created_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  -- 同一 SKU 只能有一条，加购是累加 num
  CONSTRAINT uk_cart_user_sku UNIQUE (user_id, sku_id)
);
CREATE INDEX idx_cart_user_shop ON cart.cart_item (user_id, shop_id);
COMMENT ON TABLE cart.cart_item IS '购物车项，粒度=SKU';
```

**设计要点**：

1. **唯一约束 `(user_id, sku_id)`**：同一 SKU 重复加购走 `num = num + N`，避免购物车出现两行一样的商品。用 PG 的 upsert 实现原子加购：

   ```sql
   INSERT INTO cart.cart_item (id, user_id, shop_id, sku_id, spu_id, price_snapshot, num)
   VALUES (:id, :user_id, :shop_id, :sku_id, :spu_id, :price, :num)
   ON CONFLICT (user_id, sku_id)
   DO UPDATE SET num = LEAST(cart.cart_item.num + EXCLUDED.num, 200),
                 updated_at = now();
   ```

   SQLAlchemy 中对应 `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update(...)`。
2. **购物车粒度是 SKU，不是 SPU**：用户在详情页切换规格后加购，加的是当前选中的 SKU。
3. **购物车数量上限**：单 SKU 最多 200 件，整车最多 100 个 SKU（防刷）。写入前先校验。
4. **不存库存可售量**：购物车只存用户意图，库存校验在结算页做。购物车列表返回 `status`（有效/失效/无货/下架）由实时查询填充。
5. **价格快照的作用**：加购价 vs 当前价对比，展示"较加购时降 ¥50"。结算时**一定重新查当前价**。

### 购物车列表接口的坑：N+1 查询

购物车 50 个 SKU，不能循环查商品和库存。做法：

```
1. 批量查 cart_item（1次）
2. 收集 skuIds → 批量查 sku + spu（1次，WHERE id = ANY(:ids)，或走缓存）
3. 收集 skuIds → 批量查 Redis 库存（1次 MGET / pipeline）
4. 按 shop_id 分组返回（前端按店铺分块展示）
```

商品信息用**两级缓存**：进程内 TTL 缓存（`cachetools.TTLCache`，5s）→ Redis（60s）→ DB。商品改价时发布 `product.changed` 事件到 Redis Streams，各 api 进程订阅后清理本地缓存并删除 Redis 缓存键。

## 5. 下单时的商品快照

订单一旦创建，**必须冻结当时的商品信息**。原因：商家三天后改了标题/图/价格，历史订单不能跟着变（否则退款金额算不清，用户看到的和买到的不一样）。

```sql
CREATE TABLE trade.order_item (
  id                BIGINT       PRIMARY KEY,
  order_sub_no      VARCHAR(32)  NOT NULL,          -- 子单号
  order_main_no     VARCHAR(32)  NOT NULL,          -- 母单号
  spu_id            BIGINT       NOT NULL,
  sku_id            BIGINT       NOT NULL,
  -- ★★★ 快照字段：写入后永不变更 ★★★
  spu_title_snap    VARCHAR(120) NOT NULL,          -- 下单时商品标题
  sku_spec_snap     VARCHAR(255) NOT NULL,          -- 下单时规格，如"暗夜黑;256G"
  cover_image_snap  VARCHAR(255) NOT NULL,          -- 下单时封面图
  unit_price_snap   BIGINT       NOT NULL,          -- 下单时单价（分）
  weight_g_snap     INT          NOT NULL,          -- 下单时重量（克），退款运费计算用
  num               INT          NOT NULL CHECK (num > 0),
  item_amount       BIGINT       NOT NULL,          -- = unit_price_snap * num，行小计
  -- 优惠分摊（见 05-promotion-engine）
  discount_amount   BIGINT       NOT NULL DEFAULT 0, -- 本行承担的总优惠（分）
  coupon_amount     BIGINT       NOT NULL DEFAULT 0, -- 其中券优惠
  promo_amount      BIGINT       NOT NULL DEFAULT 0, -- 其中活动优惠
  point_amount      BIGINT       NOT NULL DEFAULT 0, -- 其中积分抵扣
  pay_amount        BIGINT       NOT NULL,           -- = item_amount - discount_amount
  -- 退款进度（用于部分退计算）
  refunded_num      INT          NOT NULL DEFAULT 0,
  refunded_amount   BIGINT       NOT NULL DEFAULT 0, -- 已退金额（含优惠还原）
  item_status       SMALLINT     NOT NULL DEFAULT 1, -- 1正常 2退款中 3已退款 4换货中
  warehouse_id      BIGINT       NOT NULL,           -- 发货仓，下单时定，运费计算用
  -- ★ 金额守恒与退款上限由数据库兜底
  CONSTRAINT ck_item_amount  CHECK (item_amount = unit_price_snap * num),
  CONSTRAINT ck_item_pay     CHECK (pay_amount = item_amount - discount_amount),
  CONSTRAINT ck_item_refund  CHECK (refunded_num <= num AND refunded_amount <= pay_amount)
);
CREATE INDEX idx_order_item_sub  ON trade.order_item (order_sub_no);
CREATE INDEX idx_order_item_main ON trade.order_item (order_main_no);
CREATE INDEX idx_order_item_sku  ON trade.order_item (sku_id);
COMMENT ON TABLE trade.order_item IS '订单商品项（快照）';
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

## 7. 商品搜索（PostgreSQL）

第一期不引入 Elasticsearch，用 PG 自带能力实现。以 **SPU 为搜索单元**，维护一个冗余的搜索文本列，把标题、品牌、类目路径和所有在售 SKU 的规格值拼进去：

```sql
ALTER TABLE product.spu ADD COLUMN search_text TEXT NOT NULL DEFAULT '';
-- 例："iPhone 16 Pro Apple 数码 手机 暗夜黑 原色钛 256G 512G"
CREATE INDEX idx_spu_search_trgm ON product.spu USING gin (search_text gin_trgm_ops);
```

`search_text` 在 SPU/SKU 保存时由 product 模块重算写入（同一事务），不依赖触发器。

```sql
-- 用户搜索 "黑色 256G iPhone"：按空格拆词，每个词都要命中（AND）
SELECT id, title, main_image, price_min, price_max, total_sold
FROM product.spu
WHERE status = 2 AND NOT deleted
  AND search_text ILIKE '%黑色%'
  AND search_text ILIKE '%256G%'
  AND search_text ILIKE '%iPhone%'
  AND price_max >= :price_from AND price_min <= :price_to   -- 价格区间交叉判断
ORDER BY total_sold DESC, id DESC
LIMIT 20 OFFSET :offset;
```

- `pg_trgm` 的 GIN 索引能加速 `ILIKE '%xx%'`，对中文同样生效（按字符三元组），**无需分词**即可做子串匹配。
- 召回之后**返回 SPU 卡片**，SKU 级别的规格匹配在详情页处理。
- 排序字段 `total_sold` 是冗余计数，支付成功事件中异步累加，避免搜索时实时聚合。
- 二期若需要相关度排序与同义词，可装 `zhparser` 扩展做中文分词 + `tsvector`，或再引入专门的搜索引擎；接口层不变。

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
7. 上架时：写 sku_stock 初始化库存 + 更新 spu.search_text（同一事务）→ 提交后同步库存到 Redis
```

**第 7 步必须幂等**：重复上架不能造成库存翻倍。做法是以 `(sku_id, warehouse_id)` 为主键做 `INSERT ... ON CONFLICT (sku_id, warehouse_id) DO UPDATE`，Redis 库存用 `SET`（覆盖，不是 `INCR`）。Redis 同步失败不影响上架结果，由库存对账任务补齐（见 [03](03-inventory.md) §7）。
