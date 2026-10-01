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
CREATE TABLE promotion.promo_stack_rule (
  id             BIGINT      PRIMARY KEY,
  rule_name      VARCHAR(64) NOT NULL,
  type_a         VARCHAR(32) NOT NULL,          -- 优惠类型A，如 COUPON_PLATFORM
  type_b         VARCHAR(32) NOT NULL,          -- 优惠类型B
  stackable      BOOLEAN     NOT NULL,          -- true可叠加 false互斥
  priority       INT         NOT NULL DEFAULT 0, -- 优先级，用于冲突裁决
  shop_id        BIGINT      NOT NULL DEFAULT 0, -- 0=全局规则
  effective_from TIMESTAMPTZ(3),
  effective_to   TIMESTAMPTZ(3),
  CONSTRAINT uk_stack_rule_pair UNIQUE (type_a, type_b, shop_id)
);
COMMENT ON TABLE promotion.promo_stack_rule IS '优惠叠加规则矩阵';
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

```python
# 每个优惠声明自己属于哪个冲突组，以及组内可用的数量上限
@dataclass(frozen=True, slots=True)
class Discount:
    id: int
    group: str
    group_max_select: int
    amount: int          # 分
    priority: int

# 组定义示例
# group = "PLATFORM_COUPON"       max_select = 1   ← 平台券只能 1 张
# group = "SHOP_COUPON:{shop_id}" max_select = 1   ← 每个店铺的券只能 1 张
# group = "ITEM_PROMO:{sku_id}"   max_select = 1   ← 每个 SKU 只能命中 1 个单品活动
# group = "PLATFORM_PROMO"        max_select = 1   ← 平台活动只能 1 个
# group = "POINT"                 max_select = 1
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

```python
@dataclass(slots=True)
class ItemCalc:
    sku_id: int
    num: int
    unit_price: int              # 原价（分）
    promo_price: int = 0         # Level 0 后的价格
    promo_amount: int = 0        # Level 0 的优惠额 = (原价 - 促销价) * num
    current_amount: int = 0      # 当前剩余金额，每层计算后递减
    shop_eligible: int = 0       # 参与店铺级优惠的金额
    platform_eligible: int = 0   # 参与平台级优惠的金额
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

规则：**折扣向下取整（用户多得 1 分）或四舍五入？** 选择**四舍五入到分**（整数实现，见下），理由：
- 向下取整在部分退款时容易出现"退的比付的多"（因为多次向下取整的累积误差）；
- 四舍五入在数学期望上中立，配合分摊算法能保证守恒。

**所有金额运算的绝对规则**：

```python
# ✅ 正确：全整数运算（Python int 无溢出）
discounted = amount * rate // 10000                 # rate = 8500 表示 85 折，向下取整
rounded    = (amount * rate + 5000) // 10000        # 四舍五入（amount、rate 均为非负）

# ❌ 错误：浮点
d = amount * 0.85            # 0.1 + 0.2 != 0.3 的经典问题
# ❌ 错误：内置 round() 是"银行家舍入"，round(2.5) == 2
r = round(amount * rate / 10000)
```

> Python 的 `//` 对负数是向负无穷取整（`-7 // 2 == -4`），金额计算的被除数必须保证非负；需要处理负数时显式用 `Decimal.quantize(..., ROUND_HALF_UP)`。Pydantic 模型中金额字段声明为 `int`（`Field(ge=0)`），前端传入小数会直接校验失败。

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

