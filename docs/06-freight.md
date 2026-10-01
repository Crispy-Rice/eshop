# 06 运费计算

## 0. 实现状态

已按本文实现（`app/modules/freight/`）：模板/区域规则/SKU 绑定/不发货区域四张表，
纯函数计算引擎（`calculator.py`），商家后台的模板配置页，
**并已接入 `POST /api/checkout/calc`** —— 结算页的运费不再是 0。

四处按依赖现状裁剪：

| 项 | 说明 |
|---|---|
| 计费类型"按体积" | 表结构留了 `charge_type=3`，第一期只实现重量与件数 |
| 运费**分摊到子单**（§6 末段） | 拆单属于 trade 模块，现在还没有子单。最大余数法已在 `promotion/allocation.py` 实现，届时直接复用 |
| 退货时的运费处理（§8） | 依赖 aftersale |
| 模板的 Redis 缓存（§11） | 第一期直接查 DB，量小 |

**已验证的数值**（docs/06 §4.1 与 §5.3 的例子都在 `tests/test_freight_calc.py` 里跑通）：

- 1 件 1200g 的商品 → 首重 1000g/10元 + 续重 500g/3元 → **13 元**（超 200g 也收一个续重单位）
- 同仓两个 SKU 绑不同模板 → 取首重最高者，合并 1400g → **15 元**（不合并会是 25 元）

## 1. 需求拆解

| 需求 | 设计要点 |
|---|---|
| 每个 SKU 绑定多个运费规则 | SKU ↔ 运费模板 多对多，按仓库/区域区分 |
| 首重 / 续重 | 分段计费：首重价格 + (超重部分 / 续重单位) 向上取整 × 续重价格 |
| 不同仓库 | 运费按仓库独立计算，再按规则合并 |
| 同订单不同 SKU 金额冲突 | **以首重最高的 SKU 为准** |

## 2. 模型分层

```
运费模板 freight_template
   ├── 配送区域 region（省/市，或"全国默认"/"偏远地区"）
   ├── 计费规则 freight_rule（首重重量、首重价、续重单位、续重价、是否包邮）
   └── 排除区域 exclude（新疆/西藏/港澳台等不发货）

SKU ──(多对多，带优先级)──> 运费模板
         sku_freight_bind(sku_id, template_id, warehouse_id, priority)
```

## 3. 表结构

### 3.1 运费模板

```sql
CREATE TABLE freight.freight_template (
  id             BIGINT       PRIMARY KEY,
  shop_id        BIGINT       NOT NULL,
  name           VARCHAR(64)  NOT NULL,              -- 如"默认快递模板"
  charge_type    SMALLINT     NOT NULL DEFAULT 1,    -- 1按重量 2按件数 3按体积
  -- 首重/续重（weight 型）
  first_unit     INT          NOT NULL DEFAULT 1000, -- 首重（克），按件数时表示首件数
  first_price    BIGINT       NOT NULL CHECK (first_price >= 0), -- 首重价（分）
  add_unit       INT          NOT NULL DEFAULT 1000 CHECK (add_unit > 0), -- 续重单位（克）
  add_price      BIGINT       NOT NULL CHECK (add_price >= 0),   -- 续重价（分/单位）
  -- 组合选项
  free_shipping  BOOLEAN      NOT NULL DEFAULT false, -- 全场包邮
  free_threshold BIGINT       NOT NULL DEFAULT 0,    -- 满额包邮（分），0=不参与
  free_num       INT          NOT NULL DEFAULT 0,    -- 满件包邮，0=不参与
  merge_type     SMALLINT     NOT NULL DEFAULT 1,    -- 多 SKU 合并方式，见 §5
  status         SMALLINT     NOT NULL DEFAULT 1,
  created_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now()
);
CREATE INDEX idx_freight_tpl_shop ON freight.freight_template (shop_id, status);
COMMENT ON TABLE freight.freight_template IS '运费模板';
```

### 3.2 区域规则

