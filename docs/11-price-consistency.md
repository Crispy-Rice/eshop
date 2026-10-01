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
| **交互响应** | 用户勾选/取消一个商品、换一张券，金额要立刻变。每次都调后端会有明显延迟 |
| **本地推算** | 优惠券列表、可用性提示、凑单提示都需要本地快速计算 |
| **减少请求** | 用户反复调整购物车选择时，不需要每次请求后端 |

**但要求**：**前端算法与后端算法必须严格一致**。差异会导致"前端显示 355，下单显示 358"这种致命体验问题。

## 3. 一致性方案：单一规则 + 双端实现

### 3.1 方案对比

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 前端只展示后端算的结果 | 绝不不一致 | 每次调整都要请求，体验差 | ❌ |
| B. 前端独立实现全部算法 | 响应快 | 两套代码必然漂移 | ❌ |
| **C. 规则数据化 + 双端实现同一组纯函数 + 共享测试用例** | 一份规则，算法有自动化对齐 | 双端各写一份基础函数 | ✅ **采用** |
| D. 前端先本地算（预估）+ 后端返回权威结果覆盖 | 体验好且最终一致 | 可能出现短暂闪变 | ✅ **配合 C 使用** |

### 3.2 实现：规则由后端下发，前端只做"汇总 + 局部重算"

```
                     ┌──────────────────┐
                     │  促销规则         │  ← 活动配置，后端以 JSON 下发
                     │  (JSON)          │
                     └────────┬─────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
      ┌───────────────┐              ┌───────────────┐
      │ 后端计算引擎   │              │ 前端计算函数   │
      │ (Python)      │              │ (TypeScript)  │
      └───────────────┘              └───────────────┘
              │                               │
              └───────────┬───────────────────┘
                          ▼
                 同一份规则，同样的整数运算
                 同样的舍入，同样的最大余数法分摊
                 同一份 testcases.json 验证（§8）
```

前后端技术栈不同（Python / TypeScript），没有低成本的"一份代码两端运行"方案，所以采用：**规则只有一份（后端下发）+ 基础算法双端各写一份（取整、分摊、门槛判断）+ 共享测试用例强制对齐**。前端只实现会在本地重算的部分，复杂的跨店叠加仍以后端结果为准。

后端接口返回算价的**"明细解释"**而非仅数字：

```json
{
  "items": [
    { "skuId": "1001", "num": 2, "unitPrice": 799900,
      "promoPrice": 749900, "itemDiscount": 100000,
      "couponAmount": 30000, "pointAmount": 0,
      "payAmount": 1369800 }
  ],
  "shops": [
    { "shopId": "100", "shopDiscount": 30000, "shopDiscountName": "满200减30" }
  ],
  "platform": { "platformDiscount": 5000, "platformDiscountName": "平台9折券" },
  "points": { "used": 0, "deduction": 0 },
  "freight": { "amount": 1600, "detail": [{"warehouse":"WH-1","weightG":1400,"fee":1600}] },
  "totalAmount": 1599800,
  "discountTotal": 135000,
  "payableAmount": 1484400,
  "priceToken": "eyJ1aWQiOi..."
}
```

ID 类字段以字符串返回（雪花 ID 超过 JS 安全整数范围），金额以整数分返回（见 §7）。

**关键**：**后端返回的是"每个环节的明细"，前端做的是"汇总 + 局部重算"**。

例如用户取消勾选商品 C，前端只需：
1. 从 items 中移除 C
2. 重新汇总（`totalAmount` 重新求和）
3. **优惠部分请求后端重算**（因为优惠是全局依赖的），请求做 300ms 防抖

**这样前端只在"无优惠变化"的场景本地算，有优惠变化的场景调后端**。既快又准。

## 4. priceToken 设计（防篡改 + 时间一致性）

### 4.1 结构

