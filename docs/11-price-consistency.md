# 11 价格一致性：前端展示 / 后端裁决

## 1. 问题定义

**需求**：结算页金额由前端计算展示，**支付时后端重新计算并与前端提交的金额对比**。

三个子问题：

| 问题 | 说明 |
|---|---|
| **展示一致性** | 前端算的 358.00，后端算的也是 358.00，不能差 1 分 |
| **时间一致性** | 前端算价（T1）到提交订单（T2）之间，价格/库存/活动可能已变 |
| **防篡改** | 前端提交的金额不能被用户改（改了就少付钱） |

**核心原则**：**后端是唯一裁决者，前端计算只是为了"即时反馈"而非"最终依据"**。

## 2. 为什么前端必须算一遍

有人会问：既然后端要重算，前端为什么还要算？

| 理由 | 说明 |
|---|---|
| **交互响应** | 用户勾选/取消一个商品、换一张券，金额要立刻变。每次都调后端会有 200ms 延迟，体验差 |
| **本地推算** | 优惠券列表、可用性提示、凑单提示都需要本地快速计算 |
| **减少请求** | 用户反复调整购物车选择时，不需要每次请求后端 |

**但要求**：**前端算法与后端算法必须严格一致**。差异会导致"前端显示 355，下单显示 358"这种致命体验问题。

## 3. 一致性方案：单一算法 + 双端实现

### 3.1 方案对比

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 前端只展示后端算的结果 | 绝不不一致 | 每次调整都要请求，体验差 | ❌ |
| B. 前端独立实现算法 | 响应快 | 两套代码必然漂移 | ❌ |
| **C. 用同一份算法（跨端代码生成/WASM/公式配置）** | 一份逻辑 | 工程成本 | ✅ **推荐** |
| D. 前端先本地算（预估）+ 后端返回权威结果覆盖 | 体验好且最终一致 | 可能出现短暂闪变 | ✅ **配合 C 使用** |

### 3.2 实现：算法下沉为"规则 + 解释器"

**核心思路**：把算价逻辑抽象成**数据驱动的规则计算**，前端和后端跑同一份规则。

```
                     ┌──────────────────┐
                     │  促销规则 DSL     │  ← 活动配置生成
                     │  (JSON)          │
                     └────────┬─────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
      ┌───────────────┐              ┌───────────────┐
      │ 后端计算引擎   │              │ 前端计算引擎   │
      │ (Java)        │              │ (TypeScript)  │
      └───────────────┘              └───────────────┘
              │                               │
              └───────────┬───────────────────┘
                          ▼
                 同一份规则，同样的算法
                 同样的整数运算，同样的舍入
                 同样的分摊（最大余数法）
```

**落地方式（成本从低到高）**：

| 方式 | 说明 | 适合 |
|---|---|---|
| 1. 规则数据化（推荐起点） | 优惠规则由后端以 JSON 下发（`{type, threshold, value, rate}`），前端按固定算法解释。**算法代码双端各写一份，但规则只有一份** | 大多数团队 |
| 2. 共享代码库 | 把算法写成 TypeScript，前端直接用；后端用 GraalVM JS 或转译运行 | 有前端工程能力 |
| 3. WASM | 核心算法用 Rust/C 写，编译成 WASM，两端调用 | 追求极致一致 |

**方案 1 的具体做法**：

后端接口返回算价的**"明细解释"**而非仅数字：

```json
{
  "items": [
    { "skuId": 1001, "num": 2, "unitPrice": 799900,
      "promoPrice": 749900, "itemDiscount": 100000,
      "couponAmount": 30000, "pointAmount": 0,
      "payAmount": 1369800 }
  ],
  "shops": [
    { "shopId": 100, "shopDiscount": 30000, "shopDiscountName": "满200减30" }
  ],
  "platform": { "platformDiscount": 5000, "platformDiscountName": "平台9折券" },
  "points": { "used": 0, "deduction": 0 },
  "freight": { "amount": 1600, "detail": [{"warehouse":"WH-1","weight":1400,"first":"1000/1000","add":1,"fee":1600}] },
  "totalAmount": 1599800,
  "discountTotal": 135000,
  "payableAmount": 1484400
}
```

**关键**：**后端返回的是"每个环节的明细"，前端做的是"汇总 + 局部重算"**。

