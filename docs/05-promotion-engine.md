# 05 促销计算引擎与优惠分摊

> 复杂优惠券的**设计**（规则模型）和**计算**（算法）都在这里。这是全系统最复杂的一块。

## 1. 为什么"复杂优惠券"难

朴素做法是"一堆 if-else"：满减券 + 折扣券 + 店铺券 + 平台券 + 积分 + 会员折扣 + 秒杀价……组合数是爆炸的。

```
平台券(3选1) × 店铺券(2选1) × 活动(2选1) × 积分(用/不用) = 12 种组合
再乘上"顺序不同结果不同" → 需要确定性算法
```

三个必须解决的问题：

| 问题 | 说明 |
|---|---|
| **可叠加性判定** | 哪些券能同时用？规则必须数据化，不能写死在代码里 |
| **计算顺序** | 先打折还是先满减？结果金额不同（折后价是否参与满减门槛） |
| **优惠分摊** | 总优惠 100 元分摊到 3 个商品上，必须**分得干净**（加起来正好 100，不能有 99.99 或 100.01） |

## 2. 优惠类型全景

| 类别 | 类型 | 计算方式 | 归属 |
|---|---|---|---|
| **单品促销** | 直降 | `price = price - X` | 商品级 |
| | 折扣 | `price = price * rate` | 商品级 |
| | 特价/秒杀 | `price = 固定值` | 商品级 |
| | 第N件半价 | 按数量分组打折 | 商品级 |
| | 赠品 | 不影响金额，加赠品行 | 商品级 |
| **订单促销** | 满减（阶梯） | 满 A 减 B，取最高档 | 订单级 |
| | 满折（阶梯） | 满 A 打 X 折 | 订单级 |
| | 满赠 | 达到门槛送赠品 | 订单级 |
| **优惠券** | 满减券 | 门槛 + 减免额 | 订单级（可限定范围） |
| | 折扣券 | 门槛 + 折扣率 + 封顶 | 订单级 |
| | 无门槛券 | 直接减 | 订单级 |
| | 运费券 | 抵扣运费 | 运费级 |
| **虚拟资产** | 积分抵扣 | 积分换钱 | 订单级 |
| | 余额/红包 | 直接抵现 | 订单级 |

## 3. 分层模型：四个层级

**核心设计**：给每种优惠分配一个**层级（level）**，按层依次计算。层级内的优惠**互斥或取最优**，层间**按固定顺序叠加**。

```
Level 0  商品级促销（单品直降 / 折扣 / 秒杀价）
   │       ↓ 得到"促销价"（item.promoPrice）
   │
Level 1  店铺级优惠（店铺满减活动 / 店铺券）
   │       ↓ 作用在"参与店铺优惠的金额"上
   │
Level 2  平台级优惠（平台满减活动 / 平台券）
   │       ↓ 作用在"参与平台优惠的金额"上
   │
Level 3  虚拟资产（积分抵扣 / 红包 / 余额）
   │       ↓ 作用在"剩余应付金额"上
   │
   └──► 最终应付金额
```

### 3.1 层级设计的理由

1. **确定性**：顺序固定，同一个购物车永远算出同一个结果（可缓存、可复现、可对账）。
2. **可解释**：前端能清晰地展示"商品优惠 -50，店铺优惠 -30，平台券 -20"，而不是一个黑盒数字。
3. **可扩展**：新增一种优惠类型，只需确定它属于哪一层，不用改动其它层。

### 3.2 层内规则：互斥与取最优

**同一层级内，默认互斥，只能生效一个**。多个候选时取"用户实际支付最少"的那个。

```
Level 1 候选：[店铺满200减30, 店铺9折券(最高减50)]
  店铺券计算结果：
    满减券 → 减 30
    9折券  → 300 * 0.1 = 30 → 减 30
  两者相同 → 按优先级（面额高的优先？券到期早的优先？）→ 规则：先取优惠大的，
            相同的取"用户当前选中的"，未选中的按 valid_end 升序（快过期的先用）
```

