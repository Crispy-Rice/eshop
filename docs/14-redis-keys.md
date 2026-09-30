# 14 Redis Key 与 Lua 脚本汇总

## 1. Key 命名规范

```
{业务域}:{对象}:{标识}[:{子标识}]

业务域：stock / coupon / promo / order / cart / pay / user / idem / seckill / lock
标识：ID 或 业务号
```

**规范要求**：

| 要求 | 说明 |
|---|---|
| 所有库存类 key 带 `:shard:{n}` 后缀 | 支持分片，见 [03-inventory §3.3](03-inventory.md) |
| 所有 key 必须能通过 `SCAN` 模式批量清理 | 例如 `stock:sku:1001:wh:*` |
| 所有 key 必须设置 TTL（除持久性配置外） | 防止内存泄漏 |
| 禁止大 key（> 10KB） | 券的作用域集合超 5000 元素时改用更紧凑的结构或反转逻辑 |

## 2. Key 清单

### 2.1 库存

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `stock:sku:{skuId}:wh:{whId}:shard:{i}` | String(int) | 永久 | 分片可售库存，`i ∈ [0,8)` |
| `stock:sku:{skuId}:wh:{whId}:meta` | Hash | 永久 | `{total, available, locked, frozen, version, syncTs}` |
| `stock:lock:{orderSubNo}:{skuId}` | Hash | 2h | 预占记录：`{num, shard, orderNo, ts, status}` |
| `stock:warm:{skuId}` | String | 1h | 预热标记，避免每次下单都回源 DB |
| `stock:zero:{skuId}:{whId}` | String | 1h | 售罄标记，快速拒绝（避免查分片） |

**售罄快速拒绝的设计意图**：

```java
// 下单前先看售罄标记（一次 GET，比 MGET 8 个分片快）
if (redis.get("stock:zero:" + skuId + ":" + whId) != null) {
    throw new StockShortageException(skuId);   // 快速失败
}
```

**标记的设置与清除**：

```lua
-- 扣减脚本内：如果扣减后所有分片都是 0，设置售罄标记
-- 回补脚本内：删除售罄标记（DEL stock:zero:...）
```

### 2.2 优惠券

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `coupon:tpl:{tplId}:stock` | String(int) | 到期后 7d | 剩余可发量 |
| `coupon:tpl:{tplId}:user_count` | Hash | 活动结束后 30d | `{userId: 已领数}` |
| `coupon:tpl:{tplId}:meta` | Hash | 活动结束后 7d | 模板缓存：`{tplId,startTs,endTs,status,threshold,...}` |
| `coupon:tpl:{tplId}:warm` | String | 永久 | 预热标记 |
| `coupon:tpl:{tplId}:spu_set` | Set | 活动结束后 7d | 适用 SPU 集合（`scope_type=2` 且范围小时） |
| `coupon:tpl:{tplId}:sku_set` | Set | 活动结束后 7d | 适用 SKU 集合 |
| `coupon:code:{codeId}` | Hash | 到券有效期 | 券实例状态：`{userId, status, orderNo, validEnd}` |
| `coupon:user:{userId}` | Hash | 7d | 用户券 ID 列表缓存：`{codeId: status}` |
| `coupon:ip:{ip}:{tplId}` | String(int) | 1d | IP 维度领券计数 |
| `coupon:device:{deviceId}:{tplId}` | String(int) | 1d | 设备维度领券计数 |
| `coupon:pending:{userId}:{tplId}:{idem}` | String | 5m | 预发券标记（DB 异步生成期间） |

### 2.3 幂等

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `idem:{userId}:{uri}:{key}` | String | 5m | 值 = `PROCESSING` 或 响应 JSON |

### 2.4 秒杀排队

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `seckill:queue:{actId}` | List | 活动结束后 1h | 排队队列 |
| `seckill:uid_set:{actId}` | Set | 活动结束后 1d | 已参与用户（去重） |
| `seckill:result:{actId}:{userId}` | String | 5m | 排队结果 `SUCCESS/FAILED/ORDER_NO` |
| `seckill:stock:{actId}` | String(int) | 活动结束后 1d | 活动库存 |
| `seckill:token:{actId}` | String(int) | 永久 | 令牌桶剩余 |

