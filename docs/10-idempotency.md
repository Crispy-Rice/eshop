# 10 幂等与防连点

## 1. 连点问题的本质

用户网络慢或手快，连点 5 次"提交订单"→ 生成 5 个订单，**预占 5 份库存**，用户可能支付多份。

**为什么简单的前端禁用按钮不够**：

| 手段 | 能挡住吗 |
|---|---|
| 按钮置灰 | 部分。用户可能刷新页面重来、或用多标签页、或抓包重放请求 |
| 前端计时器 | 部分。同 上 |
| 后端限流 | 不精确。限流是"速率"控制，不是"同一请求只执行一次" |
| **幂等键** | ✅ 精确。同一逻辑请求，无论来多少次，只产生一次效果 |

**结论**：前端优化体验，**后端幂等才是保证**。两者都要做。

## 2. 幂等的三个层次

```
第 1 层：接口幂等（Idempotency-Key）      —— 挡住重复的"同一个请求"
第 2 层：业务状态机守卫                   —— 挡住"语义上不该发生"的操作
第 3 层：数据库唯一约束                   —— 挡住"前两层都漏掉"的极端情况
```

**必须三层都有**，因为它们防的是不同的问题：

- 第 1 层防"用户连点"和"网络重试"
- 第 2 层防"订单已支付，又收到一次支付回调"
- 第 3 层防"代码 Bug 或并发窗口"

## 3. 第 1 层：Idempotency-Key 实现

### 3.1 客户端的责任

```javascript
// 前端：进入结算页时生成一次，直到下单成功/失败才重新生成
class OrderSubmitter {
  constructor() {
    this.idempotencyKey = this.generateUUID();   // ★ 只在初始化时生成一次
    this.submitting = false;
  }

  async submit() {
    if (this.submitting) return;      // ★ 同步锁，防同一页面的连点
    this.submitting = true;
    try {
      const resp = await fetch('/api/order/create', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': this.idempotencyKey   // ★ 每次提交都用同一个
        },
        body: JSON.stringify(this.buildPayload())
      });
      // 成功后才重置（允许下一次购买）
      if (resp.ok) this.idempotencyKey = this.generateUUID();
      return resp.json();
    } finally {
      // ★ 不重置 submitting！保持按钮禁用，避免失败后立刻重试
      // 失败时给用户"重试"按钮，重试时用同一个 key（这是关键）
      // this.submitting = false;
    }
  }
}
```

**关键点**：**失败后重试必须用同一个 key**。这样如果第 1 次实际上成功了（只是响应丢失），重试会拿到"已存在"的结果，而不是创建第 2 个订单。

### 3.2 服务端的实现（网关层）

```java
@Component
public class IdempotencyInterceptor implements HandlerInterceptor {

    private static final String HEADER = "Idempotency-Key";
    private static final long TTL_SECONDS = 300;   // 5 分钟窗口

    @Override
    public boolean preHandle(HttpServletRequest req, HttpServletResponse resp, Object handler) {
        String key = req.getHeader(HEADER);
        if (key == null || key.isBlank()) {
            // 强制要求（白名单接口除外）
            throw new BusinessException(IDEMPOTENCY_KEY_REQUIRED);
        }

        String userId = UserContext.getUserId();
        String redisKey = "idem:" + userId + ":" + req.getRequestURI() + ":" + key;

        // ★ 原子占位
        Boolean first = redisTemplate.opsForValue()
            .setIfAbsent(redisKey, "PROCESSING", Duration.ofSeconds(TTL_SECONDS));

        if (Boolean.TRUE.equals(first)) {
            // 首次请求，放行
            req.setAttribute("idemKey", redisKey);
            return true;
        }

        // 已存在：查状态
        String status = (String) redisTemplate.opsForValue().get(redisKey);

        if ("PROCESSING".equals(status)) {
            // ★ 正在处理中：返回"处理中"而不是"重复提交"，让前端知道要等待
            throw new BusinessException(REQUEST_PROCESSING, "请求处理中，请稍候");
        }

        // 已完成：返回缓存的结果
        String cachedResult = status;   // 存的就是序列化后的响应
        resp.setContentType("application/json;charset=UTF-8");
        resp.getWriter().write(cachedResult);
        return false;   // 不再进 Controller
    }

    @Override
    public void afterCompletion(HttpServletRequest req, HttpServletResponse resp, Object handler, Exception ex) {
        String redisKey = (String) req.getAttribute("idemKey");
        if (redisKey == null) return;

        if (ex != null) {
            // ★ 异常时删除 key，允许用户重试
            //   但如果异常发生在"业务已提交、只是响应失败"的情况，删除会有风险
            //   → 因此只对明确的"业务校验失败"删除，系统异常保留（用户重试拿到 PROCESSING 提示）
            if (isBusinessException(ex)) {
                redisTemplate.delete(redisKey);
            }
            return;
        }

        // 成功：把响应体写入 Redis，供重复请求返回
        // 实现上通常用 ResponseBodyAdvice 拿到响应体
        String body = (String) req.getAttribute("idemResponseBody");
        if (body != null) {
            redisTemplate.opsForValue().set(redisKey, body, Duration.ofSeconds(TTL_SECONDS));
        }
    }
}
```