```sql
CREATE TABLE freight.freight_region_rule (
  id             BIGINT      PRIMARY KEY,
  template_id    BIGINT      NOT NULL REFERENCES freight.freight_template (id),
  region_code    VARCHAR(16) NOT NULL,     -- 行政区划码，"0"=全国默认，"999999"=偏远
  region_level   SMALLINT    NOT NULL,     -- 1省 2市 3区
  first_unit     INT         NOT NULL,     -- ★ 覆盖模板默认值，实现"不同区域不同首重"
  first_price    BIGINT      NOT NULL,
  add_unit       INT         NOT NULL CHECK (add_unit > 0),
  add_price      BIGINT      NOT NULL,
  free_shipping  BOOLEAN     NOT NULL DEFAULT false,
  enabled        BOOLEAN     NOT NULL DEFAULT true,
  priority       INT         NOT NULL DEFAULT 0, -- 越大越优先（市 > 省 > 全国）
  CONSTRAINT uk_freight_rule_tpl_region UNIQUE (template_id, region_code)
);
COMMENT ON TABLE freight.freight_region_rule IS '模板-区域运费规则';
```

唯一约束 `(template_id, region_code)` 的首列已覆盖按模板查询，不再单独建 `(template_id, enabled)` 索引。

### 3.3 SKU ↔ 模板 绑定（多对多）

```sql
CREATE TABLE freight.sku_freight_bind (
  id            BIGINT      PRIMARY KEY,
  sku_id        BIGINT      NOT NULL,
  template_id   BIGINT      NOT NULL REFERENCES freight.freight_template (id),
  warehouse_id  BIGINT      NOT NULL,          -- 该绑定的适用仓库
  priority      INT         NOT NULL DEFAULT 0, -- 同 SKU 多模板时的优先级，越大越优先
  enabled       BOOLEAN     NOT NULL DEFAULT true,
  CONSTRAINT uk_sku_freight_bind UNIQUE (sku_id, template_id, warehouse_id)
);
-- 只索引启用的绑定，按优先级倒序取第一条
CREATE INDEX idx_sku_freight_active ON freight.sku_freight_bind (sku_id, warehouse_id, priority DESC)
  WHERE enabled;
COMMENT ON TABLE freight.sku_freight_bind IS 'SKU 与运费模板绑定（多对多）';
```

**"每个 SKU 绑定多个运费规则"的落地**：一个 SKU 可以有 3 条绑定记录：
- （模板 A，仓库 1，priority 10）——主仓发
- （模板 B，仓库 2，priority 5）——备仓发
- （模板 C，仓库 1，priority 0）——促销期特价运费

下单时**按实际发货仓筛选**，取 `priority` 最高的那条生效。

### 3.4 不发货区域

```sql
CREATE TABLE freight.freight_exclude_region (
  id          BIGINT      PRIMARY KEY,
  template_id BIGINT      NOT NULL REFERENCES freight.freight_template (id),
  region_code VARCHAR(16) NOT NULL,
  reason      VARCHAR(64),                 -- 如"暂不配送"
  CONSTRAINT uk_freight_exclude_tpl_region UNIQUE (template_id, region_code)
);
```

## 4. 单 SKU 运费计算

### 4.1 按重量（最常用）

```
输入：sku 重量 w（克），数量 n，总重 W = w * n
规则：首重 F 克 / 首重价 P_f，续重单位 A 克 / 续重价 P_a

若 W <= F:
    freight = P_f
否则:
    overWeight  = W - F
    addUnits    = ceil(overWeight / A)          ← ★ 向上取整，不足一个单位按一个计
    freight     = P_f + addUnits * P_a
```

Python 实现中用整数向上取整，避免 `math.ceil(a / b)` 走浮点：

```python
def ceil_div(a: int, b: int) -> int:
    """a >= 0, b > 0 时的整数向上取整。"""
    return -(-a // b)

def charge_by_weight(total_g: int, rule: RegionRule) -> int:
    if total_g <= rule.first_unit:
        return rule.first_price
    return rule.first_price + ceil_div(total_g - rule.first_unit, rule.add_unit) * rule.add_price
```

**例子**：首重 1000g / 10 元，续重 500g / 3 元。商品 200g，买 8 件 = 1600g。
```
overWeight = 600g
addUnits   = ceil(600 / 500) = 2
freight    = 10 + 2 * 3 = 16 元
```

**边界**：`W = 1001g` → `overWeight = 1` → `addUnits = 1` → 运费 = 13 元。虽然只超 1 克，但按规则要收一个续重单位。**这是行业标准做法**，但结算页必须提前告知用户（在购物车和结算页展示"预计运费"，避免到支付才发现）。

### 4.2 按件数

```
若 n <= first_unit:  freight = first_price
否则:                freight = first_price + ceil((n - first_unit) / add_unit) * add_price
```

### 4.3 满额/满件包邮

```
若 模板.free_shipping = 1 → 直接 0
若 模板.free_threshold > 0 且 本模板下商品金额 >= free_threshold → 0
若 模板.free_num > 0 且 本模板下商品件数 >= free_num → 0
```