例如用户取消勾选商品 C，前端只需：
1. 从 items 中移除 C
2. 重新汇总（`totalAmount` 重新求和）
3. **优惠部分请求后端重算**（因为优惠是全局依赖的）

**这样前端只在"无优惠变化"的场景本地算，有优惠变化的场景调后端**。既快又准。

## 4. priceToken 设计（防篡改 + 时间一致性）

### 4.1 结构

```
priceToken = base64url(payload) + "." + HMAC_SHA256(payload, SECRET)

payload = {
  "uid":  88001,                          // 用户
  "its": [                                // 商品项（排序后）
    {"s": 1001, "n": 2, "up": 799900, "pa": 1369800},
    {"s": 1002, "n": 1, "up": 899900, "pa": 869900}
  ],
  "itsHash": "a3f8c1...",                 // ★ 商品项的 hash，用于快速比对
  "cp":  [88001_1001, ...],               // 使用的券 ID 列表
  "cpSnap": [{"id":..., "amt":30000}],    // 券的抵扣快照
  "pt":  0,                               // 使用的积分
  "fr":  1600,                            // 运费
  "ta":  1599800,                         // 商品原价总额
  "dt":  135000,                          // 优惠总额
  "pba": 1484400,                         // 应付
  "ad":  "addrId:12345",                  // 地址 ID 的 hash
  "exp": 1759240000000,                   // 过期时间（签发 + 30 分钟）
  "iat": 1759238200000
}
```

### 4.2 签名与验签

```java
public class PriceTokenSigner {
    private final SecretKeySpec key;   // 从 KMS/配置中心获取，不入代码库

    public String sign(CalcContext ctx, CalcPriceResponse resp) {
        String payload = buildPayload(ctx, resp);
        String sig = hmacSha256(payload, key);
        return Base64Url.encode(payload.getBytes(UTF_8)) + "." + sig;
    }

    public PriceToken verify(String token, Long userId) {
        String[] parts = token.split("\\.");
        if (parts.length != 2) throw new BusinessException(INVALID_PRICE_TOKEN);

        String payload = new String(Base64Url.decode(parts[0]), UTF_8);
        String expectedSig = hmacSha256(payload, key);

        // ★ 常量时间比较，防时序攻击
        if (!MessageDigest.isEqual(expectedSig.getBytes(), parts[1].getBytes())) {
            throw new BusinessException(INVALID_PRICE_TOKEN, "价格校验失败");
        }

        PriceToken pt = JSON.parseObject(payload, PriceToken.class);

        if (!pt.getUid().equals(userId)) {
            throw new BusinessException(INVALID_PRICE_TOKEN, "令牌不属于当前用户");
        }
        if (System.currentTimeMillis() > pt.getExp()) {
            throw new BusinessException(PRICE_TOKEN_EXPIRED, "页面已过期，请刷新后重试");
        }
        return pt;
    }
}
```

### 4.3 下单时的完整比对

**这是需求的核心**："支付时后端计算后进行对比"。

```java
@Transactional
public OrderCreateResult createOrder(OrderCreateRequest req) {
    // ========== ① 幂等检查（见 10-idempotency） ==========
    // ...

    // ========== ② 验证 priceToken 签名与有效期 ==========
    PriceToken clientToken = priceTokenSigner.verify(req.getPriceToken(), req.getUserId());

    // ========== ③ 后端重新算价 ==========
    CalcPriceRequest calcReq = buildCalcRequest(req);
    CalcPriceResponse serverCalc = promotionEngine.calc(calcReq);

    // ========== ④ 逐项对比 ==========
    List<PriceDiff> diffs = new ArrayList<>();

    // 4.1 商品项对比（SKU 集合、数量、单价）
    compareItems(clientToken, serverCalc, diffs);

    // 4.2 优惠对比
    if (!Objects.equals(clientToken.getDt(), serverCalc.getDiscountTotal())) {
        diffs.add(PriceDiff.of("优惠金额",
            clientToken.getDiscountTotal(), serverCalc.getDiscountTotal()));
    }

    // 4.3 运费对比
    if (!Objects.equals(clientToken.getFr(), serverCalc.getFreight())) {
        diffs.add(PriceDiff.of("运费",
            clientToken.getFr(), serverCalc.getFreight()));
    }

    // 4.4 积分对比
    if (!Objects.equals(clientToken.getPt(), serverCalc.getPointUsed())) {
        diffs.add(PriceDiff.of("积分抵扣",
            clientToken.getPt(), serverCalc.getPointDeduction()));
    }

    // 4.5 ★ 应付总额对比（最关键）
    if (!Objects.equals(clientToken.getPba(), serverCalc.getPayableAmount())) {
        diffs.add(PriceDiff.of("应付金额",
            clientToken.getPba(), serverCalc.getPayableAmount()));
    }

    // ========== ⑤ 有不一致 → 拒绝下单 ==========
    if (!diffs.isEmpty()) {
        log.warn("价格校验不一致: userId={}, diffs={}", req.getUserId(), diffs);
        throw new PriceChangedException(diffs, serverCalc.getPriceToken());
        // ★ 异常里带上"最新的算价结果 + 新 token"，
        //   前端可以直接展示"商品价格有变动，请确认"并让用户一键继续
    }

    // ========== ⑥ 一致 → 继续下单流程 ==========
    return doCreateOrder(req, serverCalc);
}
```