```
priceToken = base64url(payload_json) + "." + base64url(HMAC_SHA256(payload_json, SECRET))

payload = {
  "uid":  "88001",                        // 用户
  "its": [                                // 商品项（按 skuId 排序）
    {"s": "1001", "n": 2, "up": 799900, "pa": 1369800},
    {"s": "1002", "n": 1, "up": 899900, "pa": 869900}
  ],
  "cp":  ["90001"],                       // 使用的券 ID 列表
  "cpSnap": [{"id": "90001", "amt": 30000}], // 券的抵扣快照
  "pt":  0,                               // 使用的积分
  "fr":  1600,                            // 运费
  "ta":  1599800,                         // 商品原价总额
  "dt":  135000,                          // 优惠总额
  "pba": 1484400,                         // 应付
  "ad":  "12345",                         // 收货地址 ID
  "exp": 1759240000,                      // 过期时间（签发 + 30 分钟，Unix 秒）
  "iat": 1759238200
}
```

### 4.2 签名与验签

```python
# app/modules/trade/price_token.py
class PriceTokenSigner:
    def __init__(self, secret: str) -> None:
        self._key = secret.encode()          # 来自环境变量 PRICE_TOKEN_SECRET，不入代码库

    def sign(self, payload: PriceTokenPayload) -> str:
        body = payload.model_dump_json(by_alias=True).encode()
        sig = hmac.new(self._key, body, hashlib.sha256).digest()
        return f"{_b64e(body)}.{_b64e(sig)}"

    def verify(self, token: str, user_id: int) -> PriceTokenPayload:
        try:
            body_b64, sig_b64 = token.split(".")
            body, sig = _b64d(body_b64), _b64d(sig_b64)
        except ValueError:
            raise BizError(ErrorCode.INVALID_PRICE_TOKEN) from None

        expected = hmac.new(self._key, body, hashlib.sha256).digest()
        # ★ 常量时间比较，防时序攻击
        if not hmac.compare_digest(expected, sig):
            raise BizError(ErrorCode.INVALID_PRICE_TOKEN, "价格校验失败")

        payload = PriceTokenPayload.model_validate_json(body)
        if payload.uid != user_id:
            raise BizError(ErrorCode.INVALID_PRICE_TOKEN, "令牌不属于当前用户")
        if time.time() > payload.exp:
            raise BizError(ErrorCode.PRICE_TOKEN_EXPIRED, "页面已过期，请刷新后重试")
        return payload


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
```

验签只验证"这份数据是我们签发的、属于这个用户、没过期"，**签名本身不代表价格仍然有效**——有效性由 §4.3 的重算对比决定。

### 4.3 下单时的完整比对

**这是需求的核心**："支付时后端计算后进行对比"（实现位置的说明见 §11）。

```python
async def create_order(session: AsyncSession, user_id: int, req: CreateOrderRequest,
                       request_id: str) -> OrderCreated:
    # ========== ① 幂等检查：由路由上的 Idempotency 依赖完成（见 10-idempotency）==========

    # ========== ② 验证 priceToken 签名与有效期 ==========
    client = price_token_signer.verify(req.price_token, user_id)

    # ========== ③ 后端重新算价（与结算页同一个函数）==========
    server = await promotion_service.calc_price(session, user_id, req.to_calc_request())

    # ========== ④ 逐项对比 ==========
    diffs: list[PriceDiff] = []
    compare_items(client, server, diffs)                 # 4.1 SKU 集合、数量、单价、行实付
    if client.dt != server.discount_total:               # 4.2 优惠
        diffs.append(PriceDiff.of("优惠金额", client.dt, server.discount_total))
    if client.fr != server.freight:                      # 4.3 运费
        diffs.append(PriceDiff.of("运费", client.fr, server.freight))
    if client.pt != server.point_used:                   # 4.4 积分（比较的是积分数量）
        diffs.append(PriceDiff.of("积分抵扣", client.pt, server.point_used))
    if client.pba != server.payable_amount:              # 4.5 ★ 应付总额（最关键）
        diffs.append(PriceDiff.of("应付金额", client.pba, server.payable_amount))

    # ========== ⑤ 有不一致 → 拒绝下单 ==========
    if diffs:
        logger.warning("价格校验不一致", user_id=user_id, diffs=diffs)
        price_mismatch_counter.labels(kind=classify(diffs)).inc()      # 见 §9
        # ★ 异常里带上"最新的算价结果 + 新 token"，
        #   前端可以直接展示"商品价格有变动，请确认"并让用户一键继续
        raise PriceChangedError(diffs, new_price_token=server.price_token)

    # ========== ⑥ 一致 → 继续下单流程（同一事务内落单、预占、锁券、冻结积分）==========
    return await do_create_order(session, user_id, req, server, request_id)
```