```python
def allocate(total_discount: int, eligible: list[int]) -> list[int]:
    """最大余数法分摊。

    :param total_discount: 总优惠（分），>= 0
    :param eligible: 每个分摊对象的基数（分），>= 0，顺序即平局时的裁决顺序
    :return: 每个对象分到的金额（分），和恰好为 total_discount
    """
    n = len(eligible)
    total_eligible = sum(eligible)
    if total_eligible <= 0:
        return [0] * n                      # 无可分摊基数，全为 0

    # ① 整数部分与余数（全整数，无浮点）
    result: list[int] = []
    remainders: list[int] = []
    for e in eligible:
        q, r = divmod(total_discount * e, total_eligible)
        result.append(q)
        remainders.append(r)

    # ② 补足缺口：按余数降序，平局按下标升序（确定性）
    gap = total_discount - sum(result)      # 0 <= gap < n
    order = sorted(range(n), key=lambda i: (-remainders[i], i))
    for i in order[:gap]:
        result[i] += 1
    return result
```

**溢出**：Python 的 `int` 是任意精度，不存在 Java `long` 的乘法溢出问题。但 PG 的 `BIGINT` 上限约 `9.2 × 10^18` 分，所以仍需在下单校验中**限制单订单金额上限**（业务上单订单不超过 1000 万元），防止写库时溢出。

**为什么不用 `Decimal`**：`Decimal` + `ROUND_DOWN` + 同样的余数排序逻辑结果等价，但整数实现更快、更不容易写错舍入模式。`Decimal` 只用在需要表达比例的地方（如积分兑换比例），并且在进入分摊前就转换成整数分。

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

```python
def allocate_with_cap(total: int, eligible: list[int], cap: list[int]) -> list[int]:
    """带行上限的最大余数法分摊。

    结果满足：sum(result) == min(total, sum(cap))，且 0 <= result[i] <= cap[i]。
    """
    n = len(eligible)
    result = [0] * n
    remaining = min(total, sum(cap))         # 优惠封顶到可分摊总额
    done = [cap[i] <= 0 for i in range(n)]   # 已达上限（或无上限空间）的行

    while remaining > 0:
        active = [i for i in range(n) if not done[i]]
        if not active:
            break                            # 全部达上限
        active_eligible = sum(eligible[i] for i in active)
        if active_eligible <= 0:
            # 剩余行基数都为 0：按行顺序依次填满，保证守恒
            for i in active:
                take = min(cap[i] - result[i], remaining)
                result[i] += take
                remaining -= take
            break

        # 本轮按比例分摊 remaining（复用 allocate，保证本轮守恒）
        this_round = allocate(remaining, [eligible[i] for i in active])

        # 应用并检查上限，超出部分进入下一轮
        overflow = 0
        for i, amount in zip(active, this_round):
            space = cap[i] - result[i]
            if amount >= space:
                overflow += amount - space
                result[i] = cap[i]
                done[i] = True
            else:
                result[i] += amount
        remaining = overflow
    return result
```

> 循环一定终止：每一轮要么 `overflow == 0`（结束），要么至少有一行被标记为 `done`（活跃行数严格递减）。

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

```python
# app/modules/promotion/schemas.py —— 结算页 & 下单共用同一个算价函数
class CalcItem(BaseModel):
    sku_id: int
    num: int = Field(ge=1, le=200)


class CalcPriceRequest(BaseModel):
    items: list[CalcItem] = Field(min_length=1, max_length=100)
    coupon_code_ids: list[int] = []      # 用户选中的券
    use_points: bool = False
    address_id: int                      # 用于运费计算
    # user_id 不从请求体取，由鉴权依赖注入


class CalcPriceResponse(BaseModel):
    # ★ 每个字段都是"可以给前端看的"，同时后端也会用同一份结果下单
    items: list[ItemResult]              # 每行的原价、促销价、分摊优惠、实付
    shops: list[ShopResult]              # 按店铺分组
    total_amount: int                    # 商品总额（原价）
    item_discount: int                   # 单品促销优惠
    shop_discount: int                   # 店铺级优惠
    platform_discount: int               # 平台级优惠
    point_deduction: int                 # 积分抵扣
    freight: int                         # 运费
    payable_amount: int                  # 实付 = total_amount - 所有优惠 + 运费
    unavailable_coupons: list[UnavailableCoupon]   # 不可用券 + 原因
    price_token: str                     # ★ 签名令牌，下单时必须带回
    expire_at: int                       # token 过期时间（Unix 秒）
```