**例外：明确声明可叠加的券**。`coupon_template.stackable = 1` 的券可以与同层其它券叠加（如"店铺券 + 平台券"跨层天然可叠）。同层叠加需要额外规则（见 §4.2）。

## 4. 叠加规则建模

### 4.1 叠加矩阵（数据化，不写死在代码里）

```sql
CREATE TABLE `promo_stack_rule` (
  `id`            BIGINT      NOT NULL,
  `rule_name`     VARCHAR(64) NOT NULL,
  `type_a`        VARCHAR(32) NOT NULL COMMENT '优惠类型A，如 COUPON_PLATFORM',
  `type_b`        VARCHAR(32) NOT NULL COMMENT '优惠类型B',
  `stackable`     TINYINT     NOT NULL COMMENT '1可叠加 0互斥',
  `priority`      INT         NOT NULL DEFAULT 0 COMMENT '优先级，用于冲突裁决',
  `shop_id`       BIGINT      NOT NULL DEFAULT 0 COMMENT '0=全局规则',
  `effective_from` DATETIME(3) DEFAULT NULL,
  `effective_to`   DATETIME(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_pair` (`type_a`, `type_b`, `shop_id`)
) ENGINE=InnoDB COMMENT='优惠叠加规则矩阵';
```

初始化数据：

| type_a | type_b | stackable | 说明 |
|---|---|---|---|
| PROMO_ITEM | PROMO_ORDER_SHOP | 1 | 单品促销 + 店铺满减，可叠 |
| PROMO_ITEM | COUPON_PLATFORM | 1 | 单品促销 + 平台券，可叠 |
| PROMO_ORDER_SHOP | PROMO_ORDER_PLATFORM | 1 | 店铺活动 + 平台活动，可叠 |
| COUPON_SHOP | COUPON_PLATFORM | 1 | 店铺券 + 平台券，可叠（默认允许） |
| PROMO_ORDER_SHOP | COUPON_SHOP | 0 | **店铺活动与店铺券互斥**（商家自己定） |
| PROMO_ORDER_PLATFORM | COUPON_PLATFORM | 0 | 平台活动与平台券互斥（防资损） |
| COUPON_PLATFORM | COUPON_PLATFORM | 0 | 平台券之间互斥（同层只能一张） |
| POINT | COUPON_PLATFORM | 1 | 积分与券可叠（积分放最后） |
| PROMO_ITEM | PROMO_ITEM | 0 | 单品促销之间互斥（取最优） |

**设计原则**：默认互斥，显式声明可叠。这样新增优惠类型时，默认是"安全的"（不能叠），避免漏配规则导致资损。

### 4.2 冲突组（Conflict Group）

更精确的表述：把所有的优惠放入"冲突组"，同组内只能选一定数量。

```java
// 每个优惠声明自己属于哪个冲突组，以及组内可用的数量上限
record Discount(long id, String group, int groupMaxSelect, long amount, int priority) {}

// 组定义示例
// group = "PLATFORM_COUPON"    maxSelect = 1   ← 平台券只能 1 张
// group = "SHOP_COUPON:{shopId}" maxSelect = 1 ← 每个店铺的券只能 1 张
// group = "ITEM_PROMO:{skuId}" maxSelect = 1   ← 每个 SKU 只能命中 1 个单品活动
// group = "PLATFORM_PROMO"     maxSelect = 1   ← 平台活动只能 1 个
// group = "POINT"              maxSelect = 1
```

**组间是否可叠**由 `promo_stack_rule` 决定。计算时：

```
1. 枚举所有候选优惠，按 group 分桶
2. 每个桶内按"优惠金额降序 + 优先级 + 过期时间升序"排序，取前 maxSelect 个
3. 检查跨组冲突矩阵，移除冲突的
4. 按 level 排序输出
```

## 5. 金额计算算法

### 5.1 关键概念：参与优惠金额（eligibleAmount）

**每个优惠不是作用在"订单总额"上，而是作用在"它能作用的那部分金额"上**。

```java
class ItemCalc {
    long skuId;
    int  num;
    long unitPrice;        // 原价（分）
    long promoPrice;       // Level0 后的价格
    long promoAmount;      // Level0 的优惠额（原价 - promo价）* num