> 注意 `PriceChangedError` 是 `BizError` 的子类，幂等依赖捕获到它会删除幂等键（[10 §3.2](10-idempotency.md)），用户点"确认并支付"时用新 token 重新提交即可。前端同时要调用 `resetKey()`，因为提交内容（token）已经变了。

### 4.4 差异的响应格式

```json
{
  "code": "PRICE_CHANGED",
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
    "newPriceToken": "eyJ1aWQiOi...",
    "note": "确认后将以新价格下单"
  }
}
```

HTTP 状态码为 `409`，错误码体系见 [15](15-api-and-errors.md)。

前端展示（`el-dialog`，标题与金额变化用文字说明，不只靠红绿颜色区分涨跌）：

```
┌─────────────────────────────────────────┐
│  ⚠️ 商品信息有变化                        │
│                                          │
│  iPhone 16 Pro 256G                      │
│  ¥7999.00  →  ¥8199.00   (上涨 ¥200.00) │
│                                          │
│  运费                                     │
│  ¥16.00    →  ¥20.00     (上涨 ¥4.00)   │
│                                          │
│  ─────────────────────────────────────   │
│  应付金额                                 │
│  ¥14844.00 →  ¥15788.00  (上涨 ¥944.00) │
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
| 收货地址变更 | 比对地址 ID | 重算运费 |
| 积分余额变化 | 查积分 | 调整积分抵扣 |
| 会员等级变化 | 查等级 | 调整会员折扣 |

**关键：先"重新算"，再"对比"**。不要试图逐项判断"什么变了"——直接算一遍拿到权威结果，然后对比差异，差异自然反映出变化。

## 6. 金额精度规范（贯穿全系统的硬约束）

### 6.1 存储

```sql
-- ✅ 正确
price   BIGINT NOT NULL,   -- 价格（分）
amount  BIGINT NOT NULL,   -- 金额（分）

-- ❌ 禁止
price NUMERIC(10,2)        -- 元。精确，但与"全链路整数分"的约定冲突，两种单位混用必出 Bug
price REAL / DOUBLE PRECISION  -- 绝对禁止，精度丢失
price MONEY                -- PG 的 money 类型依赖 lc_monetary 区域设置，禁止使用
```

**统一用分（整数）的理由**：

| 理由 | 说明 |
|---|---|
| 无精度问题 | 整数加减乘（不含除）绝对精确 |
| 运算快 | 比 `Decimal` 快得多，写法也不容易出错 |
| 序列化安全 | JSON 传整数分，前端不会出现 `0.1 + 0.2` 问题 |
| 一致 | DB（`BIGINT`）、Python（`int`）、前端（`number`/`bigint`）用同一个单位 |

**注意**：`BIGINT` 分能表示约 `9.2 × 10^18` 分，远超业务需要。但**除法**（折扣、分摊）必须用整数运算规则，见下。

### 6.2 除法的三条规则

```python
# 规则 1：折扣用"乘法 + 整数除法"
#   折扣率用万分比表示：8500 = 85 折
discounted = amount * rate // 10000                # 向下取整
discounted = (amount * rate + 5000) // 10000       # 四舍五入（amount、rate 非负）

# 规则 2：分摊用最大余数法保证守恒（见 05-promotion-engine §6）
alloc = allocate(total_discount, eligibles)

# 规则 3：比例计算（如积分返还）向下取整 + 最后一次用差额
partial = total * part // whole                    # 向下取整
last = total - already_refunded                    # 最后一次用差额
```

**Python 专属陷阱**：

| 写法 | 问题 |
|---|---|
| `amount / 100` | `/` 永远返回 `float`，哪怕能整除 |
| `round(x)` | 银行家舍入，`round(0.5) == 0`、`round(2.5) == 2` |
| `int(x)` | 向零截断，对负数与 `//` 结果不同 |
| `Decimal(0.1)` | 用 `float` 构造会带入二进制误差，必须写 `Decimal("0.1")` |
| Pydantic 字段 `amount: float` | 前端传 `12.5` 会被接受；金额字段一律声明为 `int` 并加 `Field(ge=0)`，并开启 `strict` 拒绝字符串/浮点隐式转换 |