> 接口 JSON 使用 camelCase（`totalAmount`），Python 内部使用 snake_case。在公共基类上配置 `model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)` 统一转换，ID 类字段序列化为字符串（见 [15](15-api-and-errors.md) §1）。

### 7.2 主流程伪代码

算价分成两段：**异步加载**（查 DB/Redis）和**纯函数计算**（无 IO，便于单测与复现）。

```python
# app/modules/promotion/service.py
async def calc_price(session: AsyncSession, user_id: int, req: CalcPriceRequest) -> CalcPriceResponse:
    # ---------- ① 加载数据（并发执行，全部是同进程模块函数）----------
    skus, stock_map, user_coupons, activities, address = await asyncio.gather(
        product_service.batch_get_skus(session, [i.sku_id for i in req.items]),  # 价格、重量、店铺、类目
        inventory_service.batch_check(req.items),                               # 只读 Redis，不预占
        coupon_repo.get_user_coupons(session, user_id, req.coupon_code_ids),
        activity_cache.match(req.items),                                        # 命中的活动（进程内缓存）
        user_service.get_address(session, user_id, req.address_id),
    )
    points_balance = await points_service.get_balance(session, user_id) if req.use_points else 0
```

> 注意：同一个 `AsyncSession` **不能被并发协程同时使用**。上面 `gather` 中涉及 DB 的函数要么各自从会话工厂取独立会话（只读查询），要么改为顺序 `await`。示例为表达"批量预加载"的意图，实际实现时 DB 查询顺序执行、Redis 查询并发执行。

```python
    # ---------- ② 校验 ----------
    validate_sku_status(skus)                 # 上架、未删除
    validate_stock(skus, stock_map)           # 库存充足（不预占）
    validate_coupon_ownership(user_coupons, user_id)

    # ---------- ③~⑥ 纯内存计算（无 IO）----------
    calc = PriceCalculator(skus, req.items, activities, user_coupons, points_balance, stack_rules)
    result = calc.run()                       # 见下

    # ---------- ⑦ 运费（见 06-freight）----------
    freight = await freight_service.calc(session, skus, address)

    # ---------- ⑧ 汇总 + 签名 ----------
    payable = (result.total_amount - result.item_discount - result.shop_discount
               - result.platform_discount - result.point_deduction + freight.total)
    if payable < 0:
        raise PriceInvariantError("应付金额为负")
    token = price_token_signer.sign(user_id, req, result, freight)   # 见 11-price-consistency
    return build_response(result, freight, payable, token)


class PriceCalculator:
    def run(self) -> CalcResult:
        # Level 0：单品促销（同一 SKU 的多个单品活动取最优）
        for item in self.items:
            promo = pick_best_item_promo(item, self.activities)
            item.promo_price = promo.apply(item.unit_price) if promo else item.unit_price
            item.promo_amount = (item.unit_price - item.promo_price) * item.num
            item.current_amount = item.promo_price * item.num

        # Level 1：店铺级（按店铺分组独立计算）
        for shop in group_by_shop(self.items):
            candidates = shop_activities(shop) + shop_coupons(shop, self.coupons)
            candidates = filter_by_scope(candidates, shop.items)          # 适用范围过滤
            candidates = filter_by_threshold(candidates, shop.eligible_amount())  # 门槛过滤
            best = select_by_conflict_group(candidates, self.stack_rules)  # 冲突组选最优
            if best:
                discount = best.compute(shop.eligible_amount())            # 含封顶
                alloc = allocate_with_cap(discount, shop.eligibles(), shop.caps())
                shop.apply_allocation(alloc, best.type)

        # Level 2：平台级（同 Level 1，eligible 只算平台券 scope 内的商品）
        self.apply_platform_level()

        # Level 3：积分（100 积分 = 1 元 = 100 分，最多抵 max_point_ratio）
        if self.points_balance > 0:
            payable_before_point = sum(i.current_amount for i in self.items)
            max_by_amount = payable_before_point * self.max_point_ratio // 10000   # 如 5000 = 50%
            max_by_balance = self.points_balance // POINTS_PER_YUAN * 100
            deduction = min(max_by_amount, max_by_balance) // 100 * 100           # 取整到元
            alloc = allocate_with_cap(deduction, self.eligibles(), self.caps())
            self.apply_allocation(alloc, DiscountType.POINT)

        return self.summarize()
```