### 2.5 商品与购物车缓存

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `sku:info:{skuId}` | Hash | 5m | SKU 详情（价格、重量、店铺） |
| `spu:info:{spuId}` | Hash | 5m | SPU 详情 |
| `spu:skus:{spuId}` | String(JSON) | 5m | SPU 下所有 SKU（规格选择器用） |
| `cart:{userId}` | Hash | 30d | 购物车项 |

### 2.6 分布式锁

| Key | 类型 | TTL | 说明 |
|---|---|---|---|
| `lock:stock:{skuId}:{whId}` | String | 3s | 库存初始化锁（回源 DB 时用） |
| `lock:seckill:consumer:{actId}` | String | 30s | 秒杀消费者选主 |
| `lock:settle:{shopId}:{date}` | String | 1h | 结算任务互斥 |

## 3. 核心 Lua 脚本

### 3.1 库存扣减（含幂等）

```lua
-- ============================================================
-- 库存预占
-- KEYS[1] = stock:sku:{skuId}:wh:{whId}:shard:{i}
-- KEYS[2] = stock:lock:{orderSubNo}:{skuId}
-- KEYS[3] = stock:zero:{skuId}:{whId}
-- ARGV[1] = qty
-- ARGV[2] = lock TTL 秒
-- ARGV[3] = orderSubNo
-- ARGV[4] = 当前时间戳
-- 返回 {code, remain}
--   1 成功 / 0 库存不足 / 2 幂等命中 / -1 未初始化 / 3 整片售罄
-- ============================================================

local qty      = tonumber(ARGV[1])
local lockKey  = KEYS[2]
local zeroKey  = KEYS[3]

-- ① 幂等：同一 (子单, SKU) 已预占过
local lockedNum = redis.call('HGET', lockKey, 'num')
if lockedNum then
    return {2, tonumber(lockedNum)}
end

-- ② 快速失败：整片已售罄
if redis.call('EXISTS', zeroKey) == 1 then
    return {3, 0}
end

-- ③ 库存检查与扣减
local stock = redis.call('GET', KEYS[1])
if not stock then
    return {-1, 0}                     -- 未初始化，调用方需回源 DB 后重试
end

stock = tonumber(stock)
if stock < qty then
    return {0, stock}
end

redis.call('DECRBY', KEYS[1], qty)

-- ④ 写预占记录
redis.call('HSET', lockKey,
    'num', qty,
    'shard', KEYS[1],
    'orderNo', ARGV[3],
    'ts', ARGV[4],
    'status', 'LOCKED')
redis.call('EXPIRE', lockKey, tonumber(ARGV[2]))

return {1, stock - qty}
```

### 3.2 库存回补

```lua
-- ============================================================
-- 库存回补（取消/超时/失败）
-- KEYS[1] = stock:lock:{orderSubNo}:{skuId}
-- KEYS[2] = stock:zero:{skuId}:{whId}
-- ARGV[1] = 当前时间戳
-- 返回 {code, qty}
--   1 回补成功 / 2 幂等（已回补）/ 0 无预占记录
-- ============================================================

local lockKey = KEYS[1]
local status = redis.call('HGET', lockKey, 'status')
if not status then
    return {0, 0}
end
if status == 'RELEASED' then
    return {2, 0}                      -- 已回补，幂等
end
if status == 'CONFIRMED' then
    return {2, 0}                      -- 已转实扣，不能再回补
end

local qty   = tonumber(redis.call('HGET', lockKey, 'num'))
local shard = redis.call('HGET', lockKey, 'shard')

-- 回补到原分片
redis.call('INCRBY', shard, qty)
redis.call('HSET', lockKey, 'status', 'RELEASED', 'releaseTs', ARGV[1])

-- 清除售罄标记（有货了）
redis.call('DEL', KEYS[2])

return {1, qty}
```

### 3.3 库存实扣（预占转已售）

