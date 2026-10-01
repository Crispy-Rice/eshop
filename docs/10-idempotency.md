# 10 幂等与防连点

## 1. 连点问题的本质

用户网络慢或手快，连点 5 次"提交订单"→ 生成 5 个订单，**预占 5 份库存**，用户可能支付多份。

**为什么简单的前端禁用按钮不够**：

| 手段 | 能挡住吗 |
|---|---|
| 按钮置灰 | 部分。用户可能刷新页面重来、或用多标签页、或抓包重放请求 |
| 前端计时器 | 部分。同上 |
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

前端统一封装在 axios 实例里，业务代码只需声明"这个请求需要幂等键"：

```ts
// web-mall/src/api/http.ts
import axios, { type AxiosRequestConfig } from 'axios'

export const http = axios.create({ baseURL: '/api', timeout: 10_000 })

// 调用方传入 idempotencyKey，拦截器负责写入请求头
http.interceptors.request.use((config) => {
  const key = (config as AxiosRequestConfig & { idempotencyKey?: string }).idempotencyKey
  if (key) config.headers.set('Idempotency-Key', key)
  return config
})
```

```ts
// web-mall/src/composables/useIdempotentSubmit.ts
import { ref } from 'vue'

/** 进入页面时生成一次 key；只有成功或用户修改了提交内容才换新 key。 */
export function useIdempotentSubmit<T>(request: (key: string) => Promise<T>) {
  let key = crypto.randomUUID()
  const submitting = ref(false)

  async function submit(): Promise<T | undefined> {
    if (submitting.value) return            // ★ 同一页面的连点直接忽略
    submitting.value = true
    try {
      const result = await request(key)     // ★ 失败重试时仍用同一个 key
      key = crypto.randomUUID()             // 成功后才换 key（允许下一次购买）
      return result
    } finally {
      submitting.value = false
    }
  }

  /** 用户修改了地址、数量、优惠券等提交内容时调用：这是一个新的逻辑请求 */
  function resetKey() {
    key = crypto.randomUUID()
  }

  return { submit, submitting, resetKey }
}
```

**关键点**：**失败后重试必须用同一个 key**。这样如果第 1 次实际上成功了（只是响应丢失），重试会拿到第 1 次的结果，而不是创建第 2 个订单。

> `crypto.randomUUID()` 只在安全上下文（HTTPS 或 `localhost`）中可用。第一期用 IP + HTTP 访问时浏览器不提供该函数，需要改用 `uuid` 包的 `v4()`（版本锁定，见 [16](16-deployment.md)）。

### 3.2 服务端的实现（FastAPI 依赖）

幂等以**依赖注入**的方式挂在需要它的路由上，而不是全局中间件——这样每个路由显式声明自己的 TTL，查询接口不受影响。

```python
# app/core/idempotency.py
IDEM_PROCESSING = "__PROCESSING__"

class Idempotency:
    """在路由中使用：idem: IdemContext = Depends(Idempotency(ttl=300))"""

    def __init__(self, ttl: int = 300) -> None:
        self.ttl = ttl

    async def __call__(
        self,
        request: Request,
        user: CurrentUser,
        redis: RedisDep,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> AsyncIterator["IdemContext"]:
        if not key or len(key) > 64:
            raise BizError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED)

        redis_key = f"idem:{user.id}:{request.method}:{request.url.path}:{key}"

        # ★ 原子占位：SET NX EX
        if not await redis.set(redis_key, IDEM_PROCESSING, nx=True, ex=self.ttl):
            cached = await redis.get(redis_key)
            if cached is None or cached == IDEM_PROCESSING:
                # ★ 正在处理中：返回"处理中"而不是"重复提交"，让前端知道要等待
                raise BizError(ErrorCode.REQUEST_PROCESSING)
            # 已完成：直接返回缓存的响应，不再执行业务
            raise CachedResponse(json.loads(cached))

        ctx = IdemContext(redis, redis_key, self.ttl)
        try:
            yield ctx
        except BizError:
            # ★ 明确的业务校验失败（库存不足、价格变化等）：删除 key，允许用户修正后重试
            await redis.delete(redis_key)
            raise
        # 其他异常（系统错误、超时）不删除：业务可能已经提交，
        # 用户在 TTL 内重试会得到 REQUEST_PROCESSING，而不是创建第二单


class IdemContext:
    async def save(self, response: BaseModel) -> None:
        """路由在业务成功、事务提交后调用，把响应体写入 Redis。"""
        await self.redis.set(self.redis_key, response.model_dump_json(by_alias=True), ex=self.ttl)
```