### 4.4 差异的响应格式

```json
{
  "code": "409001",
  "message": "商品信息发生变化，请确认后重新提交",
  "data": {
    "diffs": [
      { "field": "商品价格", "item": "iPhone 16 Pro 256G",
        "before": 799900, "after": 819900, "diffAmount": 20000 },
      { "field": "运费", "before": 1600, "after": 2000, "diffAmount": 400 },
      { "field": "优惠券", "before": 30000, "after": 0,
        "reason": "券已被其他订单占用" },
      { "field": "应付金额", "before": 1484400, "after": 1578800, "diffAmount": 94400 }
    ],
    "newPriceToken": "eyJ1aWQiOjg4MDAx...",
    "note": "确认后将以新价格下单"
  }
}
```

前端展示：

```
┌─────────────────────────────────────────┐
│  ⚠️ 商品信息有变化                        │
│                                          │
│  iPhone 16 Pro 256G                      │
│  ¥7999.00  →  ¥8199.00   (+¥200.00)     │
│                                          │
│  运费                                     │
│  ¥16.00    →  ¥20.00     (+¥4.00)       │
│                                          │
│  ─────────────────────────────────────   │
│  应付金额                                 │
│  ¥14844.00 →  ¥15788.00  (+¥944.00)     │
│                                          │
│     [ 取消 ]      [ 确认并支付 ]           │
└─────────────────────────────────────────┘
```

**这是价格一致性设计的核心价值**：不是"静默按新价下单"（用户投诉），也不是"直接拒绝"（用户流失），而是**透明地告诉用户变化，让用户决定**。

## 5. 时间窗口内的变化类型

从结算页算价到提交订单，中间可能有几秒到几分钟。可能发生的变化：

| 变化 | 检测方式 | 处理 |
|---|---|---|
| 商品价格调整 | 比对 `unitPrice` | 提示新价格 |
| 商品下架/删除 | 查 SKU 状态 | 移除该行，提示"已下架" |
| 库存不足 | 查库存（不预占） | 减少数量或移除，提示缺货 |
| 券被其他订单占用 | 查券状态 | 移除该券，重新算价 |
| 券过期 | 查有效期 | 移除该券 |
| 活动结束 | 查活动时间 | 移除该优惠 |
| 运费模板变更 | 重算运费 | 提示运费变化 |
| 收货地址变更 | 比对地址 hash | 重算运费 |
| 积分余额变化 | 查积分 | 调整积分抵扣 |
| 会员等级变化 | 查等级 | 调整会员折扣 |

**关键：先"重新算"，再"对比"**。不要试图逐项判断"什么变了"——直接算一遍拿到权威结果，然后对比差异，差异自然反映出变化。

## 6. 金额精度规范（贯穿全系统的硬约束）

### 6.1 存储

```sql
-- ✅ 正确
`price` BIGINT NOT NULL COMMENT '价格（分）'
`amount` BIGINT NOT NULL COMMENT '金额（分）'

-- ❌ 禁止
`price` DECIMAL(10,2) COMMENT '元'     -- 虽然 DECIMAL 精确，但与 Java 的 BigDecimal 交互成本高
`price` FLOAT / DOUBLE                  -- 绝对禁止，精度丢失
```

**统一用分（整数）的理由**：

