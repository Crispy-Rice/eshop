# 14 Redis Key 与 Lua 脚本汇总

> 第一期部署为**单实例 Redis 7.4**（Docker 容器，AOF 持久化），同时承担：库存/券闸门、缓存、幂等键、Redis Streams 事件、ARQ 任务队列。为了将来平滑迁移到 Redis Cluster，Lua 脚本涉及的多个 key 统一使用 hash tag（`{...}`）保证可落在同一 slot，见 §1。

## 1. Key 命名规范

```
{业务域}:{对象}:{标识}[:{子标识}]

业务域：stock / coupon / seckill / cache / idem / rate / lock / switch / stream / arq
标识：ID 或 业务号
```

**规范要求**：

| 要求 | 说明 |
|---|---|
| 同一 Lua 脚本操作的 key 带相同 hash tag | 如 `stock:{sku:1001:wh:1}:shard:0` 与 `stock:{sku:1001:wh:1}:zero`，单实例下无影响，切 Cluster 时不用改 key |
| 所有库存类 key 带 `:shard:{n}` 后缀 | 支持分片，见 [03-inventory §3.3](03-inventory.md) |
| 所有 key 必须能通过 `SCAN` 模式批量清理 | 例如 `SCAN 0 MATCH stock:{sku:1001:*` |
| 缓存类 key 必须设置 TTL | 防止内存泄漏；库存/券计数这类"闸门"key 除外 |
| 禁止大 key（> 10KB 或 > 5000 元素） | 券的作用域集合超限时反转逻辑，见 §8 |
| key 前缀集中定义 | `app/core/redis_keys.py` 中以函数形式定义，业务代码禁止手拼字符串 |

```python
# app/core/redis_keys.py
def stock_shard(sku_id: int, wh_id: int, i: int) -> str:
    return f"stock:{{sku:{sku_id}:wh:{wh_id}}}:shard:{i}"

def stock_zero(sku_id: int, wh_id: int) -> str:
    return f"stock:{{sku:{sku_id}:wh:{wh_id}}}:zero"

def stock_lock(order_sub_no: str, sku_id: int) -> str:
    return f"stock:lock:{order_sub_no}:{sku_id}"
```

## 2. Key 清单

### 2.1 库存

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `stock:{sku:{skuId}:wh:{whId}}:shard:{i}` | String(int) | 永久 | 分片可售库存，`i ∈ [0, N)`，N 默认 4（单机） |
| `stock:{sku:{skuId}:wh:{whId}}:meta` | Hash | 永久 | `{total, available, locked, frozen, version, syncTs}`，对账用 |
| `stock:{sku:{skuId}:wh:{whId}}:zero` | String | 1h | 售罄标记，快速拒绝（避免查分片） |
| `stock:lock:{orderSubNo}:{skuId}` | Hash | 2h | 预占记录：`{num, shard, orderNo, ts, status}` |
| `stock:batch:{requestId}` | Hash | 2h | 批量预占记录（§3.2），`{skuId:whId → shard:num}` + `status` |

**售罄快速拒绝的设计意图**：

```python
# 下单前先看售罄标记（一次 EXISTS，比 MGET 全部分片快）
if await redis.exists(keys.stock_zero(sku_id, wh_id)):
    raise StockShortageError(sku_id)       # 快速失败
```

售罄标记由扣减脚本在"所有分片都为 0"时设置，由回补脚本删除。