### 6.3 边界：负数与零

```python
# 所有金额计算后都必须检查（显式抛异常，不用 assert，python -O 会移除 assert）
if amount < 0:
    raise PriceInvariantError(f"金额不能为负: {amount}")

# 应付金额为 0 是合法的（全额用券/积分抵扣）
if payable_amount == 0:
    # 直接走"支付成功"逻辑（0 元订单，不需要走支付渠道）
    # ★ 但 payment.amount 有 CHECK (amount > 0)，所以 0 元订单不建支付单，
    #   改为在下单事务内直接调用与支付成功相同的处理函数（子单推进、库存实扣、积分实扣、用券）
    ...
```

**0 元订单的处理**：不调用渠道，直接触发"支付成功"逻辑。但要小心：**必须走与真实支付相同的状态流转逻辑**（[09 §4.3](09-payment.md) 中 `apply_pay_result` 的 ⑤~⑨ 抽成独立函数 `on_order_paid`，两处复用），只是跳过支付单与渠道。

## 7. 前端算价的实现要点（TypeScript）

```ts
// web-mall/src/utils/price.ts
//
// ★ 金额统一用整数分。中间乘法用 BigInt，避免超过 2^53 后丢精度：
//   total * eligible[i] 在单订单 1000 万元（10^9 分）时可达 10^18 > 2^53 (≈ 9.007 × 10^15)
// ★ 函数必须与后端 app/modules/promotion/allocation.py 逐行对应，由共享测试用例对齐（§8）

/** 折扣：四舍五入到分，对应后端 (amount * rate + 5000) // 10000 */
export function applyDiscount(amount: number, rate: number): number {
  return Number((BigInt(amount) * BigInt(rate) + 5000n) / 10000n)
}

/** 最大余数法分摊：余数降序，平局按下标升序（★ 必须与后端一致） */
export function allocate(total: number, eligibles: number[]): number[] {
  const n = eligibles.length
  const totalEligible = eligibles.reduce((a, b) => a + BigInt(b), 0n)
  if (totalEligible <= 0n) return new Array(n).fill(0)

  const T = BigInt(total)
  const result: bigint[] = []
  const rems: { idx: number; rem: bigint }[] = []
  eligibles.forEach((e, idx) => {
    const numerator = T * BigInt(e)
    result.push(numerator / totalEligible)                 // BigInt 除法向零取整（非负即向下取整）
    rems.push({ idx, rem: numerator % totalEligible })
  })

  const gap = Number(T - result.reduce((a, b) => a + b, 0n))   // 0 <= gap < n
  rems.sort((a, b) => (a.rem === b.rem ? a.idx - b.idx : a.rem > b.rem ? -1 : 1))
  for (let k = 0; k < gap; k++) result[rems[k].idx] += 1n
  return result.map(Number)
}

/** 带上限的分摊，对应后端 allocate_with_cap（逐行对应，此处略） */
export function allocateWithCap(total: number, eligibles: number[], caps: number[]): number[] {
  /* ... */
}

/** 展示用：分 → "12.34"，不参与计算 */
export function formatYuan(fen: number): string {
  const sign = fen < 0 ? '-' : ''
  const abs = Math.abs(fen)
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`
}
```

**为什么入参/出参仍用 `number`**：业务金额（单订单 < 10^9 分）远小于 `Number.MAX_SAFE_INTEGER`，JSON 传输与 Vue 模板渲染都用 `number` 最方便；只有**中间乘积**可能越界，所以只在函数内部转 `BigInt`。`tsconfig.json` 的 `target` 需 ≥ `ES2020` 才支持 `BigInt` 字面量（`5000n`），Vite 默认目标满足。

**禁止在前端用 `toFixed` 参与计算**：`(1.005).toFixed(2) === "1.00"`。`toFixed` 只能出现在纯展示且输入已经是整数分换算的场景，推荐统一用上面的 `formatYuan`。

## 8. 一致性测试：双端对齐测试

**这是保证前后端一致的唯一可靠手段**。用例文件放在仓库根目录 `shared/price-testcases.json`，前后端共同读取。

```python
# backend/tests/price/test_export_cases.py
# 后端：维护用例与期望结果；运行 pytest 时校验后端算法，并（加 --export 时）重新导出 JSON
CASES = [
    case(1, "空优惠", items((10000, 1)), coupons=[], expect_payable=10000),
    case(2, "满减券", items((10000, 3)), coupons=[full_reduce(1000, 20000)], expect_payable=29000),
    case(3, "3件摊1元", items((100, 1), (100, 1), (100, 1)), coupons=[no_threshold(100)],
         expect_payable=200, expect_item_discounts=[34, 33, 33]),
    # ... 覆盖 05 §10 的全部场景，约 100 个
]