### 3.3 为什么存"响应体"而不是"只标记已处理"

如果只标记"已处理"，重复请求需要重新走业务逻辑查询结果（比如"查这个用户的最近订单"）。**存响应体**更直接：重复请求直接返回一模一样的 JSON，前端体验完全一致。

**代价**：Redis 存储空间。缓解：TTL 5 分钟 + 只对关键接口（下单、支付、领券）启用。

### 3.4 三种状态的语义

| Redis 值 | 含义 | 返回行为 |
|---|---|---|
| 不存在 | 首次请求 | 放行，设为 `PROCESSING` |
| `PROCESSING` | 上一次还在处理中 | 返回 `409 REQUEST_PROCESSING`，"请稍候" |
| 一段 JSON | 上次已完成 | 直接返回该 JSON（HTTP 200） |

**为什么 `PROCESSING` 不返回成功**：如果返回成功但业务其实失败了，用户会以为下单成功。返回"处理中"更诚实，前端可以轮询或提示等待。

## 4. 第 2 层：业务状态机守卫

**每个写操作前，先校验当前状态是否允许。**

```java
// 支付：只有待付款的订单能支付
int rows = payMapper.createIfAbsent(payNo, mainOrderNo, amount,
    "WHERE STATUS = 10");   // 隐含在业务校验中
```

```java
// 取消订单：只有待付款才能取消
public void cancel(String mainOrderNo) {
    // CAS：状态必须是待付款
    int rows = mainMapper.cancel(mainOrderNo, OrderMainStatus.WAIT_PAY);
    if (rows == 0) {
        // 已经支付过了 → 提示"订单已支付，请申请退款"
        throw new BusinessException(ORDER_ALREADY_PAID);
    }
    // ...
}
```

```sql
UPDATE order_main
SET status = 50, close_time = NOW(3)
WHERE order_main_no = ? AND status = 10;
-- RowsAffected = 0 → 不是待付款状态，取消失败（幂等）
```

**状态机守卫的价值**：它把"幂等"变成"业务语义正确"。不只是"不重复执行"，而是"这个操作在当前状态下本来就无意义，拒绝掉"。

## 5. 第 3 层：数据库唯一约束

**所有幂等键最终都要落到唯一索引上**。这是最后一道防线，也是排查问题时最可靠的证据。

```sql
-- 订单
UNIQUE KEY `uk_main_no` (`order_main_no`)

-- 一个母单一个支付单
UNIQUE KEY `uk_main_order` (`order_main_no`)

-- 渠道交易号不重复入账
UNIQUE KEY `uk_channel_trade` (`channel`, `out_trade_no`)

-- 库存流水
UNIQUE KEY `uk_biz_key` (`biz_key`)

-- 积分流水
UNIQUE KEY `uk_biz_key` (`biz_key`)

-- 领券记录
UNIQUE KEY `uk_idem` (`idempotency_key`)

-- 评价（见 12-review）
UNIQUE KEY `uk_user_sku` (`user_id`, `sku_id`, `order_item_id`)

-- 售后
UNIQUE KEY `uk_biz_no` (`refund_biz_no`)  -- 一个售后单一次退款
```

**命名规范**：幂等键统一叫 `biz_key` 或 `idempotency_key`，值用 `{动作}:{业务ID}` 格式，便于排查：

```
LOCK:SO20260930123:1001      库存预占
PAY:M20260930123             支付实扣
USE:M20260930123:88001       用券
RETURN:R20260930123:1001     退货入库
REFUND:R20260930123          积分返还
```

## 6. 各接口的幂等方案总表