| 理由 | 说明 |
|---|---|
| 无精度问题 | 整数加减乘（不含除）绝对精确 |
| 运算快 | 比 BigDecimal 快 10 倍以上 |
| 序列化安全 | JSON 传来传去不会有精度问题（Double 会有） |
| 数据库友好 | BIGINT 索引效率高，DECIMAL 有额外开销 |

**注意**：`BIGINT` 分能表示 `9.2 * 10^18` 分 = 9200 万亿元，远超业务需要。但**除法**（折扣、分摊）必须用整数运算规则，见下。

### 6.2 除法的三条规则

```java
// 规则 1：折扣用"乘法 + 整数除法"
//   折扣率用万分比表示：8500 = 85折
long discounted = amount * rate / 10000;              // 向下取整
long discounted = (amount * rate + 5000) / 10000;     // 四舍五入

// 规则 2：分摊用最大余数法保证守恒（见 05-promotion-engine §6）
long[] alloc = allocate(totalDiscount, eligibles);

// 规则 3：比例计算（如积分返还）向下取整 + 最后一次用差额
long partial = total * part / whole;                  // 向下取整
long last    = total - alreadyRefunded;               // 最后一次用差额
```

### 6.3 边界：负数与零

```java
// 所有金额计算后都必须断言
assert amount >= 0 : "金额不能为负: " + amount;

// 应付金额为 0 是合法的（全额用券/积分抵扣）
if (payableAmount == 0) {
    // 直接标记订单为已支付（0 元订单，不需要走支付渠道）
    // ★ 但仍要创建支付单（金额 0），保持流程统一
}
```

**0 元订单的处理**：不调用渠道，直接触发"支付成功"逻辑。但要小心：**必须走与真实支付相同的幂等和状态流转逻辑**，只是跳过渠道调用。

## 7. 前端算价的实现要点（TypeScript）

```typescript
// ★ 所有金额都是 number（JS 的 number 是双精度，安全整数上限 2^53-1）
//    分的最大值 9.2 * 10^18 超出安全范围，但业务金额不会超过 10^15 分（10 万亿元）
//    实际单订单金额 < 10^9 分（1000 万元），完全安全
// ★ 计算中避免用浮点除法产生小数

const EPS = 0;   // 金额必须精确，不设容差

interface ItemCalc {
  skuId: number;
  num: number;
  unitPrice: number;      // 分
  promoPrice: number;     // 分
  itemDiscount: number;   // 分
  itemDiscountBy: string; // 优惠来源描述
}

class PriceCalculator {
  /**
   * 折扣：四舍五入到分
   * 与后端保持一致：(amount * rate + 5000) / 10000 的整数除法
   */
  static applyDiscount(amount: number, rate: number): number {
    return Math.floor((amount * rate + 5000) / 10000);
  }

  /**
   * 最大余数法分摊（★ 必须与后端字节级一致）
   */
  static allocate(total: number, eligibles: number[]): number[] {
    const n = eligibles.length;
    const result = new Array(n).fill(0);
    const totalEligible = eligibles.reduce((a, b) => a + b, 0);
    if (totalEligible <= 0) return result;

    let allocated = 0;
    const remainders: { idx: number; rem: number }[] = [];

    for (let i = 0; i < n; i++) {
      const numerator = total * eligibles[i];
      result[i] = Math.floor(numerator / totalEligible);
      remainders.push({ idx: i, rem: numerator % totalEligible });
      allocated += result[i];
    }

    const gap = total - allocated;
    // ★ 排序规则必须与后端一致：余数降序，平局时索引升序
    remainders.sort((a, b) => b.rem - a.rem || a.idx - b.idx);
    for (let k = 0; k < gap; k++) {
      result[remainders[k].idx] += 1;
    }
    return result;
  }

  /**
   * 带上限的分摊（与后端一致）
   */
  static allocateWithCap(total: number, eligibles: number[], caps: number[]): number[] {
    // ... 与 Java 版本逐行对应
  }
}

/**
 * ★ 浮点陷阱提醒：
 *   JS 的 number * number 在超过 2^53 时丢精度
 *   total * eligible[i] 最大 = 10^9 * 10^9 = 10^18 > 2^53 (9.007 * 10^15)
 *   → 单订单金额超过 900 万元时会丢精度！
 *
 *   解决方案：
 *   1. 用 BigInt 做中间计算（推荐）
 *   2. 或限制单订单金额上限（业务上合理）
 */
```