### 2.2 优惠券

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `coupon:{tpl:{tplId}}:stock` | String(int) | 活动结束后 7d | 剩余可发量 |
| `coupon:{tpl:{tplId}}:user_count` | Hash | 活动结束后 30d | `{userId: 已领数}` |
| `coupon:{tpl:{tplId}}:meta` | Hash | 活动结束后 7d | 模板缓存：`{tplId, startTs, endTs, status, threshold, ...}` |
| `coupon:{tpl:{tplId}}:spu_set` | Set | 活动结束后 7d | 适用 SPU 集合（`scope_type=2` 且范围小时） |
| `coupon:{tpl:{tplId}}:sku_set` | Set | 活动结束后 7d | 适用 SKU 集合 |
| `coupon:idem:{idempotencyKey}` | String | 24h | 领券脚本内的幂等结果 |
| `coupon:code:{codeId}` | Hash | 到券有效期 | 券实例状态：`{userId, status, orderNo, validEnd}` |
| `coupon:user:{userId}` | Hash | 7d | 用户券列表缓存：`{codeId: status}` |
| `coupon:ip:{ip}:{tplId}` | String(int) | 1d | IP 维度领券计数 |
| `coupon:pending:{userId}:{tplId}:{idem}` | String | 5m | 预发券标记（异步生成券实例期间） |

> 领券脚本同时操作 `stock`、`user_count`、`meta` 和 `coupon:idem:*`。前三个共享 hash tag `{tpl:{tplId}}`；`coupon:idem:*` 在 Cluster 下会落到别的 slot，迁移时需改为 `coupon:{tpl:{tplId}}:idem:{key}`。第一期单实例不受影响，但新代码直接使用带 tag 的形式。

### 2.3 幂等与限流

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `idem:{userId}:{method}:{path}:{key}` | String | 按接口 1~10m | 值 = `__PROCESSING__` 或响应 JSON（[10 §3.2](10-idempotency.md)） |
| `rate:user:{userId}:{route}` | String(int) | 1s | 用户维度限流计数（固定窗口） |
| `rate:ip:{ip}` | String(int) | 1s | IP 维度限流计数 |
| `review:daily:{userId}:{yyyymmdd}` | String(int) | 2d | 每日评价条数 |

### 2.4 秒杀排队

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `seckill:{act:{actId}}:queue` | List | 活动结束后 1h | 排队队列 |
| `seckill:{act:{actId}}:uid_set` | Hash | 活动结束后 1d | 已参与用户 `{userId: 已抢数量}`（去重/限购） |
| `seckill:{act:{actId}}:stock` | String(int) | 活动结束后 1d | 活动库存 |
| `seckill:result:{actId}:{userId}` | String | 5m | 排队结果 `SUCCESS/FAILED/{orderNo}` |
| `seckill:consumer:{actId}` | String | 30s | 消费协程的单实例锁（[03 §2.2](03-inventory.md)） |

### 2.5 商品与运费缓存

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `cache:sku:{skuId}` | String(JSON) | 5m ± 60s | SKU 详情（价格、重量、店铺） |
| `cache:spu:{spuId}` | String(JSON) | 5m ± 60s | SPU 详情（含规格与 SKU 列表） |
| `cache:freight_tpl:{tplId}` | String(JSON) | 10m ± 60s | 运费模板 + 区域规则 |
| `cache:null:sku:{skuId}` | String | 60s | 空值缓存（防穿透） |

购物车**不放 Redis**：第一期购物车直接存 PG（[02 §4](02-domain-model.md)），数据量与并发都不需要缓存层。

### 2.6 锁、开关与 Streams

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `lock:stock_init:{skuId}:{whId}` | String | 3s | 库存回源 DB 初始化时的互斥锁 |
| `lock:settle:{shopId}:{date}` | String | 1h | 结算任务互斥 |
| `switch:{name}` | String | 永久 | 降级开关的缓存副本（真源在 PG `ops.switch`，[10 §8.3](10-idempotency.md)） |
| `stream:{topic}` | Stream | `MAXLEN ~ 100000` | 领域事件，如 `stream:trade.order_paid`，消费组见 §6 |
| `stream:dead:{topic}` | Stream | `MAXLEN ~ 10000` | 死信（重试超过 5 次） |
| `arq:*` | — | ARQ 管理 | ARQ 的任务队列与结果，**不要手工改写** |

## 3. 核心 Lua 脚本