### 7.3 不可用券的原因提示

用户体验的关键：**券用不了要说清为什么**。

```python
class UnavailableReason(StrEnum):
    EXPIRED           = "已过期"
    NOT_STARTED       = "未到使用时间"
    THRESHOLD_NOT_MET = "还差 ¥{gap} 可用"          # ★ 最有价值：告诉用户还差多少
    SCOPE_NOT_MATCH   = "仅限指定商品使用"
    SHOP_NOT_MATCH    = "仅限 {shop_name} 店铺商品使用"
    STACK_CONFLICT    = "与已选优惠互斥"
    LOCKED_BY_ORDER   = "正在被订单 {order_no} 占用"
    STOCK_OUT         = "商品已下架或无货"            # 作用域商品全部失效


def fen_to_yuan(fen: int) -> str:
    """分 → 展示用元字符串，只用于文案，不参与计算。"""
    return f"{fen // 100}.{fen % 100:02d}"
```

`THRESHOLD_NOT_MET` 的"还差多少"提示能显著提升转化（凑单行为）。实现上，在 §7.2 的 `filter_by_threshold` 中计算 `threshold - eligible_amount` 即可。

## 8. 计算结果的持久化

订单创建时，**把整份计算结果冻结到订单表**：

```sql
-- 母单的金额汇总（完整 DDL 见 07-order-and-split）
CREATE TABLE trade.order_main (
  ...
  total_amount       BIGINT NOT NULL,              -- 商品原价总额
  item_discount      BIGINT NOT NULL DEFAULT 0,    -- 单品促销优惠
  shop_discount      BIGINT NOT NULL DEFAULT 0,    -- 店铺级优惠
  platform_discount  BIGINT NOT NULL DEFAULT 0,    -- 平台级优惠
  point_deduction    BIGINT NOT NULL DEFAULT 0,    -- 积分抵扣
  point_used         INT    NOT NULL DEFAULT 0,    -- 消耗积分数量
  freight_amount     BIGINT NOT NULL DEFAULT 0,    -- 运费
  discount_amount    BIGINT GENERATED ALWAYS AS
      (item_discount + shop_discount + platform_discount + point_deduction) STORED,
  payable_amount     BIGINT NOT NULL,              -- 应付 = total - discount + freight
  ...
  -- ★ 金额守恒由数据库兜底
  CONSTRAINT ck_main_payable CHECK (
    payable_amount = total_amount - item_discount - shop_discount
                   - platform_discount - point_deduction + freight_amount
    AND payable_amount >= 0)
);
```

**母单必须有分摊汇总**，因为拆单后子单各自有金额，母单是支付对象（见 [07](07-order-and-split.md)）。

## 9. 优惠快照表：审计与退款依据