**包邮判定用的是"该模板下商品的金额/件数"**，不是整单金额。这是常见误解——不同模板的商品各自判断。

## 5. 多 SKU 合并：冲突裁决

这是需求中最特殊的一条。**同一订单包含多个 SKU 时，如果运费规则冲突，以首重最高的 SKU 为准。**

### 5.1 什么算"冲突"

两个 SKU 在**同一仓库**发货（这是前提——跨仓不算冲突，见 §6），但绑定了不同运费模板，且这两个模板的参数不一致。

```python
def is_conflict(a: ItemFreight, b: ItemFreight) -> bool:
    """同仓、跨模板，且计费参数不一致即为冲突。"""
    def params(f: ItemFreight) -> tuple[int, int, int, int, int]:
        return (f.charge_type, f.first_unit, f.first_price, f.add_unit, f.add_price)

    return (a.warehouse_id == b.warehouse_id
            and a.template_id != b.template_id
            and params(a) != params(b))
```

### 5.2 裁决规则

```
规则 R1（需求指定）：同仓库、跨模板冲突时，取"首重最高"的 SKU 的模板为准
   "首重最高"的排序键（依次比较）：
      ① 首重 weight 大者优先（first_unit）
      ② first_unit 相同时，首重价 first_price 大者优先
      ③ 都相同时，add_price 大者优先
      ④ 全相同则按 skuId 升序（确定性）

规则 R2：合并后只收一次首重
   合并重量 = Σ(该仓库下所有 SKU 的重量 × 数量)
   运费 = P_f(胜出模板) + ceil((合并重量 - F) / A) * P_a(胜出模板)

规则 R3：未被"胜出模板"覆盖的 SKU（比如它本来绑定的是另一个模板）
   其重量仍然计入合并重量（因为同仓一起发），只是计费规则用胜出者的
```

**为什么这样设计**：同仓库一起发一个包裹，物理上只有一次首重成本。如果按每个 SKU 的模板各收一次首重，用户会被重复收费（3 个商品收 3 次首重），这是不合理的。取"首重最高"的模板是平台侧的保守选择——避免商家因为低价模板漏收运费而亏损。

### 5.3 完整示例

```
订单（同一仓库 WH-1 发货）：
  SKU-A: 2 件 × 300g = 600g  模板 T1: 首重 1000g/10元, 续重 500g/3元
  SKU-B: 1 件 × 800g = 800g  模板 T2: 首重 2000g/15元, 续重 1000g/5元

① 冲突检测：同仓跨模板 → 冲突
② 排序：T2.first_unit(2000) > T1.first_unit(1000) → T2 胜出
③ 合并重量 = 600 + 800 = 1400g
④ 用 T2 规则：1400 <= 2000 → 运费 = 15 元（首重价）

对比：如果不合并，各算各的
   SKU-A: 600g <= 1000g → 10 元
   SKU-B: 800g <= 2000g → 15 元
   合计 25 元
合并后只收 15 元 —— 对用户更友好，符合"一次首重"的物理事实。
```

```
另一个场景：续重区间不同
  SKU-A: 1200g  模板 T1: 首重 1000g/10元, 续重 500g/3元
  SKU-B:  500g  模板 T3: 首重 500g/8元,  续重 250g/2元

① 冲突：T1.first_unit(1000) > T3.first_unit(500) → T1 胜出
② 合并重量 = 1700g
③ T1 计费：over = 700, units = ceil(700/500) = 2 → 10 + 2*3 = 16 元
```

### 5.4 冲突裁决的例外

| 例外 | 处理 |
|---|---|
| 其中一个模板是"包邮" | **只要有一个包邮不冲突**？→ 不。包邮是最宽松的规则，但如果取包邮模板，商家会亏。**规则**：包邮模板与付费模板冲突时，按 §5.2 正常裁决；若包邮模板胜出（首重最高），则整仓包邮 |
| 其中一个模板的 `charge_type` 不同（重量 vs 件数） | 无法直接比较首重 → **统一转成"等效首重价"比较**：`chargeType=件数` 时按平均单件重量折算成克。若无法折算，则取 `first_price` 大者 |
| 区域规则不同（T1 对上海 10 元，T2 对上海 15 元） | 先各自算出**该收货地址下的实际首重价**，再比 `first_unit`；`first_unit` 相同则比实际首重价 |
| 一个 SKU 绑定了多个模板（多对多） | 先按 `priority` 选出生效模板，再做跨 SKU 裁决 |