脚本放在 `backend/lua/*.lua`，应用启动时通过 `redis.register_script()` 注册（底层用 `EVALSHA`，脚本缓存丢失时自动回退为 `EVAL`）：

```python
# app/core/redis.py
class LuaScripts:
    def __init__(self, redis: Redis) -> None:
        load = lambda name: redis.register_script((LUA_DIR / f"{name}.lua").read_text())
        self.stock_lock_batch = load("stock_lock_batch")
        self.stock_release_batch = load("stock_release_batch")
        self.coupon_receive = load("coupon_receive")
        self.coupon_lock = load("coupon_lock")
        self.seckill_enqueue = load("seckill_enqueue")
```

所有脚本的时间戳都由应用传入（`int(time.time())`），原因见 [04 §4.1](04-coupon.md)。

### 3.1 单 SKU 库存预占（含幂等）

```lua
-- ============================================================
-- stock_lock.lua：单个 SKU 预占（秒杀消费者、单商品下单使用）
-- KEYS[1] = stock:{sku:X:wh:Y}:shard:{i}
-- KEYS[2] = stock:lock:{orderSubNo}:{skuId}
-- KEYS[3] = stock:{sku:X:wh:Y}:zero
-- ARGV[1] = qty
-- ARGV[2] = lock TTL 秒
-- ARGV[3] = orderSubNo
-- ARGV[4] = 当前时间戳
-- 返回 {code, remain}
--   1 成功 / 0 库存不足 / 2 幂等命中 / -1 未初始化 / 3 整体售罄
-- ============================================================
local qty     = tonumber(ARGV[1])
local lockKey = KEYS[2]

-- ① 幂等：同一 (子单, SKU) 已预占过
local lockedNum = redis.call('HGET', lockKey, 'num')
if lockedNum then
    return {2, tonumber(lockedNum)}
end

-- ② 快速失败：已售罄
if redis.call('EXISTS', KEYS[3]) == 1 then
    return {3, 0}
end

-- ③ 库存检查与扣减
local stock = redis.call('GET', KEYS[1])
if not stock then
    return {-1, 0}                     -- 未初始化，调用方需回源 DB 后重试（§7）
end
stock = tonumber(stock)
if stock < qty then
    return {0, stock}
end
redis.call('DECRBY', KEYS[1], qty)

-- ④ 写预占记录
redis.call('HSET', lockKey, 'num', qty, 'shard', KEYS[1], 'orderNo', ARGV[3],
           'ts', ARGV[4], 'status', 'LOCKED')
redis.call('EXPIRE', lockKey, tonumber(ARGV[2]))
return {1, stock - qty}
```

### 3.2 批量库存预占（下单主路径，整批原子）

[10 §9](10-idempotency.md) 提到的"整批原子"方案：先检查全部 SKU，全部满足后再统一扣减，Redis 单线程保证不会出现部分成功。

```lua
-- ============================================================
-- stock_lock_batch.lua：一次下单的所有 SKU 整批预占
-- KEYS[1]       = stock:batch:{requestId}           批量预占记录（幂等 + 回补依据）
-- KEYS[2..n+1]  = 每个 SKU 选中的分片 key（调用方按 (whId, skuId) 排序并按 userId 选片）
-- ARGV[1]       = n（SKU 个数）
-- ARGV[2]       = 预占记录 TTL 秒
-- ARGV[3]       = 当前时间戳
-- ARGV[4..n+3]  = 每个 SKU 的数量，与 KEYS[2..] 一一对应
-- 返回 {code, idx, remain}
--   1 成功 / 2 幂等命中 / 0 第 idx 个 SKU 库存不足（remain 为该分片余量）/ -1 第 idx 个未初始化
-- ============================================================
local batchKey = KEYS[1]
local n = tonumber(ARGV[1])

-- ① 幂等
if redis.call('EXISTS', batchKey) == 1 then
    return {2, 0, 0}
end

-- ② 先检查全部，任何一个不足就整体失败（此时还没有任何写操作）
for i = 1, n do
    local stock = redis.call('GET', KEYS[i + 1])
    if not stock then
        return {-1, i, 0}
    end
    if tonumber(stock) < tonumber(ARGV[i + 3]) then
        return {0, i, tonumber(stock)}
    end
end

-- ③ 全部满足，统一扣减并记录
for i = 1, n do
    local qty = tonumber(ARGV[i + 3])
    redis.call('DECRBY', KEYS[i + 1], qty)
    redis.call('HSET', batchKey, KEYS[i + 1], qty)
end
redis.call('HSET', batchKey, '__status', 'LOCKED', '__ts', ARGV[3])
redis.call('EXPIRE', batchKey, tonumber(ARGV[2]))
return {1, 0, 0}
```