| 接口 | 幂等键 | 幂等实现 | 前端防连点 |
|---|---|---|---|
| 提交订单 | `Idempotency-Key` | 网关 SETNX + 订单号唯一 | 按钮置灰 + 5s 冷却 |
| 发起支付 | `Idempotency-Key` | 网关 SETNX + `uk_main_order` | 按钮置灰 |
| 支付回调 | `(channel, out_trade_no)` | 落库唯一 + 状态 CAS | N/A（渠道触发） |
| 领取优惠券 | `Idempotency-Key` | Lua 脚本内幂等 + `uk_idem` | 按钮置灰 + 3s |
| 锁定优惠券 | `LOCK:{mainNo}:{codeId}` | 状态 CAS（`status=1`） | N/A（内部调用） |
| 核销优惠券 | `USE:{mainNo}:{codeId}` | 状态 CAS + 流水唯一 | N/A |
| 库存预占 | `LOCK:{orderSubNo}:{skuId}` | Lua 检查 + `uk_biz_key` | N/A |
| 库存回补 | `CANCEL:{mainOrderNo}` | SQL 条件 + `uk_biz_key` | N/A |
| 退货入库 | `RETURN:{refundNo}:{skuId}` | `uk_biz_key` | N/A |
| 申请售后 | `Idempotency-Key` + 订单项 CAS | `refunded_num` 条件更新 | 按钮置灰 |
| 确认收货 | 状态 CAS | `WHERE status = 30` | 按钮置灰 |
| 取消订单 | 状态 CAS | `WHERE status = 10` | 按钮置灰 |
| 提交评价 | `uk_user_sku` | 唯一索引 | 按钮置灰 |
| 商品收藏 | `uk_user_spu` | 唯一索引 | 前端本地状态 |
| 加购物车 | `uk_user_sku` + `num = num + N` | 唯一索引 | 允许连点（累加是合理的） |

**注意最后一条**：**加购物车不应该被幂等挡住**。用户快速点 3 次"加入购物车"期望数量变成 3，而不是 1。这提醒我们：**幂等策略要按业务语义决定，不能一刀切**。

## 7. 幂等的反面案例（常见错误）

| 错误做法 | 问题 |
|---|---|
| 用 `SELECT` 判断后 `INSERT` | 并发下两个请求都 SELECT 到空，都 INSERT → 重复 |
| 用 `SELECT ... FOR UPDATE` 但事务提交晚 | 锁等待超时，用户体验差（下单要 3 秒） |
| 用时间戳当幂等键 | 两次点击时间不同，不认为是同一个请求 |
| 用"用户 ID + 商品 ID"当幂等键 | 用户确实会买两次同一个商品 |
| 幂等键 TTL 太长（24h） | 用户第二天想重新下单，被判定为重复 |
| 幂等键 TTL 太短（10s） | 网络重试在 15s 后到达，产生两单 |
| 只在前端防连点 | 抓包/多标签页绕过 |
| 异常时也缓存错误响应 | 用户重试永远拿到同一个错误 |

**TTL 的建议值**：

| 接口 | TTL | 理由 |
|---|---|---|
| 提交订单 | 5 分钟 | 足够覆盖网络重试，又不会妨碍用户重新下单 |
| 领券 | 1 分钟 | 领券是短促动作，1 分钟足够 |
| 支付 | 10 分钟 | 支付可能涉及跳转，窗口稍长 |
| 确认收货 | 1 分钟 | |

## 8. 分布式场景下的幂等

### 8.1 多实例部署

幂等键必须存**外部存储**（Redis），不能用 JVM 内存（8 个实例各有一份，挡不住）。

### 8.2 Redis 集群下的原子性

`SETNX` 在 Redis Cluster 下单 key 操作是原子的，可放心使用。

**但如果幂等键需要跨 key 操作**（如"检查限领 + 扣库存 + 写幂等"），必须用 Lua 脚本（见 [04](04-coupon.md)）。

### 8.3 Redis 不可用时的降级

| 接口 | 降级策略 |
|---|---|
| 提交订单 | **拒绝服务**（无幂等保护会产生重复订单，代价太高） |
| 领券 | 拒绝服务（会超发） |
| 查询类接口 | 正常服务（无幂等需求） |
| 加购物车 | 可降级为 DB 唯一索引兜底（影响小） |

**核心原则**：**资金和库存相关的接口，拿不到 Redis 就拒绝，不要"降级放行"**。少卖比超卖好，用户重试比重复下单好。

## 9. 特殊场景：同一请求的"部分成功"

**场景**：批量预占 5 个 SKU，前 3 个成功，第 4 个失败。

**处理**：**整单回滚**。不允许部分成功。

```java
public LockResult lockBatch(List<LockItem> items, String bizKey) {
    // 按 (warehouseId, skuId) 排序防死锁
    items.sort(...);

    // 使用同一幂等键加序号
    List<LockedItem> locked = new ArrayList<>();
    try {
        for (int i = 0; i < items.size(); i++) {
            LockItem item = items.get(i);
            String itemBizKey = bizKey + ":" + item.getSkuId();
            if (!doLock(item, itemBizKey)) {
                throw new StockShortageException(item.getSkuId());
            }
            locked.add(...);
        }
        return LockResult.success();
    } catch (Exception e) {
        // ★ 回滚已成功的部分
        for (LockedItem l : locked) {
            releaseOne(l, bizKey + ":ROLLBACK:" + l.getSkuId());
        }
        throw e;
    }
}
```