### 5.5 裁决的伪代码

```python
def calc_freight(items: list[FreightItem], region_code: str, tpls: FreightTemplateCache) -> FreightResult:
    """纯函数：模板数据由调用方预加载到 tpls，计算过程无 IO。"""
    result = FreightResult()

    # ① 按仓库分组（按仓库 ID 排序，保证结果确定）
    by_warehouse: dict[int, list[FreightItem]] = defaultdict(list)
    for it in items:
        by_warehouse[it.warehouse_id].append(it)

    for wh_id in sorted(by_warehouse):
        # ② 每个 item 解析出生效模板（含区域规则覆盖）
        resolved: list[ItemFreight] = []
        for it in by_warehouse[wh_id]:
            tpl = tpls.resolve_template(it.sku_id, wh_id)            # 按 priority 选
            rule = tpls.resolve_region_rule(tpl.id, region_code)     # 市 > 省 > 全国
            if rule is None or rule.excluded:
                raise BizError(ErrorCode.NOT_DELIVERABLE,
                               f"商品 {it.title} 不支持配送至该地区")
            resolved.append(ItemFreight(item=it, tpl=tpl, rule=rule))

        # ③ 冲突裁决：取首重最高者；平局按 sku_id 升序（取负数后求 max）
        winner = max(resolved, key=lambda f: (f.first_unit, f.first_price, f.add_price, -f.item.sku_id))

        # ④ 包邮判定（按胜出模板的口径，统计该模板"覆盖"的金额/件数）
        if is_free_shipping(winner, resolved):
            result.add_package(wh_id, freight=0, winner=winner)
            continue

        # ⑤ 合并计费：只收一次首重
        total_weight = sum(f.item.weight_g * f.item.num for f in resolved)
        total_qty = sum(f.item.num for f in resolved)
        result.add_package(wh_id, freight=compute_charge(winner, total_weight, total_qty), winner=winner)

    return result   # result.total 为各包裹运费之和，result.packages 写入 freight_detail 快照
```

## 6. 跨仓:多包裹分别计费

**不同仓库发货 = 不同包裹 = 各收一次首重**。这与 §5 的"同仓合并"不冲突——物理上确实是两个包裹。

```
订单：
  SKU-A × 2（仓 WH-1，600g，模板 T1）
  SKU-B × 1（仓 WH-2，800g，模板 T2）

运费 = 包裹1(WH-1, 600g, T1) + 包裹2(WH-2, 800g, T2)
     = 10 + 15 = 25 元
```

**为什么拆单后运费可能变化**：如果用户先买了 SKU-A（仓 WH-1），后来又买了 SKU-B（仓 WH-2），两个订单各收首重。但如果一起下单，还是各收——所以**跨仓下单不会因为合并而省钱**，这是告知用户的关键点（结算页需展示"该商品由不同仓库发出，运费分别计算"）。

**运费的拆单归属**：运费也要拆到子单上（因为退款要退运费）。

```
母单运费 = Σ 各子单运费
子单运费 = 该仓（该店铺）的运费
```

注意：同一个仓库可能服务多个店铺。**拆单以店铺为维度**（见 [07](07-order-and-split.md)），但**运费以仓库为维度**。当"一个仓库的多个店铺"时：

```
仓 WH-1 中有 店铺A 和 店铺B 的商品
  → 拆成两个子单（A、B）
  → 但物理上是一个包裹
  → 运费怎么算？

方案：按店铺拆单时，运费也按店铺拆分；但同一仓库的多个店铺的商品，
      先按仓库合并算出一个总运费，再按各店铺商品金额占比分摊到子单。
      这样可以避免用户被收两次首重（快递实际是打包发的）。
```

分摊用 [05 §6](05-promotion-engine.md) 的最大余数法，保证子单运费之和 == 母单运费。

```python
# 运费分摊到子单（同一包裹内的多个店铺子单）
sub_freights = allocate(package_freight, [sub.total_amount for sub in subs_in_package])
```

## 7. 运费券

运费券作用于运费的抵扣，**计算顺序在运费算出之后**：

```
① 促销引擎算完商品优惠 → payableBeforeFreight
② freight 模块算出 freight
③ 若用户有运费券且可用：
     deductible = min(运费券面额, freight)
     freight = freight - deductible
④ payable = payableBeforeFreight + freight
```