```typescript
// ✅ 用 BigInt 保证一致性（推荐）
static allocate(total: bigint, eligibles: bigint[]): bigint[] {
  const n = eligibles.length;
  const result = new Array(n).fill(0n);
  const totalEligible = eligibles.reduce((a, b) => a + b, 0n);
  if (totalEligible <= 0n) return result;

  let allocated = 0n;
  const remainders: { idx: number; rem: bigint }[] = [];

  for (let i = 0; i < n; i++) {
    const numerator = total * eligibles[i];        // BigInt 无精度问题
    result[i] = numerator / totalEligible;          // BigInt 除法自动向下取整
    remainders.push({ idx: i, rem: numerator % totalEligible });
    allocated += result[i];
  }

  const gap = Number(total - allocated);            // gap 一定很小（< n）
  remainders.sort((a, b) => (b.rem > a.rem ? 1 : b.rem < a.rem ? -1 : a.idx - b.idx));
  for (let k = 0; k < gap; k++) result[remainders[k].idx] += 1n;

  return result;
}
```

**JS 的 `number` vs `BigInt` 的选择**：

| 方案 | 一致性 | 性能 | 建议 |
|---|---|---|---|
| `number`（限制金额 < 900 万元） | ✅ | 快 | 单订单金额有上限时可接受 |
| `BigInt` | ✅ 绝对 | 慢 2-3 倍 | **推荐**，算价不是性能瓶颈 |

## 8. 一致性测试：双端对齐测试

**这是保证前后端一致的唯一可靠手段**。

```java
// 后端：生成测试用例 + 期望结果，导出为 JSON
@Test
public void exportTestCases() {
    List<TestCase> cases = List.of(
        tc(1, "空优惠", items(100_00, 1), noCoupon(), expect(100_00)),
        tc(2, "满减券", items(100_00, 3), coupon(10_00, 200_00), expect(290_00)),
        tc(3, "3件摊1元", items(1_00, 1, 1_00, 1, 1_00, 1), coupon(1_00, 0), expect(2_00)),
        // ... 100 个用例
    );
    Files.writeString(Path.of("shared/testcases.json"),
        JSON.toJSONString(cases));
}
```

```typescript
// 前端：读取同一份用例，跑自己的算法，比对
describe('PriceCalculator 与后端一致性', () => {
  const cases = JSON.parse(fs.readFileSync('shared/testcases.json', 'utf8'));

  cases.forEach(tc => {
    it(`case ${tc.id}: ${tc.name}`, () => {
      const result = PriceCalculator.calc(tc.input);
      expect(result.payableAmount).toBe(tc.expect.payableAmount);
      // ★ 逐项比对，不只有总额
      expect(result.discountTotal).toBe(tc.expect.discountTotal);
      expect(result.freight).toBe(tc.expect.freight);
      result.items.forEach((item, i) => {
        expect(item.payAmount).toBe(tc.expect.items[i].payAmount);
      });
    });
  });
});
```

**CI 集成**：后端代码变更 → 重新生成 testcases.json → 前端 CI 跑比对测试 → 不一致则构建失败。

## 9. 监控与告警

**线上必须监控"前后端算价不一致"的比例**。

```java
// 在下单时对比，不一致就上报（即使最终一致也要记录首次不一致）
if (!diffs.isEmpty()) {
    metrics.counter("price.mismatch",
        "field", diffs.get(0).getField(),
        "reason", diffs.get(0).getReason()
    ).increment();

    // ★ 区分"正常变化"和"算法 Bug"
    if (diffs.stream().allMatch(d -> d.getReason() == PriceChangeReason.STOCK_OR_STATUS)) {
        // 商品下架/库存变化导致的差异，是正常的
        metrics.counter("price.mismatch.expected").increment();
    } else {
        // 金额算不一致，可能是 Bug
        metrics.counter("price.mismatch.unexpected").increment();
        log.error("★ 算价不一致（疑似Bug）: userId={}, diffs={}", req.getUserId(), diffs);
    }
}
```

| 指标 | 阈值 | 处理 |
|---|---|---|
| `price.mismatch.unexpected` 比例 | > 0.1% | P1 告警，排查算法 |
| `price.mismatch.expected` 比例 | > 5% | 检查是否有活动异常或库存问题 |
| token 验签失败率 | > 0.5% | 检查密钥、时区 |

## 10. 边界场景