    long shopEligible;     // 参与店铺级优惠的金额
    long platformEligible; // 参与平台级优惠的金额
    // 每层计算后递减
}
```

### 5.2 逐层计算

```
初始：
  for each item:
      item.promoPrice     = applyItemPromo(item)                // Level 0
      item.promoAmount    = (unitPrice - promoPrice) * num
      item.currentAmount  = promoPrice * num                    // 当前剩余金额

Level 1（店铺级）：
  for each shop group:
      shopAmount     = sum(item.currentAmount for item in shop)
      shopEligible   = sum(item.currentAmount if item in 店铺优惠范围)
      candidates     = 店铺满减活动 + 该店铺的店铺券
      best           = pickBest(candidates, shopEligible)        // 取最优/按冲突组
      discount       = best.compute(shopEligible)
      allocate(discount, items in 范围的 item)                   // 分摊，见 §6
      for each item: item.currentAmount -= 该项分摊额

Level 2（平台级）：
  platformEligible = sum(item.currentAmount if item in 平台优惠范围)
  similar to Level 1
  （注意：平台券的 scope 可能是"指定商品"，所以 eligible 只算范围内的）

Level 3（积分/余额）：
  pointDeduct = min(floor(积分余额 / 兑换比例), floor(orderAmount * 最大抵扣比例))
  allocate(pointDeduct, all items)
```

### 5.3 折扣计算的两个坑

**坑 1：折扣是按行算还是按总额算**

```
商品A：100.00 元，商品B：33.33 元，9折券
按行算：A → 90.00（省10.00），B → 30.00（省3.33，实际29.997→30.00）  合计 120.00
按总额算：(100.00 + 33.33) * 0.9 = 119.997 → 120.00  省 13.33
```

**规则：折扣必须按"作用域总额"算，再分摊到行**。如果按行算，`33.33 * 0.9 = 29.997` 需要四舍五入，多行累积会产生分差，而且和"按总额算"的结果对不上，导致前后端金额不一致。

**坑 2：舍入方向**

```
折扣后金额 = floor(amount * rate)   // 用 floor，对平台有利还是对用户有利？
```

规则：**折扣向下取整（用户多得 1 分）或四舍五入？** 选择**四舍五入到分**（`Math.round(amount * rate / 10000.0)` 的整数实现），理由：
- 向下取整在部分退款时容易出现"退的比付的多"（因为多次向下取整的累积误差）；
- 四舍五入在数学期望上中立，配合分摊算法能保证守恒。

**所有金额运算的绝对规则**：

```java
// ✅ 正确：全整数运算
long discounted = amount * rate / 10000;                  // rate = 8500 表示 85 折
long rounded    = (amount * rate + 5000) / 10000;         // 四舍五入