> 扣减后某分片变为 0 时的售罄标记，由调用方在脚本返回后检查各 SKU 所有分片（`MGET`）再设置，不放进这个脚本——售罄标记的 key 与分片不在同一个 SKU 下，放进来会让脚本的 key 列表翻倍。

### 3.3 批量库存回补

```lua
-- ============================================================
-- stock_release_batch.lua：取消/超时/下单事务失败时回补整批预占
-- KEYS[1] = stock:batch:{requestId}
-- ARGV[1] = 当前时间戳
-- 返回 {code}  1 回补成功 / 2 幂等（已回补或已实扣）/ 0 无预占记录
-- 注意：回补的分片 key 从记录中读出，属于"脚本内动态 key"，
--       单实例 Redis 可用；切 Cluster 前必须改为由调用方先 HGETALL 再传入 KEYS
-- ============================================================
local batchKey = KEYS[1]
local status = redis.call('HGET', batchKey, '__status')
if not status then
    return {0}
end
if status ~= 'LOCKED' then
    return {2}                         -- RELEASED 或 CONFIRMED，幂等
end

local fields = redis.call('HGETALL', batchKey)
for i = 1, #fields, 2 do
    local k = fields[i]
    if string.sub(k, 1, 2) ~= '__' then
        redis.call('INCRBY', k, tonumber(fields[i + 1]))
    end
end
redis.call('HSET', batchKey, '__status', 'RELEASED', '__releaseTs', ARGV[1])
-- 预占记录延长保留 1 天，便于排查
redis.call('EXPIRE', batchKey, 86400)
return {1}
```

回补成功后由调用方删除相关 SKU 的售罄标记。支付成功后的"预占转实扣"只需把 `__status` 置为 `CONFIRMED`（防止之后误回补），脚本与 §3.4 相同，不再列出。

### 3.4 单 SKU 回补 / 实扣

```lua
-- ============================================================
-- stock_release.lua：单 SKU 预占回补
-- KEYS[1] = stock:lock:{orderSubNo}:{skuId}
-- KEYS[2] = 该预占记录中的分片 key（调用方先 HGET shard 再传入，保证 Cluster 兼容）
-- KEYS[3] = stock:{sku:X:wh:Y}:zero
-- ARGV[1] = 当前时间戳
-- 返回 {code, qty}  1 回补成功 / 2 幂等（已回补或已实扣）/ 0 无预占记录
-- ============================================================
local status = redis.call('HGET', KEYS[1], 'status')
if not status then return {0, 0} end
if status ~= 'LOCKED' then return {2, 0} end

local qty = tonumber(redis.call('HGET', KEYS[1], 'num'))
redis.call('INCRBY', KEYS[2], qty)
redis.call('HSET', KEYS[1], 'status', 'RELEASED', 'releaseTs', ARGV[1])
redis.call('DEL', KEYS[3])             -- 有货了，清除售罄标记
return {1, qty}
```