```lua
-- ============================================================
-- 支付成功后：预占转实扣
-- KEYS[1] = stock:lock:{orderSubNo}:{skuId}
-- 返回 {code, qty}
--   1 成功 / 2 幂等 / 0 无记录
-- ============================================================

local lockKey = KEYS[1]
local status = redis.call('HGET', lockKey, 'status')
if not status then return {0, 0} end
if status == 'CONFIRMED' then return {2, 0} end
if status == 'RELEASED' then
    -- ★ 异常：已回补的预占又要求实扣（时序错乱）
    return {-1, 0}
end

local qty = tonumber(redis.call('HGET', lockKey, 'num'))
redis.call('HSET', lockKey, 'status', 'CONFIRMED')
return {1, qty}
```

### 3.4 领券（完整版）

见 [04-coupon §4](04-coupon.md)，此处不重复。

### 3.5 券锁定

```lua
-- ============================================================
-- 锁券（订单占用）
-- KEYS[1] = coupon:code:{codeId}
-- KEYS[2] = coupon:user:{userId}
-- ARGV[1] = orderMainNo
-- ARGV[2] = userId
-- ARGV[3] = 锁定时长（秒）
-- ARGV[4] = 当前时间戳（秒）
-- 返回 code: 1成功 / 2已使用 / 3被其他订单锁定 / 4已过期 / 0券不存在
-- ============================================================

local codeKey = KEYS[1]
if redis.call('EXISTS', codeKey) == 0 then
    return {0}
end

local status  = redis.call('HGET', codeKey, 'status')
local orderNo = redis.call('HGET', codeKey, 'orderNo')
local validEnd = tonumber(redis.call('HGET', codeKey, 'validEnd') or '0')
local nowTs   = tonumber(ARGV[4])

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

redis.call('HSET', codeKey, 'status', 'LOCKED', 'orderNo', ARGV[1], 'lockTs', ARGV[4])
redis.call('EXPIRE', codeKey, tonumber(ARGV[3]))

-- 同步更新用户的券列表缓存
if redis.call('EXISTS', KEYS[2]) == 1 then
    redis.call('HSET', KEYS[2], ARGV[2] .. ':' .. ARGV[1], 'LOCKED')
end

return {1}
```

### 3.6 秒杀入队

```lua
-- ============================================================
-- 秒杀入队
-- KEYS[1] = seckill:queue:{actId}
-- KEYS[2] = seckill:uid_set:{actId}
-- KEYS[3] = seckill:stock:{actId}
-- ARGV[1] = userId
-- ARGV[2] = skuId
-- ARGV[3] = num
-- ARGV[4] = requestId
-- ARGV[5] = 队列最大长度
-- ARGV[6] = 每人限购数
-- ARGV[7] = 当前时间戳
-- 返回 {code, data}
--   1 入队成功(返回排位) / 2 已参与 / 3 队列已满 / 4 已售罄
-- ============================================================

local queueKey = KEYS[1]
local uidKey   = KEYS[2]
local stockKey = KEYS[3]
local userId   = ARGV[1]

-- ① 库存已空，直接拒绝（避免无意义排队）
local stock = tonumber(redis.call('GET', stockKey) or '0')
if stock <= 0 then
    return {4, 0}
end

-- ② 限购校验
local myCount = tonumber(redis.call('HGET', uidKey, userId) or '0')
if myCount >= tonumber(ARGV[6]) then
    return {2, myCount}
end

-- ③ 队列长度限制
local qlen = redis.call('LLEN', queueKey)
if qlen >= tonumber(ARGV[5]) then
    return {3, qlen}
end

-- ④ 入队
local payload = cjson.encode({
    uid = userId, sku = ARGV[2], num = tonumber(ARGV[3]),
    req = ARGV[4], ts = tonumber(ARGV[7])
})
redis.call('LPUSH', queueKey, payload)
redis.call('HINCRBY', uidKey, userId, tonumber(ARGV[3]))

local position = redis.call('LLEN', queueKey)
return {1, position}
```

### 3.7 秒杀出队消费