// ❌ 错误：浮点
double d = amount * 0.85;   // 0.1 + 0.2 != 0.3 的经典问题
```

## 6. 分摊算法（核心）

### 6.1 问题

优惠 30 元，作用在 3 个商品上：A=10.00，B=20.00，C=70.00（合计 100.00）。

按比例：A 分 3.00，B 分 6.00，C 分 21.00 → 正好 30.00。**但如果是 33 元呢？**

```
A: 33 * 10/100 = 3.30
B: 33 * 20/100 = 6.60
C: 33 * 70/100 = 23.10
合计 = 33.00  ← 恰好
```

再试 33.33 元，三行：A=3.333, B=6.666, C=23.331 → 各自四舍五入 = 3.33 + 6.67 + 23.33 = 33.33 ✅

但试 10 元分摊到 3 个相等的商品：3.333, 3.333, 3.334 → 如果都向下取整 = 3+3+3 = 9 ≠ 10。**差 1 分去哪了？**

### 6.2 最大余数法（Largest Remainder Method）

```
1. 计算每个 item 应分摊的"精确值" exact_i = totalDiscount * eligible_i / totalEligible
2. 取每个 item 的整数部分 floor_i = floor(exact_i)，余数 rem_i = exact_i - floor_i
3. 计算已分配的整数和 sumFloor = Σ floor_i
4. 缺口 gap = totalDiscount - sumFloor（一定是 0 或 1，最多是 (n-1) 分）
5. 按 rem_i 从大到小排序，取前 gap 个 item，各 +1 分
6. 平局时的裁决：rem 相同 → 按 item.id 升序（确定性）
```

**严谨性说明**：用整数运算实现，避免浮点误差。

```java
/**
 * 最大余数法分摊
 * @param totalDiscount 总优惠（分）
 * @param items 分摊对象，含 eligible（分）
 * @return 每个 item 分到的金额（分），和为 totalDiscount
 */
public static long[] allocate(long totalDiscount, long[] eligible) {
    int n = eligible.length;
    long[] result = new long[n];
    long totalEligible = 0;
    for (long e : eligible) totalEligible += e;

    if (totalEligible <= 0) return result;  // 无可分摊基数，全为 0

    // ① 精确值（用 long 保存分子，避免浮点）
    long allocated = 0;
    long[] remainder = new long[n];
    for (int i = 0; i < n; i++) {
        long numerator = totalDiscount * eligible[i];     // 可能溢出？见下方说明
        result[i] = numerator / totalEligible;            // floor
        remainder[i] = numerator % totalEligible;         // 余数
        allocated += result[i];
    }

    // ② 补足缺口：按余数降序，平局按 id 升序
    long gap = totalDiscount - allocated;                 // 0 <= gap < n
    Integer[] idx = ...;  // 0..n-1 按 (remainder desc, index asc) 排序
    for (int k = 0; k < gap; k++) {
        result[idx[k]] += 1;
    }
    return result;
}
```

**溢出防护**：`totalDiscount * eligible[i]` 最大是 `10^8 * 10^8 = 10^16`，`long` 上限约 `9.2 * 10^18`，安全。但如果订单金额超过 1 亿元（`10^10` 分），乘积会到 `10^20` → 溢出。**加断言**：`Math.multiplyExact` 或限制单订单金额上限（业务上单订单不超过 1000 万元，完全够用）。

**另一种实现（BigDecimal）**：如果团队更信任 `BigDecimal`，用 `BigDecimal` + `RoundingMode.DOWN` + 同样的余数排序逻辑，结果等价。但性能差 10 倍以上，结算页算价是高频操作，推荐整数实现。

### 6.3 分摊的三个约束

```
约束 1（守恒）：Σ allocated_i == totalDiscount          ← 必须精确相等
约束 2（非负）：allocated_i >= 0                        ← 不能出现负分摊
约束 3（不超行金额）：allocated_i <= item.currentAmount  ← 不能把商品分摊到负数
```

**约束 3 的麻烦**：如果某行金额是 5 元，按比例分摊到了 8 元，怎么办？

```
场景：优惠 20 元，商品 A=5.00，B=95.00（合计 100）
按比例：A → 1.00，B → 19.00   ← 没问题
场景：无门槛券 20 元，商品 A=5.00，B=10.00（合计 15）
按比例：A → 6.67，B → 13.33   ← A 分摊 6.67 > A 的 5.00，A 变成负价！
```

**处理方案**：**逐行截断 + 迭代重分摊**。

```
1. cap_i = item.currentAmount（该行最多能被分摊多少）
2. 若 Σ cap_i < totalDiscount → 说明优惠超过了订单金额
   → totalDiscount = Σ cap_i（优惠封顶到订单金额，不能为负）
   → 若这是有门槛券，理论上不该发生；若是无门槛券，允许 0 元订单