`CachedResponse` 是一个自定义异常，由全局异常处理器转换成 `200` + 缓存的 JSON。路由用法：

```python
@router.post("/api/orders", response_model=ApiResponse[OrderCreated])
async def create_order(
    body: CreateOrderRequest,
    user: CurrentUser,
    session: DbSession,
    idem: Annotated[IdemContext, Depends(Idempotency(ttl=300))],
) -> ApiResponse[OrderCreated]:
    # 事务由 DbSession 依赖统一管理（见 07 §4.4），路由一般不写 begin()
    result = await order_service.create(session, user.id, body, request_id=idem.redis_key)

    # ★ 但写幂等缓存必须**先提交再写**：依赖的提交发生在路由返回之后，
    #   如果不在这里显式提交就写缓存，一旦提交阶段失败（约束冲突等），
    #   用户会拿到一个"成功"的缓存响应，而订单其实没落库。
    await session.commit()

    resp = ApiResponse.ok(result)
    await idem.save(resp)
    return resp
```

> 依赖里那次 `commit()` 在已提交的情况下是空操作，不会重复提交。

> FastAPI 0.106 起，带 `yield` 的依赖在**响应发送前**执行退出代码，因此依赖内能捕获到路由抛出的异常。实现时以项目锁定的 FastAPI 版本跑一遍 §11 的测试用例确认行为。

### 3.3 为什么存"响应体"而不是"只标记已处理"

如果只标记"已处理"，重复请求需要重新走业务逻辑查询结果（比如"查这个用户的最近订单"）。**存响应体**更直接：重复请求直接返回一模一样的 JSON，前端体验完全一致。

**代价**：Redis 存储空间。缓解：TTL 5 分钟 + 只对关键接口（下单、支付、领券、售后申请）启用。

### 3.4 三种状态的语义

| Redis 值 | 含义 | 返回行为 |
|---|---|---|
| 不存在 | 首次请求 | 放行，设为 `__PROCESSING__` |
| `__PROCESSING__` | 上一次还在处理中（或系统异常后未清理） | 返回 `409 REQUEST_PROCESSING`，"请稍候" |
| 一段 JSON | 上次已完成 | 直接返回该 JSON（HTTP 200） |

**为什么 `PROCESSING` 不返回成功**：如果返回成功但业务其实失败了，用户会以为下单成功。返回"处理中"更诚实，前端可以提示等待后再试。

**系统异常后卡在 `PROCESSING` 怎么办**：TTL 到期后自动释放。下单接口额外有 `request_id`（= 幂等 Redis key）写入 `order_main`，用户在订单列表能看到第一次是否已成功，见 §5。

## 4. 第 2 层：业务状态机守卫

**每个写操作前，先校验当前状态是否允许。** 统一用"带状态条件的 UPDATE + 检查 rowcount"实现：

```python
async def cancel_order(session: AsyncSession, user_id: int, order_main_no: str) -> None:
    # CAS：只有本人的、待付款的订单能取消
    result = await session.execute(
        update(OrderMain)
        .where(OrderMain.order_main_no == order_main_no,
               OrderMain.user_id == user_id,
               OrderMain.status == SubOrderStatus.WAIT_PAY)
        .values(status=SubOrderStatus.CLOSED, close_time=func.now())
    )
    if result.rowcount == 0:
        # 已经支付过了 / 已关闭 / 不是本人订单 → 查一次给出准确提示
        raise BizError(await explain_cancel_failure(session, user_id, order_main_no))
    ...
```

```sql
UPDATE trade.order_main
SET status = 50, close_time = now()
WHERE order_main_no = :no AND user_id = :user_id AND status = 10;
-- rowcount = 0 → 不是待付款状态，取消失败（幂等）
```

**状态机守卫的价值**：它把"幂等"变成"业务语义正确"。不只是"不重复执行"，而是"这个操作在当前状态下本来就无意义，拒绝掉"。

## 5. 第 3 层：数据库唯一约束

**所有幂等键最终都要落到唯一约束上**。这是最后一道防线，也是排查问题时最可靠的证据。