```lua
-- ============================================================
-- stock_confirm.lua：支付成功后预占转实扣（只改记录状态，库存数在预占时已扣）
-- KEYS[1] = stock:lock:{orderSubNo}:{skuId}  或  stock:batch:{requestId}
-- ARGV[1] = 状态字段名（单 SKU 记录为 'status'，批量记录为 '__status'）
-- 返回 {code}  1 成功 / 2 幂等 / 0 无记录 / -1 已回补（时序错乱，需告警）
-- ============================================================
local st = redis.call('HGET', KEYS[1], ARGV[1])
if not st then return {0} end
if st == 'CONFIRMED' then return {2} end
if st == 'RELEASED' then return {-1} end
redis.call('HSET', KEYS[1], ARGV[1], 'CONFIRMED')
return {1}
```

**`-1` 的含义**：支付成功时发现 Redis 预占已被回补（比如超时关单与晚到的支付同时发生）。DB 才是账本——此时走 [09 §7](09-payment.md) 的晚付处理，Redis 计数由对账任务按 DB 修正。

### 3.5 领券（完整版）

见 [04-coupon §4](04-coupon.md)，此处不重复。

### 3.6 券锁定

```lua
-- ============================================================
-- coupon_lock.lua：锁券（订单占用）
-- KEYS[1] = coupon:code:{codeId}
-- ARGV[1] = orderMainNo
-- ARGV[2] = 锁定时长（秒）
-- ARGV[3] = 当前时间戳（秒）
-- 返回 {code}  1成功 / 2已使用 / 3被其他订单锁定 / 4已过期 / 0券不存在（缓存未命中，回源 DB）
-- ============================================================
local codeKey = KEYS[1]
if redis.call('EXISTS', codeKey) == 0 then
    return {0}
end

local status   = redis.call('HGET', codeKey, 'status')
local orderNo  = redis.call('HGET', codeKey, 'orderNo')
local validEnd = tonumber(redis.call('HGET', codeKey, 'validEnd') or '0')
local nowTs    = tonumber(ARGV[3])

if status == 'USED' then return {2} end
if status == 'LOCKED' then
    if orderNo == ARGV[1] then
        return {1}                    -- 同一订单重复锁，幂等成功
    end
    return {3}
end
if validEnd > 0 and nowTs > validEnd then
    redis.call('HSET', codeKey, 'status', 'EXPIRED')
    return {4}
end

redis.call('HSET', codeKey, 'status', 'LOCKED', 'orderNo', ARGV[1], 'lockTs', ARGV[3])
return {1}
```

> 原方案在锁券时把券的 TTL 改成 30 分钟，订单关闭后 key 过期消失，下一次查询再回源 DB。这里改为**不修改 TTL**：券缓存的生命周期始终跟随券有效期，解锁由关单流程显式执行（[07 §7.3](07-order-and-split.md)）。锁券的权威记录是 DB 的条件更新（[04 §6](04-coupon.md)），Redis 只是让结算页能快速判断。

### 3.7 秒杀入队

```lua
-- ============================================================
-- seckill_enqueue.lua
-- KEYS[1] = seckill:{act:X}:queue
-- KEYS[2] = seckill:{act:X}:uid_set
-- KEYS[3] = seckill:{act:X}:stock
-- ARGV[1] = userId   ARGV[2] = skuId   ARGV[3] = num   ARGV[4] = requestId
-- ARGV[5] = 队列最大长度   ARGV[6] = 每人限购数   ARGV[7] = 当前时间戳
-- 返回 {code, data}
--   1 入队成功(返回排位) / 2 已达限购 / 3 队列已满 / 4 已售罄
-- ============================================================
local userId = ARGV[1]
local num    = tonumber(ARGV[3])

-- ① 库存已空，直接拒绝（避免无意义排队）
if tonumber(redis.call('GET', KEYS[3]) or '0') <= 0 then
    return {4, 0}
end

-- ② 限购校验（本次加上后不能超过限购）
local myCount = tonumber(redis.call('HGET', KEYS[2], userId) or '0')
if myCount + num > tonumber(ARGV[6]) then
    return {2, myCount}
end

-- ③ 队列长度限制
local qlen = redis.call('LLEN', KEYS[1])
if qlen >= tonumber(ARGV[5]) then
    return {3, qlen}
end

-- ④ 入队（LPUSH 入、RPOP 出 = FIFO）
redis.call('LPUSH', KEYS[1], cjson.encode({
    uid = userId, sku = ARGV[2], num = num, req = ARGV[4], ts = tonumber(ARGV[7])
}))
redis.call('HINCRBY', KEYS[2], userId, num)
return {1, qlen + 1}
```