**运费券的约束**：
- 一张订单只能用一张运费券
- 运费券不参与商品金额的优惠计算（不影响商品优惠门槛）
- 运费券的 `threshold` 判断针对**运费本身**（如"满 20 元运费减 10"），不是商品金额（这是与普通券的区别）
- 退运费时，运费券**不退回**（部分退货时不退运费）

## 8. 运费在退货时的处理

| 场景 | 运费处理 |
|---|---|
| 未发货退款（整单） | 退全部运费 |
| 未发货退款（部分） | 退对应子单运费（按分摊值） |
| 已发货退货（整单，质量问题） | 退运费 + 商家承担退货运费 |
| 已发货退货（整单，非质量问题） | 退**发货运费**，用户承担退货运费 |
| 已发货退货（部分） | **不退运费**（因为包裹已发出，运费已产生） |
| 七天无理由 | 同上，发货运费退，退货运费用户出 |

**判定依据**：`freight_amount` 和分摊明细都存在订单里，退款时直接读，不重算。

```sql
-- 母单的运费
SELECT freight_amount FROM trade.order_main WHERE order_main_no = :no;
-- 子单的分摊运费（拆单时写入）
SELECT freight_amount FROM trade.order_sub WHERE order_sub_no = :no;
```

**关键设计**：运费**必须分摊到子单并持久化**。如果退款时用"按比例重算"，因为商品可能部分退款过，重算的基数变了，结果会和用户当时付的不一致。

## 9. 结算页与下单的一致性

运费也是**价格一致性**的一部分（见 [11](11-price-consistency.md)）：

```
结算页：POST /api/checkout/calc 内部调 freight_service.calc → freight = 16.00 → 计入 priceToken
下单时：POST /api/orders 内部重新调 freight_service.calc → 16.00 → 与 priceToken 中的对比
  一致 → 继续
  不一致 → 409 PRICE_CHANGED，提示"运费已变化，请确认"
```

**运费变化的常见原因**：库存扣减导致某 SKU 无货（拆出该行）、收货地址变更、商家改了模板、快递区域政策调整。

## 10. 边界场景

| 场景 | 处理 |
|---|---|
| SKU 未绑定任何运费模板 | 拒绝下单，返回 `SKU_NOT_SUPPORTED`。商品发布时强制校验："未绑定运费模板不可上架" |
| 收货地址在排除区域 | 结算页就提示"该地区暂不配送"，不进入下单流程 |
| 模板被删除但 SKU 还绑定着 | 软删模板；绑定关系失效时回退到"店铺默认模板"；没有默认模板则拒绝 |
| 首重 0 克（按件数模板误配） | 校验：`first_unit > 0 || charge_type == 件数`，发布时拦截 |
| 超大重量（如 100kg） | 超过 `add_unit * 100` 时提示"请联系客服"，避免算出的运费离谱（如 10000 元） |
| 运费为 0（包邮） | 正常，`freight_amount = 0`，不影响后续流程 |
| 同一模板但区域规则不同 | 按 §5.4 的例外处理：先算出各自的实际首重价再比较 |
| 商品重量数据缺失（weight_g = 0） | 发布时强制重量 > 0；历史脏数据兜底按 500g 计（并告警） |

## 11. 性能

| 优化 | 说明 |
|---|---|
| 模板缓存 | 模板 + 区域规则整体缓存到 Redis（JSON），TTL 10 分钟；商家修改模板时同事务写 outbox，提交后删除缓存键 |
| 区域码解析 | 收货地址 → 区域码，一次性解析出省/市/区三级，避免逐级查询 |
| 批量计算 | 结算页一次算整单运费，不逐 SKU 查询 |
| 纯内存计算 | 模板从缓存拿到后，`calc_freight` 是纯函数，过程无 IO |

**freight 模块与促销引擎一样，计算阶段不允许 `await`。**

## 12. 商家后台的配置界面要点

商家配置运费模板时的关键校验：

```
1. 首重价 >= 0，续重价 >= 0
2. first_unit > 0（按重量时）
3. 区域规则如果填了"上海市"，则必须有"全国默认"（否则其他地区无规则可匹配）
4. 排除区域不能与配送区域冲突
5. 包邮选项与付费规则并存时，明确提示"包邮优先"
6. 修改模板时提示"已绑定 N 个 SKU，修改后立即对新建订单生效，已下单订单不受影响"
```

第 6 点的实现：订单创建时把运费的**计算明细快照**写入订单（`freight_detail` JSONB 字段），这样即使模板改了，历史订单的运费依据仍可追溯。