3. 正常情况下：用"带上限的最大余数法"
   - 第一轮：按比例分摊，若某行超 cap，则置为该行的 cap，记录"溢出量"
   - 第二轮：把溢出量在未满 cap 的行中重新按比例分摊
   - 迭代直到溢出量为 0 或所有行都到 cap
   - 最后一轮用最大余数法补齐分差
```

```java
public static long[] allocateWithCap(long total, long[] eligible, long[] cap) {
    int n = eligible.length;
    long[] result = new long[n];
    long remaining = total;
    boolean[] done = new boolean[n];   // 已达上限的行

    while (remaining > 0) {
        // 收集未达上限的行
        long activeEligible = 0;
        int activeCount = 0;
        for (int i = 0; i < n; i++) {
            if (!done[i]) { activeEligible += eligible[i]; activeCount++; }
        }
        if (activeCount == 0) break;   // 全部达上限

        // 分摊本轮
        long allocated = 0;
        long[] thisRound = new long[n];
        List<int[]> rems = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            if (done[i]) continue;
            long num = remaining * eligible[i];
            thisRound[i] = num / activeEligible;
            rems.add(new int[]{i, (int)(num % activeEligible)});
        }
        // 余数补齐
        long sum = 0;
        for (long v : thisRound) sum += v;
        long gap = remaining - sum;
        rems.sort((a,b) -> b[1] != a[1] ? b[1]-a[1] : a[0]-b[0]);
        for (int k = 0; k < gap; k++) thisRound[rems.get(k)[0]] += 1;

        // 应用并检查上限
        long overflow = 0;
        for (int i = 0; i < n; i++) {
            if (done[i] || thisRound[i] == 0) continue;
            long space = cap[i] - result[i];
            if (thisRound[i] > space) {
                overflow += thisRound[i] - space;
                result[i] = cap[i];
                done[i] = true;
            } else {
                result[i] += thisRound[i];
            }
        }
        remaining = overflow;
    }
    return result;
}
```

### 6.4 为什么分摊必须精确（业务影响）

**部分退款场景**：用户买了 A、B 两件，用了 30 元券，只退 A。

```
item A: 原价 10.00，分摊优惠 3.00，实付 7.00
item B: 原价 20.00，分摊优惠 6.00，实付 14.00
（另有 C：70.00，分摊 21.00，实付 49.00）

退 A → 退款金额 = 7.00（实付的部分），不是 10.00
退 A+B → 退款 = 7.00 + 14.00 = 21.00
退全部 → 退款 = 70.00 - 30.00 + 运费
```

**如果分摊不精确**（比如三行分摊之和是 29.99），那么全额退款时退款金额 = 100 - 29.99 = 70.01，比实付多 1 分 → **平台资损，且对账永远对不平**。

这就是为什么分摊算法是资金安全的核心，必须：
1. 有单元测试覆盖各种边界（大额、小额、1 分钱、除不尽、行数 > 100）
2. 分摊明细**持久化**到 `order_item.discount_amount`，退款时直接读，不重算
3. 有守恒断言：`assert Σ item.discount_amount == order.discount_amount`

## 7. 完整的算价服务实现

### 7.1 输入输出

```java
// 输入（结算页 & 下单共用同一个服务）
public class CalcPriceRequest {
    Long userId;
    List<CalcItem> items;         // skuId, num, 选中的
    List<Long> couponCodeIds;     // 用户选中的券
    boolean usePoints;
    String addressId;             // 用于运费计算
}