消费端使用 [03 §2.2](03-inventory.md) 中的 `BRPOP` 协程逐个处理（每处理一个都要调 trade 模块创建订单，批量出队并不能减少这部分开销）。抢购失败的请求需要把 `uid_set` 中的计数减回去，否则用户无法再次参与。

## 4. 容量规划（第一期单实例）

### 4.1 内存估算

| Key 类型 | 单条大小 | 数量（第一期预估） | 总内存 |
|---|---|---|---|
| 库存分片 | ~100 B | 10 万 SKU × 4 片 | ~40 MB |
| 库存 meta | ~200 B | 10 万 | ~20 MB |
| 预占记录 | ~300 B | 峰值 1 万 | ~3 MB |
| 券实例缓存 | ~200 B | 峰值 100 万 | ~200 MB |
| 券 user_count | ~50 B/field | 100 万 field | ~50 MB |
| 幂等键 | ~1 KB | 峰值 1 万 | ~10 MB |
| 商品缓存 | ~2 KB | 10 万 | ~200 MB |
| Streams + ARQ | — | 按 MAXLEN 截断 | ~100 MB |

**合计约 0.6 GB**。Redis 容器 `maxmemory` 设为 **1.5 GB**（留足余量与 AOF 重写时的 fork 开销），宿主机 8G 内存足够。

**只缓存活跃 SKU 的库存**：长尾商品没有并发，库存 key 不存在时走 DB 路径（带 `lock:stock_init` 互斥锁回源初始化，§7），不必把全量 SKU 预热进 Redis。

### 4.2 将来的扩展路径

| 阶段 | 触发条件 | 做法 |
|---|---|---|
| 一期 | — | 单实例 + AOF，与应用同机 |
| 二期 | 内存 > 1GB 或需要高可用 | 迁移到腾讯云 Redis 主从版（应用只改 `REDIS_URL`） |
| 三期 | 单实例 CPU 成为瓶颈 | 腾讯云 Redis 集群版；库存、券 key 已带 hash tag，需把 §3.3 的动态 key 改为调用方传入 |

## 5. 持久化与故障处理

### 5.1 持久化配置

```conf
# deploy/redis/redis.conf
appendonly yes
appendfsync everysec            # 每秒 fsync，最多丢 1 秒数据
aof-use-rdb-preamble yes        # 混合持久化：重启加载快
no-appendfsync-on-rewrite yes
save 900 1                      # RDB 快照，用于备份（每日拷贝到备份目录）
save 300 100

maxmemory 1536mb
maxmemory-policy noeviction     # ★ 写满就报错，绝不静默淘汰库存/券 key
requirepass ${REDIS_PASSWORD}   # 由 docker compose 注入
protected-mode yes
# 禁用危险命令（生产）
rename-command FLUSHALL ""
rename-command FLUSHDB ""
rename-command KEYS ""
```

**为什么用 `noeviction`**：缓存、闸门、队列共用一个实例时，`allkeys-lru` 会淘汰库存分片 key，淘汰 = 库存数据丢失 = 可能超卖。宁可在内存满时让写入报错（应用侧会走降级），也不能静默丢 key。缓存类 key 都有 TTL，正常情况下内存不会被它们撑满。

**`everysec` 的后果**：Redis 宕机最多丢 1 秒数据。1 秒内的库存扣减丢失 → Redis 库存偏高 → 有超卖风险。

**应对**：**DB 侧条件更新与 `CHECK` 约束是最终防线**（[03 §5](03-inventory.md)）。Redis 数据丢失只影响"拦截效率"，不影响正确性；Redis 重启后由对账任务按 DB 重建库存计数。