```sql
CREATE TABLE trade.order_discount_snapshot (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_main_no   VARCHAR(32)  NOT NULL,
  order_sub_no    VARCHAR(32),                  -- 为空表示平台级（未拆到子单）
  level           SMALLINT     NOT NULL,        -- 0单品 1店铺 2平台 3积分
  source_type     VARCHAR(32)  NOT NULL,        -- ITEM_PROMO/SHOP_PROMO/COUPON_SHOP/COUPON_PLATFORM/POINT
  source_id       BIGINT       NOT NULL DEFAULT 0, -- 活动ID或券码ID，积分为 0
  source_name     VARCHAR(128) NOT NULL,        -- 快照名称，如"满200减30"
  rule_snapshot   JSONB        NOT NULL,        -- ★ 规则快照：threshold, discountValue, rate, scope...
  discount_amount BIGINT       NOT NULL,        -- 本优惠总金额
  created_at      TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT uk_discount_snap UNIQUE (order_main_no, level, source_type, source_id)
);
CREATE INDEX idx_discount_snap_source ON trade.order_discount_snapshot (source_type, source_id);
COMMENT ON TABLE trade.order_discount_snapshot IS '订单优惠快照（审计与退款依据）';
```

**为什么需要 `rule_snapshot`**：退款时按比例退还是按原额退，需要知道当时的规则。运营半年后改了活动规则，历史订单的退款逻辑不能跟着变。JSONB 快照存下 `{"threshold":20000,"discountValue":3000,"scope":"SHOP","rate":null}`。

**`uk_discount_snap` 的唯一约束**：防止同一活动的优惠被重复记账（幂等）。`source_id` 用 `NOT NULL DEFAULT 0` 而不是可空——PG 的唯一约束中 `NULL` 互不相等，可空列会让积分类快照绕过唯一性。唯一约束的首列 `order_main_no` 同时覆盖了按母单查询，不需要再单独建索引。

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

测试使用 `pytest`，分摊函数额外用 `hypothesis` 做性质测试（随机生成金额与行数，断言守恒/非负/不超上限三条约束恒成立）。

**守恒检查（生产环境始终开启）**：

Python 的 `assert` 在 `python -O` 下会被移除，**不能用来做生产校验**。改为显式检查并抛业务异常：

```python
def check_invariants(order: OrderDraft) -> None:
    if sum(i.discount_amount for i in order.items) != order.discount_amount:
        raise PriceInvariantError(f"分摊不守恒: {order.debug_detail()}")
    if order.payable_amount != order.total_amount - order.discount_amount + order.freight_amount:
        raise PriceInvariantError("金额不平")
    if order.payable_amount < 0:
        raise PriceInvariantError("应付金额为负")
```

表上的 `CHECK` 约束（§8、[02](02-domain-model.md) §5）是最后一道防线。

## 11. 性能设计

| 优化 | 说明 |
|---|---|
| 模板/活动进程内缓存 | `cachetools.TTLCache` 5 分钟，活动变更时发布 `promotion.changed` 事件，各进程收到后清缓存 |
| 作用域用 Redis Set | 避免每次解析大 JSON（见 [04 §7.1](04-coupon.md)） |
| 计算无 IO | 所有数据预加载，`PriceCalculator` 纯内存计算，单次算价 < 5ms |
| 结果缓存 | 相同 (userId, itemHash, couponIds, addressId) 的算价结果缓存 10s（防前端反复调用） |
| 批量查询 | 结算页一次拿齐商品+库存+券+活动（`WHERE id = ANY(:ids)`），避免 N+1 |

**`PriceCalculator.run()` 内不允许有任何 `await`**。这是硬性约束——算价逻辑一旦夹杂 IO，P99 就不可控，也无法用固定输入做确定性单测。

## 12. 与其它模块的边界

| 模块 | 边界 |
|---|---|
| 优惠券（promotion.coupon） | 提供"券的规则"，不提供"券怎么算"。**计算逻辑在促销引擎里**，券只管生命周期 |
| freight 模块 | 促销引擎**不计算运费**，只把运费加到总额。运费券是例外：它作用于运费，由 freight 模块算完后回传给促销引擎抵扣 |
| user 模块（积分） | 提供余额和兑换比例；**扣减发生在下单事务内**，由 trade 模块统一编排 |
| trade 模块 | 调用促销引擎算价 → 冻结快照 → 创建订单 |