@pytest.mark.parametrize("c", CASES, ids=lambda c: f"{c.id}-{c.name}")
def test_backend_matches_expectation(c):
    result = PriceCalculator.from_case(c).run()
    assert result.to_case_output() == c.expect

def test_export(request):
    if request.config.getoption("--export"):
        Path("../shared/price-testcases.json").write_text(
            json.dumps([c.to_json() for c in CASES], ensure_ascii=False, indent=2))
```

```ts
// web-mall/src/utils/__tests__/price.consistency.spec.ts（vitest）
import cases from '../../../../shared/price-testcases.json'
import { calcLocal } from '../price'

describe('前端算价与后端一致性', () => {
  it.each(cases)('case $id: $name', (tc) => {
    const result = calcLocal(tc.input)
    expect(result.payableAmount).toBe(tc.expect.payableAmount)
    // ★ 逐项比对，不只有总额
    expect(result.discountTotal).toBe(tc.expect.discountTotal)
    expect(result.items.map((i) => i.payAmount)).toEqual(tc.expect.items.map((i) => i.payAmount))
  })
})
```

> 测试代码中可以使用 `assert`（pytest 依赖它），"不用 `assert`"的约束只针对生产代码。

**CI 集成**：后端算法变更 → `pytest --export` 重新生成 JSON 并提交 → 前端 CI 跑 vitest 比对 → 不一致则构建失败。另加一条 CI 检查：`shared/price-testcases.json` 与后端导出结果必须一致（防止只改了后端忘了导出）。

## 9. 监控与告警

**线上必须监控"前后端算价不一致"的比例**。

```python
price_mismatch_counter = Counter("price_mismatch_total", "下单价格校验不一致次数", ["kind"])

def classify(diffs: list[PriceDiff]) -> str:
    # ★ 区分"正常变化"和"算法 Bug"
    if all(d.reason in EXPECTED_REASONS for d in diffs):   # 改价、下架、库存、券被占用、活动结束
        return "expected"
    logger.error("★ 算价不一致（疑似Bug）", diffs=diffs)
    return "unexpected"