**为什么不允许部分成功**：用户体验上，用户要的是 5 件一起买，缺 1 件不如整个失败并提示"XX 商品库存不足"。

**回滚本身也必须幂等**：用 `bizKey + ":ROLLBACK:" + skuId` 作为回滚的幂等键。

## 10. 防连点的前端完整方案

```html
<button id="submitBtn" onclick="submitOrder()">提交订单</button>

<script>
const state = {
  idempotencyKey: uuidv4(),
  phase: 'IDLE',          // IDLE | SUBMITTING | SUCCESS | FAILED
  lastClickTime: 0
};

async function submitOrder() {
  const btn = document.getElementById('submitBtn');

  // ① 状态检查
  if (state.phase === 'SUBMITTING') return;
  if (state.phase === 'SUCCESS') { router.push('/order/success'); return; }

  // ② 点击冷却（300ms，纯体验优化）
  const now = Date.now();
  if (now - state.lastClickTime < 300) return;
  state.lastClickTime = now;

  // ③ 进入提交态，按钮置灰
  state.phase = 'SUBMITTING';
  btn.disabled = true;
  btn.textContent = '提交中...';

  try {
    const resp = await api.post('/order/create', payload, {
      headers: { 'Idempotency-Key': state.idempotencyKey },
      timeout: 10000
    });

    if (resp.code === 0) {
      state.phase = 'SUCCESS';
      router.push(`/order/pay?no=${resp.data.orderMainNo}`);
      return;
    }

    if (resp.code === 'REQUEST_PROCESSING') {
      // ★ 服务端在处理中，不是失败
      btn.textContent = '处理中，请稍候...';
      setTimeout(() => { state.phase = 'IDLE'; btn.disabled = false;
                         btn.textContent = '提交订单'; }, 2000);
      return;
    }

    // 业务失败
    showError(resp.message);
    state.phase = 'FAILED';
    btn.disabled = false;
    btn.textContent = '重新提交';   // ★ 重试时 idempotencyKey 不变（关键）
    // 只有用户修改了订单内容（改地址、改数量）才重新生成 key

  } catch (e) {
    // 网络异常：key 保持不变，用户重试
    state.phase = 'FAILED';
    btn.disabled = false;
    btn.textContent = '网络异常，重试';
    showError('网络异常，请重试');
  }
}
</script>
```

**关键细节**：

1. **`SUBMITTING` 状态不自动解除**（除非服务端明确说"处理中，可重试"）——防止失败后立即重试造成第 2 单。
2. **重试时 key 不变** —— 让服务端能识别出这是同一个请求。
3. **只有用户修改了订单内容才换 key** —— 修改内容是一个新的逻辑请求。

## 11. 测试清单

| # | 场景 | 期望 |
|---|---|---|
| 1 | 并发 100 次提交同一幂等键 | 只创建 1 个订单，其余返回相同结果 |
| 2 | 第 1 次请求处理中，第 2 次到达 | 第 2 次返回 `REQUEST_PROCESSING` |
| 3 | 第 1 次失败后重试（同 key） | 正常处理（key 已删除） |
| 4 | 第 1 次成功，5 分钟内重试 | 返回缓存的成功响应 |
| 5 | 第 1 次成功，6 分钟后重试 | 创建新订单（key 已过期） |
| 6 | 无 `Idempotency-Key` 请求下单 | 返回 400，拒绝 |
| 7 | 并发支付回调 10 次 | 只处理 1 次，其余幂等返回 |
| 8 | 并发领券 10 次（限领 1） | 只成功 1 次 |
| 9 | 并发取消订单 5 次 | 只成功 1 次，库存只回补 1 次 |
| 10 | 取消后再次取消 | 返回"订单状态不正确" |
| 11 | 支付已关闭的订单 | 返回"订单已关闭" |
| 12 | 批量预占，第 3 个 SKU 缺货 | 前 2 个回滚，全单失败 |
| 13 | 100 个并发请求不同幂等键 | 创建 100 个订单（正常） |
| 14 | Redis 宕机时下单 | 拒绝服务，友好提示 |
| 15 | 同一 user 在两个标签页下单 | 两个不同的 key，创建 2 个订单（合理） |

**第 15 条是刻意的设计**：用户在两个标签页各下一单，是两个独立的意图，应该都成功。幂等只针对"同一个逻辑请求的重复提交"，不是"同一用户的所有请求"。