### 5.2 故障降级

```python
async def lock_stock(items: list[LockItem], request_id: str) -> LockPath:
    if await switches.is_on("stock_redis_degraded"):
        return LockPath.DB_ONLY
    try:
        code, idx, remain = await lua.stock_lock_batch(keys=..., args=...)
    except (redis.ConnectionError, redis.TimeoutError):
        # ★ Redis 不可用：降级到 DB 直扣（DB 侧预占本来就在下单事务里执行）
        logger.error("Redis 不可用，降级到 DB 扣减", request_id=request_id)
        redis_degraded_counter.labels(path="stock").inc()
        # 降级路径必须有严格的限流保护（否则 DB 会被打垮），使用进程内令牌桶
        if not degrade_limiter.try_acquire():
            raise BizError(ErrorCode.SYSTEM_BUSY, "系统繁忙，请稍后再试")
        return LockPath.DB_ONLY
    ...
```

**降级限流器**：进程内令牌桶（Redis 已不可用，不能再依赖它限流），单 worker 20 QPS，总量 = 20 × worker 数。超出部分直接拒绝，保护 PG。

**Redis 客户端超时**：连接池设置 `socket_timeout=0.5`、`socket_connect_timeout=0.5`，`retry_on_timeout=False`。超时时间短，才能在 Redis 卡住时快速走降级，而不是把所有请求拖住。

## 6. Redis Streams 事件约定

outbox 投递任务（[13 §4](13-schema.md)）把 `local_message` 写入 `stream:{topic}`，worker 中的消费者以**消费组**方式读取：

```python
# app/worker/consumers.py
async def consume(redis: Redis, topic: str, group: str, handler: Handler) -> None:
    stream = f"stream:{topic}"
    with suppress(redis.ResponseError):                    # 消费组已存在
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    consumer = f"{socket.gethostname()}-{os.getpid()}"

    while True:
        # 先认领其他消费者超时未确认的消息（崩溃恢复），再读新消息
        _, claimed, _ = await redis.xautoclaim(stream, group, consumer, min_idle_time=60_000, count=50)
        fresh = await redis.xreadgroup(group, consumer, {stream: ">"}, count=50, block=5_000)
        for msg_id, fields in claimed + (fresh[0][1] if fresh else []):
            try:
                await handler(fields)                     # handler 内按 biz_key 幂等
                await redis.xack(stream, group, msg_id)
            except Exception:
                logger.exception("事件处理失败", topic=topic, msg_id=msg_id)
                await move_to_dead_letter_if_exhausted(redis, stream, group, msg_id, fields)
```

| 约定 | 说明 |
|---|---|
| 投递语义 | **至少一次**。消费者必须按消息中的 `biz_key` 幂等 |
| 消息体 | `{"bizKey": ..., "payload": "<json>", "ts": ...}` |
| 长度控制 | `XADD ... MAXLEN ~ 100000`，近似截断，性能好 |
| 死信 | 同一消息投递次数（`XPENDING` 中的 delivery count）超过 5 次，复制到 `stream:dead:{topic}` 并 `XACK` 原消息，运营后台展示并支持重放 |
| 顺序 | 同一 topic 内按写入顺序，但多消费者并行时不保证处理顺序；需要顺序的场景（如订单状态同步）用 `version` 字段丢弃旧消息 |

**消息不会因为 Redis 丢失而丢**：outbox 表才是消息的权威存储，`local_message.status` 只有在 `XADD` 成功后才置为已发送；即使 Redis 丢失了未消费的 Stream 数据，也可以按 `local_message` 重新投递。

## 7. 缓存穿透/击穿/雪崩防护