```lua
-- ============================================================
-- 秒杀消费（消费者调用）
-- KEYS[1] = seckill:queue:{actId}
-- KEYS[2] = seckill:stock:{actId}
-- ARGV[1] = 批量大小
-- 返回 出队的元素数组（每个是 JSON 字符串）
-- ============================================================

local queueKey = KEYS[1]
local stockKey = KEYS[2]
local batch    = tonumber(ARGV[1])

local result = {}
for i = 1, batch do
    local item = redis.call('RPOP', queueKey)
    if not item then break end

    local data = cjson.decode(item)
    local stock = tonumber(redis.call('GET', stockKey) or '0')

    if stock >= data.num then
        redis.call('DECRBY', stockKey, data.num)
        data.ok = true
    else
        data.ok = false
        -- ★ 库存耗尽：清空队列，剩余全部失败
        local rest = redis.call('LRANGE', queueKey, 0, -1)
        for _, r in ipairs(rest) do
            local d = cjson.decode(r)
            d.ok = false
            table.insert(result, cjson.encode(d))
        end
        redis.call('DEL', queueKey)
        table.insert(result, cjson.encode(data))
        break
    end
    table.insert(result, cjson.encode(data))
end

return result
```

**注意**：这个脚本处理的是"批量出队 + 扣库存"，一次网络往返处理 batch 个请求。相比每个请求一次 Redis 往返，吞吐提升 batch 倍。

## 4. Redis 集群与容量规划

### 4.1 分片策略

| 业务 | 分片数 | 说明 |
|---|---|---|
| 库存 | 8 分片/SKU | 散列热点，见 [03-inventory §3.3](03-inventory.md) |
| 券 | 按模板天然分散 | 一个模板的 key 集中在少数节点，容量不大 |
| 秒杀队列 | 按活动分散 | 单活动一个 List，单 key 热点——用 Redis Cluster 的 hash tag |

**热点 List 的优化**：单个活动的队列可能上百万元素。优化：**分段队列**。

```
seckill:queue:{actId}:0
seckill:queue:{actId}:1
...
seckill:queue:{actId}:15

入队：随机选一个段（或按 userId % 16）
消费：轮询各段
→ 分散到 16 个 key，缓解单 key 热点
```

**代价**：队列不再严格 FIFO。对秒杀来说可以接受（本来也不承诺先到先得，因为网络延迟差异远大于队列顺序差异）。

### 4.2 内存估算

| Key 类型 | 单条大小 | 数量 | 总内存 |
|---|---|---|---|
| 库存分片 | ~100 B | 1 亿 SKU × 8 片 | ~80 GB |
| 库存 meta | ~200 B | 1 亿 | ~20 GB |
| 预占记录 | ~300 B | 峰值 100 万 | ~300 MB |
| 券实例 | ~200 B | 峰值 1 亿 | ~20 GB |
| 券 user_count | ~50 B/field | 1 亿 field | ~5 GB |
| 幂等键 | ~500 B | 峰值 10 万 | ~50 MB |
| 商品缓存 | ~2 KB | 100 万 SKU | ~2 GB |

**总计约 130 GB**。规划：Redis Cluster 12 节点（6 主 6 从），每主 32 GB 内存（实际使用约 22 GB，留 30% 余量）。

**库存全部放 Redis 是否必要**：1 亿 SKU 的库存全量缓存代价高。**优化**：只缓存"活跃 SKU"（近 30 天有销量或浏览的商品，约 1000 万），其余走 DB（长尾商品无并发，DB 完全够用）。

```
判断：下单时先查 redis EXISTS stock:sku:{id}:...
  ├─ 存在 → 走 Redis 路径
  └─ 不存在 → 走 DB 路径（带本地锁 + DB 条件更新）
```

## 5. Redis 高可用与故障处理

### 5.1 持久化配置

```ini
# AOF：保证数据不丢（库存和券不能丢）
appendonly yes
appendfsync everysec      # 每秒 fsync，最多丢 1 秒数据
no-appendfsync-on-rewrite yes

# RDB：用于快速恢复
save 900 1
save 300 10
save 60 10000

# 混合持久化
aof-use-rdb-preamble yes
```