```sql
-- 订单号
CONSTRAINT uk_order_main_no UNIQUE (order_main_no)
-- 下单请求（幂等键持久化，Redis 丢失时仍能挡住重复下单）
CREATE UNIQUE INDEX uk_order_main_request ON trade.order_main (user_id, request_id);
-- 一个母单同时只有一个有效支付单（部分唯一索引）
CREATE UNIQUE INDEX uk_payment_main_active ON payment.payment (order_main_no) WHERE status IN (0, 1, 2, 5);
-- 渠道交易号不重复入账
CONSTRAINT uk_payment_channel_trade UNIQUE (channel, out_trade_no)
-- 库存 / 积分幂等键
inventory.stock_biz_key (biz_key PRIMARY KEY)
account.points_biz_key  (biz_key PRIMARY KEY)
-- 领券记录
CONSTRAINT uk_coupon_receive_idem UNIQUE (idempotency_key)
-- 评价（见 12-review）
CREATE UNIQUE INDEX uk_review_order_item ON review.review (order_item_id) WHERE NOT is_follow_up;
-- 资金退款：一个售后单一次退款
CONSTRAINT uk_payment_refund_biz UNIQUE (refund_biz_no)
```

`order_main.request_id VARCHAR(160) NOT NULL` 需要加到 [07 §3.1](07-order-and-split.md) 的母单表中。

写入时统一用 `INSERT ... ON CONFLICT DO NOTHING RETURNING id`：返回空即"已处理过"，比捕获 `IntegrityError` 更清晰，也不会让事务进入失败状态（PG 中任何语句报错都会使当前事务进入 aborted 状态，后续语句全部失败）。

**命名规范**：幂等键统一叫 `biz_key` 或 `idempotency_key`，值用 `{动作}:{业务ID}` 格式，便于排查：

```
LOCK:M20260930...-1:1001     库存预占
PAY:M20260930...             支付实扣
USE:M20260930...:88001       用券
RETURN:R20260930...:1001     退货入库
REFUND:R20260930...          积分返还
```

## 6. 各接口的幂等方案总表

| 接口 | 幂等键 | 幂等实现 | 前端防连点 |
|---|---|---|---|
| 提交订单 | `Idempotency-Key` | Redis `SET NX` + `uk_order_main_request` | 按钮 loading |
| 发起支付 | `Idempotency-Key` | Redis `SET NX` + `uk_payment_main_active` | 按钮 loading |
| 支付回调 | `(channel, notify_type, out_trade_no)` | 落库唯一 + 状态 CAS | N/A（渠道触发） |
| 领取优惠券 | `Idempotency-Key` | Lua 脚本内幂等 + `uk_coupon_receive_idem` | 按钮 loading + 3s 冷却 |
| 锁定优惠券 | `LOCK:{mainNo}:{codeId}` | 状态 CAS（`status = 1`） | N/A（内部调用） |
| 核销优惠券 | `USE:{mainNo}:{codeId}` | 状态 CAS（`status = 2`） | N/A |
| 库存预占 | `LOCK:{orderSubNo}:{skuId}` | Lua 检查 + `stock_biz_key` | N/A |
| 库存回补 | `CANCEL:{mainOrderNo}` | SQL 条件 + `stock_biz_key` | N/A |
| 退货入库 | `RETURN:{refundNo}:{skuId}` | `stock_biz_key` | N/A |
| 申请售后 | `Idempotency-Key` + 订单项行锁 | `refunding_num` 条件更新 | 按钮 loading |
| 确认收货 | 状态 CAS | `WHERE status = 30` | 按钮 loading |
| 取消订单 | 状态 CAS | `WHERE status = 10` | 按钮 loading |
| 提交评价 | `uk_review_order_item` | 唯一约束 | 按钮 loading |
| 商品收藏 | `uk_user_spu` | 唯一约束 + `ON CONFLICT DO NOTHING` | 前端本地状态 |
| 加购物车 | `uk_cart_user_sku` + `num = num + N` | upsert | 允许连点（累加是合理的） |

**注意最后一条**：**加购物车不应该被幂等挡住**。用户快速点 3 次"加入购物车"期望数量变成 3，而不是 1。这提醒我们：**幂等策略要按业务语义决定，不能一刀切**。

## 7. 幂等的反面案例（常见错误）