| 问题 | 场景 | 方案 |
|---|---|---|
| **穿透** | 查不存在的 SKU，每次都打 DB | 空值缓存（`cache:null:sku:{id}`，TTL 60s）；ID 格式校验（雪花 ID 的时间位必须合理） |
| **击穿** | 热点 SKU 的缓存同时失效，全部打 DB | 互斥锁重建（`SET lock:... NX EX 3`），抢不到锁的请求短暂等待后读缓存 |
| **雪崩** | 大批缓存同时失效 | TTL 加随机偏移（`300 + random.randint(0, 60)` 秒） |

**库存 key 的特殊处理**：库存 key **没有 TTL**，所以不存在"击穿/雪崩"。但需要处理**未初始化**的情况：

```python
async def ensure_stock_initialized(session_factory, redis: Redis, sku_id: int, wh_id: int) -> None:
    """Lua 返回 -1（未初始化）时调用：只让一个请求回源 DB 加载。"""
    lock_key = f"lock:stock_init:{sku_id}:{wh_id}"
    token = uuid4().hex
    if await redis.set(lock_key, token, nx=True, ex=3):
        try:
            if await redis.exists(keys.stock_shard(sku_id, wh_id, 0)):   # double check
                return
            async with session_factory() as session:
                stock = await inventory_repo.get_stock(session, sku_id, wh_id)
            await init_shards(redis, sku_id, wh_id, stock.available if stock else 0)
        finally:
            # 只删除自己加的锁（比较并删除，Lua 保证原子）
            await lua.release_lock(keys=[lock_key], args=[token])
    else:
        await asyncio.sleep(0.05)      # 没抢到锁，短暂等待后由调用方重试一次
```

`init_shards` 把可售量均分到各分片（余数放到 0 号分片），一次 pipeline 写入，`SET` 覆盖而非 `INCRBY`。

## 8. 大 key 治理

| 大 key 类型 | 阈值 | 治理 |
|---|---|---|
| `coupon:{tpl:X}:spu_set`（适用商品集合） | > 5000 元素 | 反转逻辑：存"排除集合"，默认全场可用 |
| `coupon:{tpl:X}:user_count`（用户领券计数） | > 100 万 field | 按 userId 拆成 16 个子 Hash：`coupon:{tpl:X}:user_count:{uid % 16}` |
| `stock:lock:*` / `stock:batch:*`（预占记录） | 单条不大，但数量多 | TTL 2 小时自动清理 |
| `seckill:{act:X}:queue` | > 10 万元素 | 入队脚本的 `ARGV[5]` 限制队列长度为库存 × 10 |
| `stream:*` | — | `MAXLEN ~` 截断 |

**检测命令**：

```bash
# 抽样统计大 key（内部用 SCAN，不会长时间阻塞，但会占用 CPU，低峰期执行）
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" --bigkeys
# 查看单个 key 的内存占用
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" MEMORY USAGE "coupon:{tpl:123}:user_count"
```

## 9. Redis 监控指标

第一期通过 `redis_exporter`（二期）或 worker 中的定时任务读取 `INFO` 写入日志与 `/metrics`：

| 指标 | 阈值 | 处理 |
|---|---|---|
| `used_memory` / `maxmemory` | > 80% | 排查大 key、缩短缓存 TTL，或扩容 |
| `evicted_keys` | > 0 | 配置为 `noeviction` 后应恒为 0；不为 0 说明配置被改，立即排查 |
| 写入报 `OOM command not allowed` | > 0 | 内存已满，应用已进入降级，立即扩容 |
| 命中率 `keyspace_hits / (hits + misses)` | < 90% | 检查缓存策略 |
| 慢日志 `SLOWLOG LEN` | > 10/分钟 | 排查大 key、复杂脚本 |
| `connected_clients` | > 500 | 检查连接池配置、连接泄漏 |
| `aof_last_bgrewrite_status` | ≠ ok | 检查磁盘空间 |
| Lua 脚本平均耗时 | > 5ms | 检查脚本逻辑、大集合操作 |
| Stream 消费组 `lag` | > 1000 | 消费者处理慢或挂掉，检查 worker |