```

| 指标 | 阈值 | 处理 |
|---|---|---|
| `price_mismatch_total{kind="unexpected"}` 占下单比例 | > 0.1% | 告警，排查算法 |
| `price_mismatch_total{kind="expected"}` 占下单比例 | > 5% | 检查是否有活动异常或库存问题 |
| token 验签失败率 | > 0.5% | 检查密钥是否被轮换、服务器时间是否准确 |

## 10. 边界场景

| 场景 | 处理 |
|---|---|
| 用户用旧 token 下单（页面停留 1 小时） | token 过期 → 返回 `PRICE_TOKEN_EXPIRED`，前端刷新结算页 |
| 用户手改 token | 验签失败 → `INVALID_PRICE_TOKEN` |
| 用户改用另一个账号的 token | `uid` 不匹配 → 拒绝 |
| token 有效但商品已下架 | 重算时该行移除，diff 提示，返回 409 |
| token 有效但库存为 0 | 重算时提示缺货，diff 提示，返回 409 |
| 前端用错单位（金额单位是元） | Pydantic 严格模式下金额字段收到小数直接 422；若是整数元，对比发现差异巨大（100 倍），拒绝并告警（这是前端 Bug） |
| 0.01 元的差异 | **也拒绝**。不设容差——有容差就永远说不清谁对 |
| 并发下单（同 token 两次） | 幂等键拦住，只有一次成功 |
| 优惠券在两次算价之间被用掉 | 重算时券不可用 → 重新计算优惠 → diff 提示 |
| 积分在两次算价之间被其他订单消耗 | 重算时积分不足 → 调整积分抵扣 → diff 提示 |
| 运维轮换了 `PRICE_TOKEN_SECRET` | 旧 token 全部验签失败。轮换时同时配置新旧两个密钥，验签依次尝试，30 分钟后移除旧密钥 |

**"不设容差"的说明**：有人会提议"差异小于 1 分就忽略"。**绝对不行**——如果算法有系统性偏差（比如每次差 1 分），容差会掩盖它，直到某天累积成巨额差异。**任何差异都是 Bug，必须暴露**。

## 11. 设计总结

```
        ┌─────────────────────────────────────────────────┐
        │  结算页（Vue 前端）                                │
        │  ① 调后端拿算价明细（含 priceToken）               │
        │  ② 用户调整选择 → 本地重算（无优惠变化时）or 调后端  │
        │  ③ 展示明细，让用户所见即所得                       │
        └────────────────────┬────────────────────────────┘
                             │ 提交订单（带 priceToken + Idempotency-Key）
                             ▼
        ┌─────────────────────────────────────────────────┐
        │  下单接口（FastAPI）                               │
        │  ① 幂等检查（Idempotency-Key）                    │
        │  ② 验签 + 验 token 有效期                         │
        │  ③ 重新算价（与结算页同一个函数）                   │
        │  ④ 逐项对比                                       │
        │     ├─ 一致 → 创建订单（同一事务内预占库存/锁券）    │
        │     └─ 不一致 → 409 + 差异明细 + 新 token          │
        └────────────────────┬────────────────────────────┘
                             │
                             ▼
        ┌─────────────────────────────────────────────────┐
        │  支付（payment 模块）                              │
        │  ① 从订单读金额（不再按实时数据重算）★              │
        │  ② 发起支付前做"快照自检"                          │
        │  ③ 回调时校验渠道实付金额 == 支付单金额              │
        └─────────────────────────────────────────────────┘
```

**"支付时后端计算后进行对比"的实现位置说明**：

严格说，**按实时数据**重算与对比发生在**下单时**（因为订单金额在此时冻结）。支付时**不再按实时数据重算**（订单金额已是事实，商品此后改价不应影响已下单的订单），而是：
1. 支付单金额 = 订单 `payable_amount`；
2. 发起支付前，**用订单快照重算并对比**（自检，见下）；
3. 支付回调时校验"渠道实付金额 == 支付单金额"（见 [09 §4.3](09-payment.md)）。

```python
async def prepay(session: AsyncSession, user_id: int, order_main_no: str) -> PrepayResult:
    main = await order_service.get_main_owned(session, order_main_no, user_id)
    items = await order_service.list_items(session, order_main_no)
    snaps = await order_service.list_discount_snapshots(session, order_main_no)

    # ★ 从快照重算：只用 order_item 的 _snap 字段 + order_discount_snapshot，不查实时商品数据
    recheck = recalc_from_snapshot(main, items, snaps)
    if recheck.payable_amount != main.payable_amount:
        # 从快照重算应该永远等于订单金额（用的是同一份快照），不等说明有严重 Bug
        logger.error("★ 订单金额与快照重算不一致", order_main_no=order_main_no,
                     order=main.payable_amount, recalc=recheck.payable_amount)
        raise SystemError_("订单金额校验失败，请联系客服")

    return await pay_service.create_and_prepay(session, main)
```

**`recalc_from_snapshot`**：用订单里存的快照重算，而不是查实时数据。这样：
- 不依赖实时商品数据（商品可能已改价）；
- 是一个**自检**：检查订单数据是否自洽（各行实付之和、分摊之和、母子单金额是否满足恒等式）。

这种自检成本很低（纯内存计算），每次发起支付前都做。表上的 `CHECK` 约束（[07 §3](07-order-and-split.md)）保证单行内的恒等式，这里补上跨行、跨表的校验。