| 错误做法 | 问题 |
|---|---|
| 用 `SELECT` 判断后 `INSERT` | 并发下两个请求都 SELECT 到空，都 INSERT → 重复 |
| 用 `SELECT ... FOR UPDATE` 但事务里有慢操作 | 锁等待超时，用户体验差（下单要 3 秒） |
| 捕获 `IntegrityError` 后继续在同一事务里执行 | PG 事务已处于 aborted 状态，后续语句全部报错；应改用 `ON CONFLICT` 或 `SAVEPOINT` |
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
| 支付 | 10 分钟 | 支付涉及跳转收银台，窗口稍长 |
| 确认收货 | 1 分钟 | |

## 8. 多进程部署下的幂等

### 8.1 多 worker / 多实例

Uvicorn 以多 worker 进程运行，`docker compose --scale api=N` 还会有多个容器。幂等键必须存**外部存储**（Redis / PG），不能用进程内存（`dict`、`functools.lru_cache`），否则每个进程各有一份，挡不住。

### 8.2 原子性

`SET key value NX EX ttl` 是单命令，天然原子。**如果幂等判断需要跨 key 操作**（如"检查限领 + 扣库存 + 写幂等"），必须用 Lua 脚本（见 [04](04-coupon.md)）。第一期是单实例 Redis；将来切 Redis Cluster 时，同一脚本涉及的 key 需要用 hash tag（如 `coupon:{tpl123}:stock`）保证落在同一 slot。

### 8.3 Redis 不可用时的降级

| 接口 | 降级策略 |
|---|---|
| 提交订单 | **降级为 DB 幂等**：依赖 `uk_order_main_request` 唯一索引兜底，库存直接走 DB 条件更新；同时应用层把下单接口限流到正常的 1/10 |
| 发起支付 | 依赖 `uk_payment_main_active` 兜底，可正常服务 |
| 领券 | **拒绝服务**（没有 Redis 计数，DB 扛不住领券并发，且容易绕过限领） |
| 查询类接口 | 正常服务（无幂等需求），缓存未命中直接查库 |
| 加购物车 | 正常服务，依赖 DB upsert |

**核心原则**：**能用数据库约束兜底的写接口可以降级，兜不住的（领券、秒杀）直接拒绝**。少卖比超卖好，用户重试比重复下单好。降级开关由健康检查自动判断 Redis 状态，也可由运营后台手动切换（开关存 PG 的 `ops.switch` 表，Redis 故障时依然可读）。

> 降级状态下的应用层限流不能再依赖 Redis，改用进程内令牌桶（每个 worker 独立计数，总量 = 单 worker 限额 × worker 数）。

## 9. 特殊场景：同一请求的"部分成功"

**场景**：批量预占 5 个 SKU，前 3 个成功，第 4 个失败。

**处理**：**整单失败**，不允许部分成功。

- **DB 侧**：5 个 SKU 的预占在同一个事务里（[03 §5.1](03-inventory.md)），第 4 个失败抛异常，事务回滚，前 3 个自动撤销，**不需要手写回滚代码**。
- **Redis 侧**：Redis 没有事务回滚，需要显式补偿。

```python
async def redis_lock_batch(redis: Redis, items: list[LockItem], biz_key: str) -> None:
    locked: list[LockItem] = []
    try:
        for it in sorted(items, key=lambda x: (x.warehouse_id, x.sku_id)):
            ok = await lua.stock_lock(redis, it, biz_key=f"{biz_key}:{it.sku_id}")
            if not ok:
                raise StockShortageError(it.sku_id)
            locked.append(it)
    except BaseException:
        # ★ 回补已成功的部分；回补脚本按 biz_key 幂等，重复执行无副作用
        for it in locked:
            await lua.stock_release(redis, it, biz_key=f"{biz_key}:{it.sku_id}")
        raise
```

> 进一步的优化：把整批 SKU 的检查与扣减写进**一个** Lua 脚本（先全部检查，都够了再全部扣减），由 Redis 单线程保证整批原子，就不存在部分成功。代价是脚本涉及多个 key，将来切 Cluster 时需要 hash tag 让它们落在同一 slot。第一期单实例 Redis 推荐用这种方式，见 [14](14-redis-keys.md)。

**为什么不允许部分成功**：用户要的是 5 件一起买，缺 1 件不如整个失败并提示"XX 商品库存不足"。

## 10. 防连点的前端完整方案