**`everysec` 的后果**：Redis 宕机最多丢 1 秒数据。1 秒内的库存扣减丢失 → Redis 库存偏高 → 有超卖风险。

**应对**：**DB 侧条件更新是最终的防线**（见 [03-inventory §5](03-inventory.md)）。Redis 数据丢失只影响"拦截效率"，不影响正确性。

### 5.2 故障降级

```java
public LockResult lock(Long skuId, Long whId, int num) {
    try {
        return redisLockService.lock(skuId, whId, num);
    } catch (RedisConnectionFailureException | RedisSystemException e) {
        // ★ Redis 不可用：降级到 DB 直扣
        log.error("Redis 不可用，降级到 DB 扣减: skuId={}", skuId, e);
        metrics.counter("redis.degraded.stock").increment();

        // 降级路径必须有严格的限流保护（否则 DB 会被打垮）
        if (!degradeRateLimiter.tryAcquire()) {
            throw new ServiceBusyException("系统繁忙，请稍后再试");
        }
        return dbLockService.lock(skuId, whId, num);
    }
}
```

**降级限流器的配置**：DB 直扣的 QPS 上限设为 DB 能承受的 50%（如 200 QPS）。超出部分直接拒绝，保护 DB。

**降级放行的顺序**：`Redis 扣减 → 熔断器打开 → 限流放行 200 QPS → DB 扣减 → 其余拒绝`。

### 5.3 主从切换的数据一致性

Redis 主从是**异步复制**。主节点宕机时，未同步到从节点的写入会丢失。

**风险场景**：

```
① 主节点执行 DECRBY 库存 -1（成功响应客户端）
② 未同步到从节点
③ 主节点宕机
④ 从节点提升为主（此时库存还是旧值，多了 1）
⑤ → 该商品可能超卖 1 件
```

**缓解措施**：

| 措施 | 说明 |
|---|---|
| `WAIT` 命令 | `WAIT 1 100` 确保至少 1 个从节点确认，牺牲延迟换一致性 |
| DB 兜底 | 最终防线（本方案依赖此条） |
| 对账 | 每 1 分钟对账，快速发现漂移 |
| 大促期间禁切换 | 运维规范 |

**`WAIT` 的取舍**：

```java
// 对高价值商品（如手机）使用 WAIT，牺牲 2-5ms 延迟换强一致
if (isHighValue(skuId)) {
    redisTemplate.execute((RedisCallback<Void>) conn -> {
        conn.decrBy(key.getBytes(), num);
        conn.execute("WAIT", "1".getBytes(), "100".getBytes());
        return null;
    });
}
```

**结论**：**不依赖 Redis 的强一致**，把 DB 作为唯一真相源。Redis 丢数据最多导致"少卖"（用户以为售罄实际有货），对账任务会修正。

## 6. Redis 监控指标

| 指标 | 阈值 | 处理 |
|---|---|---|
| 内存使用率 | > 80% | 扩容或清理 |
| 命中率 | < 90% | 检查缓存策略 |
| 慢查询数 | > 10/分钟 | 排查大 key、`KEYS *` |
| 连接数 | > 8000 | 检查连接池配置、连接泄漏 |
| 主从延迟 | > 1s | 检查网络、复制积压 |
| `evicted_keys` | > 0 | ★ 内存不足在淘汰 key！立即扩容 |
| `blocked_clients` | > 100 | 检查阻塞命令 |
| Lua 脚本平均耗时 | > 5ms | 检查脚本逻辑、大集合操作 |

**`evicted_keys > 0` 是最危险的信号**：说明内存不足，Redis 在主动淘汰 key。库存 key 被淘汰 = 库存数据丢失 = 可能超卖。**必须立即扩容**。

**防护**：库存 key 设置 `no-eviction` 策略或单独部署实例。

```ini
maxmemory-policy noeviction     # 库存实例：不淘汰，写满就报错（宁可拒绝服务）
# 或
maxmemory-policy volatile-lru   # 混合实例：只淘汰有 TTL 的 key
```