| 场景 | 处理 |
|---|---|
| 用户用旧 token 下单（页面停留 1 小时） | token 过期 → 返回 `PRICE_TOKEN_EXPIRED`，前端刷新页面 |
| 用户手改 token | 验签失败 → `INVALID_PRICE_TOKEN` |
| 用户改用另一个账号的 token | `uid` 不匹配 → 拒绝 |
| token 有效但商品已下架 | 重算时该行移除，diff 提示，返回 409 |
| token 有效但库存为 0 | 重算时该行数量调整为 0，diff 提示，返回 409 |
| 前端用错版本（金额单位是元） | 后端对比发现差异巨大（100 倍），拒绝并告警（这是前端 Bug） |
| 0.01 元的差异 | **也拒绝**。不设容差——有容差就永远说不清谁对 |
| 并发下单（同 token 两次） | 幂等键拦住，只有一次成功 |
| 优惠券在两次算价之间被用掉 | 重算时券不可用 → 重新计算优惠 → diff 提示 |
| 积分在两次算价之间被其他订单消耗 | 重算时积分不足 → 调整积分抵扣 → diff 提示 |

**"不设容差"的说明**：有人会提议"差异小于 1 分就忽略"。**绝对不行**——如果算法有系统性偏差（比如每次差 1 分），容差会掩盖它，直到某天累积成巨额差异。**任何差异都是 Bug，必须暴露**。

## 11. 设计总结

```
        ┌─────────────────────────────────────────────────┐
        │  结算页（前端）                                    │
        │  ① 调后端拿算价明细（含 priceToken）               │
        │  ② 用户调整选择 → 本地重算（无优惠变化时）or 调后端  │
        │  ③ 展示明细，让用户所见即所得                       │
        └────────────────────┬────────────────────────────┘
                             │ 提交订单（带 priceToken）
                             ▼
        ┌─────────────────────────────────────────────────┐
        │  下单接口（后端）                                  │
        │  ① 幂等检查（Idempotency-Key）                    │
        │  ② 验签 + 验 token 有效期                         │
        │  ③ 重新算价（同一份算法）                          │
        │  ④ 逐项对比                                       │
        │     ├─ 一致 → 创建订单                             │
        │     └─ 不一致 → 409 + 差异明细 + 新 token          │
        │  ⑤ 前置校验（库存、券、积分）                       │
        └────────────────────┬────────────────────────────┘
                             │
                             ▼
        ┌─────────────────────────────────────────────────┐
        │  支付（后端）                                      │
        │  ① 从订单读金额（不再重算）★                       │
        │  ② 与渠道对账时校验金额一致                         │
        └─────────────────────────────────────────────────┘
```

**"支付时后端计算后进行对比"的实现位置说明**：

严格说，重算与对比发生在**下单时**（因为订单金额在此时冻结）。支付时**不再重算**（订单金额已是事实），而是：
1. 支付单金额 = 订单 `payable_amount`（不重算）；
2. 支付回调时校验"渠道实付金额 == 支付单金额"（见 [09-payment §4.3](09-payment.md)）。

**如果一定要求在支付时重算**（比如为了配合某些渠道的价格校验），则：

```java
// 支付前重算（可选，谨慎使用）
public PrepayResult prepay(String mainOrderNo) {
    OrderMain main = mainMapper.selectByNo(mainOrderNo);
    Payment pay = payMapper.selectByMain(mainOrderNo);

    // 重算（防御性校验：订单金额是否被异常修改）
    CalcPriceResponse recheck = promotionEngine.recalcFromSnapshot(main);
    if (!recheck.getPayableAmount().equals(main.getPayableAmount())) {
        // 从快照重算应该永远等于订单金额（因为用的是同一份快照）
        // 不等 → 说明有严重 Bug
        log.error("★ 订单金额与快照重算不一致: mainNo={}, order={}, recalc={}",
            mainOrderNo, main.getPayableAmount(), recheck.getPayableAmount());
        throw new SystemException("订单金额校验失败，请联系客服");
    }

    return channel.prepay(...);
}
```

**`recalcFromSnapshot`**：用订单里存的快照（`order_item` 的 `_snap` 字段 + `order_discount_snapshot`）重算，而不是查实时数据。这样：
- 不依赖实时商品数据（商品可能已改价）；
- 是一个**自检**：检查订单数据是否自洽（金额字段之间是否满足恒等式）。

这种自检很有价值，建议每次支付前都做（成本 < 1ms）。