```vue
<!-- web-mall/src/views/checkout/SubmitBar.vue -->
<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { createOrder, type CreateOrderPayload } from '@/api/order'
import { useIdempotentSubmit } from '@/composables/useIdempotentSubmit'
import { BizCode, isBizError } from '@/api/errors'

const props = defineProps<{ payload: CreateOrderPayload }>()
const router = useRouter()
const label = ref('提交订单')

const { submit, submitting, resetKey } = useIdempotentSubmit((key) => createOrder(props.payload, key))

// 只有用户修改了订单内容（地址、数量、优惠券）才换 key —— 这是一个新的逻辑请求
watch(() => props.payload, resetKey, { deep: true })

async function onSubmit() {
  try {
    const data = await submit()
    if (data) router.push({ name: 'pay', query: { no: data.orderMainNo } })
  } catch (e) {
    if (isBizError(e, BizCode.REQUEST_PROCESSING)) {
      // ★ 服务端还在处理，不是失败；稍后用同一个 key 重试即可
      label.value = '处理中，请稍候...'
      setTimeout(() => (label.value = '提交订单'), 2000)
      return
    }
    // 业务失败或网络异常：key 保持不变，用户点"重新提交"
    ElMessage.error(isBizError(e) ? e.message : '网络异常，请重试')
    label.value = '重新提交'
  }
}
</script>

<template>
  <el-button type="primary" size="large" :loading="submitting" :disabled="submitting" @click="onSubmit">
    {{ label }}
  </el-button>
</template>
```

**关键细节**：

1. **提交中按钮不可点**（`:loading` + `:disabled`），由 `useIdempotentSubmit` 内的 `submitting` 统一控制。
2. **重试时 key 不变** —— 让服务端能识别出这是同一个请求。
3. **只有用户修改了订单内容才换 key** —— 修改内容是一个新的逻辑请求。
4. `el-button` 的 `loading` 状态自带屏幕阅读器可感知的忙碌提示；错误提示用 `ElMessage`，同时在表单区域保留文字提示，不只依赖颜色。

## 11. 测试清单

后端用 `pytest` + `pytest-asyncio` + `httpx.AsyncClient` 并发发请求，连接 docker 中的测试 PG/Redis（不 mock 数据库，唯一约束和行锁只有真库才能测出来）。

| # | 场景 | 期望 |
|---|---|---|
| 1 | 并发 100 次提交同一幂等键 | 只创建 1 个订单，其余返回相同结果或 `REQUEST_PROCESSING` |
| 2 | 第 1 次请求处理中，第 2 次到达 | 第 2 次返回 `REQUEST_PROCESSING` |
| 3 | 第 1 次业务失败后重试（同 key） | 正常处理（key 已删除） |
| 4 | 第 1 次成功，5 分钟内重试 | 返回缓存的成功响应 |
| 5 | 第 1 次成功，6 分钟后重试 | 被 `uk_order_main_request` 拦截，返回第 1 单的信息（而不是创建新单） |
| 6 | 无 `Idempotency-Key` 请求下单 | 返回 400，拒绝 |
| 7 | 并发支付回调 10 次 | 只处理 1 次，其余幂等返回 |
| 8 | 并发领券 10 次（限领 1） | 只成功 1 次 |
| 9 | 并发取消订单 5 次 | 只成功 1 次，库存只回补 1 次 |
| 10 | 取消后再次取消 | 返回"订单状态不正确" |
| 11 | 支付已关闭的订单 | 返回"订单已关闭" |
| 12 | 批量预占，第 3 个 SKU 缺货 | DB 事务回滚 + Redis 已扣部分回补，全单失败 |
| 13 | 100 个并发请求不同幂等键 | 创建 100 个订单（正常） |
| 14 | Redis 停止后下单 | 走 DB 降级路径成功，重复提交被唯一索引拦截 |
| 15 | 同一 user 在两个标签页下单 | 两个不同的 key，创建 2 个订单（合理） |

**第 5 条与原方案不同**：原方案在 key 过期后会创建新订单；持久化 `request_id` 后，同一个 key 永远只对应一个订单。前端在下单成功后一定会换新 key，所以这不会妨碍用户正常地再下一单。

**第 15 条是刻意的设计**：用户在两个标签页各下一单，是两个独立的意图，应该都成功。幂等只针对"同一个逻辑请求的重复提交"，不是"同一用户的所有请求"。