public class CalcPriceResponse {
    // ★ 每个字段都是"可以给前端看的"，同时后端也会用同一份结果下单
    List<ItemResult> items;       // 每行的原价、促销价、分摊优惠、实付
    List<ShopResult> shops;       // 按店铺分组
    long totalAmount;             // 商品总额（原价）
    long itemDiscount;            // 单品促销优惠
    long shopDiscount;            // 店铺级优惠
    long platformDiscount;        // 平台级优惠
    long pointDeduction;          // 积分抵扣
    long freight;                 // 运费
    long payableAmount;           // 实付 = totalAmount - 所有优惠 + 运费
    List<UnavailableCoupon> unavailableCoupons;  // 不可用券 + 原因
    String priceToken;            // ★ 签名令牌，下单时必须带回
    long expireAt;                // token 过期时间
}
```

### 7.2 主流程伪代码

```java
public CalcPriceResponse calc(CalcPriceRequest req) {
    // ---------- ① 加载数据 ----------
    List<SkuInfo> skus = productClient.batchGet(req.items);          // 价格、重量、店铺、类目
    Map<Long, Integer> stockMap = inventoryClient.batchCheck(...);   // 只校验，不预占
    List<CouponCode> userCoupons = couponClient.getByCodes(req.couponCodeIds);
    List<PromoActivity> activities = promoClient.match(skus);         // 命中的活动

    // ---------- ② 校验 ----------
    validateSkuStatus(skus);            // 上架、未删除
    validateStock(skus, stockMap);      // 库存充足（不预占）
    validateCouponOwnership(userCoupons, req.userId);

    // ---------- ③ Level 0：单品促销 ----------
    for (SkuInfo sku : skus) {
        ItemPromo p = pickBestItemPromo(sku, activities);   // 同一 SKU 的多个单品活动取最优
        sku.promoPrice = p == null ? sku.unitPrice : p.apply(sku.unitPrice);
        sku.itemDiscount = (sku.unitPrice - sku.promoPrice) * sku.num;
    }

    // ---------- ④ Level 1：店铺级（按店铺分组独立计算）----------
    for (ShopGroup shop : groupByShop(skus)) {
        shopAmount = shop.sumCurrent();
        candidates = shopActivities(shop) + shopCoupons(shop, userCoupons);
        candidates = filterByScope(candidates, shop.items);         // 适用范围过滤
        candidates = filterByThreshold(candidates, shopAmount);     // 门槛过滤
        best = selectByConflictGroup(candidates);                   // 冲突组选最优
        if (best != null) {
            long discount = best.compute(shop.eligibleAmount());     // 计算优惠额（含封顶）
            long[] alloc = allocateWithCap(discount, shop.eligibles(), shop.caps());
            shop.applyAllocation(alloc, best.type());
        }
    }

    // ---------- ⑤ Level 2：平台级 ----------
    platformEligible = sumEligible(allItems, platformScopes);
    candidates = platformActivities + platformCoupons;
    best = selectByConflictGroup(candidates);
    if (best != null) {
        discount = best.compute(platformEligible);
        alloc = allocateWithCap(discount, eligibles, caps);
        applyAllocation(alloc);
    }

    // ---------- ⑥ Level 3：积分 ----------
    if (req.usePoints) {
        long maxByAmount = payableBeforePoint * maxPointRatio / 10000;  // 如最多抵 50%
        long maxByBalance = pointsClient.getBalance(userId) / RATE;      // 100 积分 = 1 元
        long deduction = min(maxByAmount, maxByBalance);
        deduction = floorToStep(deduction, 100);                          // 必须是 100 分的整数倍
        alloc = allocateWithCap(deduction, eligibles, caps);
        applyAllocation(alloc);
    }

    // ---------- ⑦ 运费（见 06-freight）----------
    freight = freightClient.calc(skus, addressId);

    // ---------- ⑧ 汇总 + 签名 ----------
    resp.payableAmount = totalAmount - itemDiscount - shopDiscount
                       - platformDiscount - pointDeduction + freight;
    assert resp.payableAmount >= 0;
    resp.priceToken = priceTokenSigner.sign(req, resp);   // 见 11-price-consistency

    return resp;
}
```

### 7.3 不可用券的原因提示

用户体验的关键：**券用不了要说清为什么**。

```java
enum UnavailableReason {
    EXPIRED            ("已过期"),
    NOT_STARTED        ("未到使用时间"),
    THRESHOLD_NOT_MET  ("还差 ¥%.2f 可用"),        // ★ 最有价值：告诉用户还差多少
    SCOPE_NOT_MATCH    ("仅限指定商品使用"),
    SHOP_NOT_MATCH     ("仅限 XX 店铺商品使用"),
    STACK_CONFLICT     ("与已选优惠互斥"),
    LOCKED_BY_ORDER    ("正在被订单 %s 占用"),
    STOCK_OUT          ("商品已下架或无货")           // 作用域商品全部失效
}
```

`THRESHOLD_NOT_MET` 的"还差多少"提示能显著提升转化（凑单行为）。实现上，在 §4 的 `filterByThreshold` 中计算 `threshold - eligibleAmount` 即可。

## 8. 计算结果的持久化

订单创建时，**把整份计算结果冻结到订单表**：

```sql
-- 母单的金额汇总
CREATE TABLE `order_main` (
  ...
  `total_amount`       BIGINT NOT NULL COMMENT '商品原价总额',
  `item_discount`      BIGINT NOT NULL DEFAULT 0 COMMENT '单品促销优惠',
  `shop_discount`      BIGINT NOT NULL DEFAULT 0 COMMENT '店铺级优惠',
  `platform_discount`  BIGINT NOT NULL DEFAULT 0 COMMENT '平台级优惠',
  `point_deduction`    BIGINT NOT NULL DEFAULT 0 COMMENT '积分抵扣',
  `point_used`         INT    NOT NULL DEFAULT 0 COMMENT '消耗积分数量',
  `freight_amount`     BIGINT NOT NULL DEFAULT 0 COMMENT '运费',
  `discount_amount`    BIGINT GENERATED ALWAYS AS
      (item_discount + shop_discount + platform_discount + point_deduction) STORED,
  `payable_amount`     BIGINT NOT NULL COMMENT '应付 = total - discount + freight',
  ...
);
```

**母单必须有分摊汇总**，因为拆单后子单各自有金额，母单是支付对象（见 [07](07-order-and-split.md)）。

## 9. 优惠快照表：审计与退款依据

```sql
CREATE TABLE `order_discount_snapshot` (
  `id`             BIGINT      NOT NULL AUTO_INCREMENT,
  `order_main_no`  VARCHAR(32) NOT NULL,
  `order_sub_no`   VARCHAR(32) DEFAULT NULL COMMENT '为空表示平台级（未拆到子单）',
  `level`          TINYINT     NOT NULL COMMENT '0单品 1店铺 2平台 3积分',
  `source_type`    VARCHAR(32) NOT NULL COMMENT 'ITEM_PROMO/SHOP_PROMO/COUPON_SHOP/COUPON_PLATFORM/POINT',
  `source_id`      BIGINT      DEFAULT NULL COMMENT '活动ID或券码ID',
  `source_name`    VARCHAR(128) NOT NULL COMMENT '快照名称，如"满200减30"',
  `rule_snapshot`  JSON        NOT NULL COMMENT '★ 规则快照：threshold, discountValue, rate, scope...',
  `discount_amount` BIGINT     NOT NULL COMMENT '本优惠总金额',
  `created_at`     DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_main_level_source` (`order_main_no`, `level`, `source_type`, `source_id`),
  KEY `idx_main` (`order_main_no`),
  KEY `idx_coupon` (`source_type`, `source_id`)
) ENGINE=InnoDB COMMENT='订单优惠快照（审计与退款依据）';
```

**为什么需要 `rule_snapshot`**：退款时按比例退还是按原额退，需要知道当时的规则。运营半年后改了活动规则，历史订单的退款逻辑不能跟着变。JSON 快照存下 `{"threshold":20000,"discountValue":3000,"scope":"SHOP","rate":null}`。

**`uk_main_level_source` 的唯一约束**：防止同一活动的优惠被重复记账（幂等）。

## 10. 测试用例（必须覆盖）

优惠计算的正确性只能靠测试保证。必备用例：

| # | 场景 | 期望 |
|---|---|---|
| 1 | 无任何优惠 | payable = Σ price*num + freight |
| 2 | 单品直降 + 店铺满减 | 两层依次作用，第二个门槛用降价后的金额判断 |
| 3 | 店铺券 + 平台券叠加 | 平台券门槛用"店铺券扣减后的金额"判断 |
| 4 | 店铺活动与店铺券互斥 | 只生效优惠大的那个 |
| 5 | 3 个商品分摊 10 元 | 3.33 + 3.33 + 3.34 = 10.00，且余数最大的行多得 1 分 |
| 6 | 分摊到 1 分钱商品 | 行金额不为负，不足部分溢出到其他行 |
| 7 | 无门槛 20 元券，订单只有 15 元 | payable = 0（不能为负），分摊封顶到行金额 |
| 8 | 折扣券 8.5 折，含封顶 50 元 | 计算超出封顶时按 50 元 |
| 9 | 积分抵扣，账户积分不足 | 按余额抵扣，向下取整到 100 分 |
| 10 | 适用范围只含商品 A，B 不参与 | 门槛只看 A 的金额，优惠只分给 A |
| 11 | 部分商品不在售 | 该行移除，重新计算所有优惠 |
| 12 | 大额：999999.99 元 | 整数运算不溢出，结果正确 |
| 13 | 100 行相同金额分摊 1 元 | 每行 1 分，正好 100 分 |
| 14 | 优惠金额 > 订单金额（异常） | 优惠封顶到订单金额，payable = 0 |
| 15 | 幂等：同一请求算两次 | 结果完全一致（确定性） |

**守恒断言（生产环境也建议开启采样）**：

```java
assert sum(item.discountAmount) == order.discountAmount
    : "分摊不守恒: " + detail;
assert order.payableAmount == order.totalAmount - order.discountAmount + order.freightAmount
    : "金额不平";
assert order.payableAmount >= 0 : "应付金额为负";
```

## 11. 性能设计

| 优化 | 说明 |
|---|---|
| 模板/活动本地缓存 | Caffeine 5 分钟，活动变更通过 MQ 广播失效 |
| 作用域用 Redis Set | 避免每次解析大 JSON（见 [04 §7.1](04-coupon.md)） |
| 计算无 IO | 所有数据预加载，纯内存计算，单次算价 < 5ms |
| 结果缓存 | 相同 (userId, itemHash, couponIds, addressId) 的算价结果缓存 10s（防前端反复调用） |
| 批量接口 | 结算页一次拿齐商品+库存+券+活动，避免 N+1 |

**算价服务不应该有任何远程调用**（在预加载之后）。这是硬性约束——一旦算价里有同步 RPC，P99 就会不可控。

## 12. 与其它模块的边界

| 模块 | 边界 |
|---|---|
| 优惠券服务 | 提供"券的规则"，不提供"券怎么算"。**计算逻辑在促销引擎里**，券服务只管生命周期 |
| 运费服务 | 促销引擎**不计算运费**，只把运费加到总额。运费券是例外：它作用于运费，由运费服务算完后回传给促销引擎抵扣 |
| 积分服务 | 提供余额和兑换比例；**扣减发生在下单时**，由交易服务统一编排 |
| 交易服务 | 调用促销引擎算价 → 冻结快照 → 创建订单 |