## 7. 缓存穿透/击穿/雪崩防护

| 问题 | 场景 | 方案 |
|---|---|---|
| **穿透** | 查不存在的 SKU，每次都打 DB | 布隆过滤器 + 空值缓存（`sku:null:{id}`，TTL 60s） |
| **击穿** | 热点 SKU 的缓存同时失效，全部打 DB | 互斥锁重建（`lock:stock:init:{skuId}`）+ 逻辑过期 |
| **雪崩** | 大批缓存同时失效 | TTL 加随机偏移（`5m + random(0,60s)`） |

**库存 key 的特殊处理**：库存 key **没有 TTL**（永久），所以不存在"击穿/雪崩"。但需要处理**未初始化**的情况：

```lua
-- 脚本返回 -1（未初始化）时
if (result[0] == -1) {
    // 用分布式锁回源 DB，只让一个线程去加载
    RLock lock = redisson.getLock("lock:stock:init:" + skuId + ":" + whId);
    if (lock.tryLock(100, TimeUnit.MILLISECONDS)) {
        try {
            // double check
            if (redisTemplate.hasKey(stockKey)) return retry();
            // 从 DB 加载并写入各分片
            SkuStock stock = stockMapper.select(skuId, whId);
            initShards(skuId, whId, stock.getAvailable());
            return retry();
        } finally {
            lock.unlock();
        }
    } else {
        // 没抢到锁，短暂等待后重试
        Thread.sleep(50);
        return retry();
    }
}
```

## 8. 幂等脚本模板

```lua
-- 通用幂等脚本
-- KEYS[1] = 幂等键
-- ARGV[1] = 结果 TTL
-- ARGV[2] = 处理中标记的 TTL
-- 返回 {1} 首次（可处理） / {2, 缓存结果} 已完成 / {3} 处理中
local v = redis.call('GET', KEYS[1])
if not v then
    redis.call('SET', KEYS[1], 'PROCESSING', 'EX', tonumber(ARGV[2]))
    return {1}
end
if v == 'PROCESSING' then
    return {3}
end
return {2, v}
```

**三步式处理**：

```java
// ① 占位
Long code = (Long) redisTemplate.execute(idemScript, List.of(key), ttl, processTtl);
if (code == 2) return cachedResult;      // 已完成
if (code == 3) throw new ProcessingException();

// ② 处理业务
try {
    Object result = doBusiness();
    // ③ 写结果
    redisTemplate.opsForValue().set(key, JSON.toJSONString(result), ttl, SECONDS);
    return result;
} catch (BusinessException e) {
    redisTemplate.delete(key);            // 业务失败，允许重试
    throw e;
} catch (Exception e) {
    // ★ 系统异常：不删除 key，保持 PROCESSING 状态
    //   让用户看到"处理中"而不是立即重试（可能造成重复）
    throw e;
}
```

## 9. 大 key 治理

| 大 key 类型 | 阈值 | 治理 |
|---|---|---|
| `coupon:tpl:{id}:spu_set`（适用商品集合） | > 5000 元素 | 反转逻辑：存"排除集合"，或改用布隆过滤器 |
| `coupon:tpl:{id}:user_count`（用户领券计数） | > 100 万 field | 按 userId 哈希拆成 16 个子 Hash：`coupon:tpl:{id}:user_count:{uid%16}` |
| `stock:lock:*`（预占记录） | 单条不大，但数量多 | TTL 2 小时自动清理 |
| `seckill:queue:{actId}` | > 100 万元素 | 分段队列（见 §4.1） |

**检测命令**：

```bash
# 扫描大 key（生产环境慎用，会阻塞）
redis-cli --bigkeys

# 更好的方式：用 redis-cli --memkeys 或监控工具（如 RedisInsight）
```

**`--bigkeys` 的坑**：它用 `SCAN` 遍历所有 key，在千万级 key 的实例上会跑很久且消耗 CPU。**生产环境应该在从节点上跑，或在低峰期跑**。
