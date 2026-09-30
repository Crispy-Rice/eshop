# 03 库存设计：排队削峰与超发防护

## 1. 问题定义

**超发（超卖）**：SKU 实际库存 100 件，却卖出了 101 件。

三个典型成因：

| 成因 | 场景 | 表现 |
|---|---|---|
| **并发覆盖** | 两个请求同时 `SELECT available` 得 1，各自判断"够"，各自 `UPDATE available = 1 - 1 = 0` | 卖出 2 件，库存只减 1 |
| **缓存与 DB 不一致** | Redis 显示有货，DB 已扣完（Redis 未及时同步） | 前端可下单，后端口头失败 |
| **超时未回补 / 重复回补** | 订单取消回补了两次，或从不回补 | 库存虚高（超发）或虚低（少卖） |

**本方案的目标**：在 5000 QPS 峰值的秒杀场景下，上述三种情况都不发生，且不牺牲吞吐。

## 2. 核心策略：排队（令牌制）

### 2.1 为什么是排队

秒杀的本质矛盾是"瞬间请求量 >> 库存量"。传统做法（直接扣 Redis）会把 100% 的请求都送到扣减逻辑，999 个失败请求也消耗了 Redis 的 CPU 和网络往返。

**排队把"随机并发抢"变成"有序消费"**：

- 用户点击 → 先进入排队队列（拿到序号/令牌）
- 队列消费端按序处理，处理到库存耗尽时，**直接给剩余排队者返回"已售罄"，连 Redis 扣减都不用走**
- 单位时间内进入扣减逻辑的请求量被严格控制在库存量级别

效果：Redis QPS 从 5000 降到 ~200（库存数 + 少量冗余），失败请求的成本从"一次 Redis 往返"降到"一次内存队列写入"。

### 2.2 两种排队实现

#### 方案 A：Redis List/Stream 队列 + 消费者（推荐，用于秒杀）

```
用户点击购买
   │
   ├─[Lua] 检查：该用户是否已在队列中 / 是否已抢到
   ├─[Lua] LPUSH seckill:queue:{activityId} {userId, skuId, num, requestId, ts}
   ├─[Lua] 返回队列长度（= 当前排位）
   │
   └─ 返回前端："排队中，前方还有 137 人"（前端轮询/WebSocket 推送结果）

消费者（独立线程池，按活动分片消费）：
   while (true) {
       item = RPOP seckill:queue:{activityId}
       if (item == null) continue;
       // 此时才真正进入扣减逻辑
       result = tryDeduct(item.skuId, item.num)
       // 写结果到 Redis，用户轮询可查
       SET seckill:result:{activityId}:{userId} = SUCCESS/FAILED EX 300
       if (result == SUCCESS) {
           创建订单（或发 MQ 让交易服务创建）
       } else {
           // 库存已耗尽：批量给队列剩余元素返回失败，清空队列
           drainAndFail(activityId)
           break;
       }
   }
```

**关键设计点**：

1. **入队是原子的，出队是原子的**：`LPUSH`/`RPOP` 天然原子，无需额外锁。
2. **同一用户去重**：用 `HSETNX seckill:uid_set:{activityId} {userId} 1` 保证一人一单（或限购 N 单）。在入队前做，避免刷子把队列塞满。
3. **队列有界**：`LLEN` 超过阈值（如库存 × 10）时，`Lua` 内直接返回"排队已满"。防止队列无限增长把 Redis 内存打爆。
4. **令牌与库存的配比**：发放的令牌数 > 库存数（打余量给未支付回补的），但队列长度上限受上面第 3 点控制。
5. **消费者多实例**：同一活动只能被一个消费者消费（否则顺序失效）→ 用 Redis 分布式锁选主，或按 `activityId % N` 分片（同一活动固定落到一个消费者）。
6. **结果回传**：用户轮询 `seckill:result:{activityId}:{userId}`，或长连接推送。轮询间隔建议 1s，最多 30 次。

#### 方案 B：Redis 令牌桶 + 信号量（用于常态限流下单）

非秒杀的普通商品下单，不排队，但**控制并发**：

```
[Lua] 获取令牌：if (GET rate:speed:{skuId} > 0) DECR; else 拒绝
     → 令牌数 = 库存量的 3 倍（够支付失败回补）
处理完（成功或失败）异步归还令牌（仅在扣减失败时归还）
```

> 选型结论：**秒杀/大促热点 SKU 用方案 A，日常下单用方案 B，长尾商品直接走 Redis Lua 扣减不排队**。
> 判断"热点"的方式：运营在活动配置里标记，或系统自动识别（1 分钟内扣减请求 > 1000 的 SKU 自动升级为排队模式）。

### 2.3 排队对用户体验的处理

排队必须**有明确的反馈和边界**，否则用户会重复点击：

| 状态 | 前端展示 | 后端行为 |
|---|---|---|
| 排队中 | "排队中，前方 137 人，预计 8 秒" | 轮询结果接口 |
| 抢到 | "抢购成功，正在创建订单" | 返回 orderNo 或"订单创建中" |
| 售罄 | "已售罄，看看其他商品" | 返回 `SECKILL_SOLD_OUT` |
| 超时 | "排队超时，请重试" | 30s 未出队则前端放弃，后端仍会处理但用户可查订单 |
| 已抢过 | "您已参与过本次抢购" | 返回 `ALREADY_PURCHASED` |

## 3. 库存模型

### 3.1 状态流转

```
            下单预占               支付成功              发货
available ──────────> locked ──────────> frozen ──────────> 扣减 total
   ↑                     │                                      │
   │     超时取消/主动取消 │                                      │ 退货入库
   └─────────────────────┘                                      │
   └──────────────────────────────────────────────────────────────┘
```

| 状态 | 含义 | 谁写入 |
|---|---|---|
| `available` | 可售，能被新订单预占 | 商家设置/回补 |
| `locked` | 已下单未支付，预占 | 下单成功 |
| `frozen` | 已支付待发货，实扣 | 支付回调 |
| `total` | 总量，发货后真正减少 | 发货 + 退货入库 |

**恒等式**：`total = available + locked + frozen`（发货时 `total -= n`，同时 `frozen -= n`，等式保持）。

### 3.2 Redis 结构

```
# SKU 在某仓的可售库存（分片存储）
stock:sku:{skuId}:wh:{warehouseId}:shard:{0..7}   → int（该分片的可售量）
stock:sku:{skuId}:wh:{warehouseId}:meta           → hash{total, available, locked, frozen, version, sync_ts}

# 预占记录（用于回补追溯）
stock:lock:{orderSubNo}:{skuId}  → hash{num, warehouseId, ts, status}  EX 2h

# 秒杀队列与令牌
seckill:queue:{activityId}
seckill:uid_set:{activityId}
seckill:result:{activityId}:{userId}
```

### 3.3 分片设计

单 SKU 热点是 Redis Cluster 的经典痛点——同一个 key 永远落在一个节点上。做法：

```
分片数 N = 8（可配）
shardIdx = hash(skuId + userId) % N   ← 用 userId 参与散列，让同一用户固定落一片
可用性判断：任一 shard > 0 即可下单（或要求指定 shard > 0）
```

**为什么用 userId 参与散列而不是纯随机**：
- 纯随机：用户 A 第一次落 shard 3 失败，第二次落 shard 5 成功 —— 体验不一致且难以排查。
- userId 散列：同一用户固定落一片，只有该片售罄才失败。配合"回落"逻辑（本片售罄时按顺序试其他片），体验和容量兼顾。

**读取时**：`MGET` 一次拿全部 8 片，求和展示给用户（"剩余 37 件"）。
**扣减时**：只操作目标分片。

## 4. Redis Lua 扣减脚本（核心）

```lua
-- KEYS[1] = stock:{skuId}:wh:{whId}:shard:{i}
-- KEYS[2] = stock:lock:{orderSubNo}:{skuId}
-- ARGV[1] = 需要扣减的数量
-- ARGV[2] = 预占记录 TTL（秒）
-- ARGV[3] = orderSubNo（用于写预占记录）
-- ARGV[4] = 当前时间戳（由应用传入）
-- 幂等依据：KEYS[2] 预占记录是否已存在（同一子单+SKU 只扣一次）
-- 返回：{1, remain} 成功 / {0, 0} 库存不足 / {2, remain} 幂等命中（已扣过）

local qty = tonumber(ARGV[1])
local lockKey = KEYS[2]

-- ① 幂等检查：同一个 (子单, SKU) 是否已经预占过
local existing = redis.call('HGET', lockKey, 'num')
if existing then
    return {2, tonumber(existing)}
end

-- ② 库存检查与扣减（单命令内完成，天然原子）
local stock = tonumber(redis.call('GET', KEYS[1]) or '-1')
if stock < 0 then
    return {-1, 0}          -- 库存未初始化，需回源 DB 加载
end
if stock < qty then
    return {0, stock}       -- 不足
end

redis.call('DECRBY', KEYS[1], qty)

-- ③ 写预占记录，供回补与对账使用
redis.call('HSET', lockKey,
    'num', qty,
    'shard', KEYS[1],
    'orderNo', ARGV[3],
    'ts', ARGV[4],
    'status', 'LOCKED')
redis.call('EXPIRE', lockKey, tonumber(ARGV[2]))

return {1, stock - qty}
```

**审计要求**：脚本过程中生成一条 `stock_flow` 流水（异步写 DB），记录 `(skuId, orderSubNo, changeType, num, before, after, ts)`。这是事后排查超卖的唯一可靠依据。

## 5. DB 侧兜底扣减

Redis 无论多可靠，都要假设它会出错（主从切换丢写、内存淘汰、脚本 Bug）。所以 DB 必须二次校验：

```sql
-- 预占（下单时）
UPDATE sku_stock
SET available = available - #{num},
    locked    = locked + #{num},
    version   = version + 1,
    updated_at = NOW(3)
WHERE sku_id = #{skuId}
  AND warehouse_id = #{whId}
  AND available >= #{num};
-- RowsAffected == 0 → 预占失败 → 触发 Redis 回补 + 订单创建失败
```

```sql
-- 实扣（支付成功时）
UPDATE sku_stock
SET locked = locked - #{num},
    frozen = frozen + #{num},
    version = version + 1
WHERE sku_id = #{skuId} AND warehouse_id = #{whId}
  AND locked >= #{num};
```

```sql
-- 发货（WMS 回传时）
UPDATE sku_stock
SET frozen = frozen - #{num},
    total  = total - #{num},
    version = version + 1
WHERE sku_id = #{skuId} AND warehouse_id = #{whId}
  AND frozen >= #{num};
```

```sql
-- 取消/超时回补（从未支付）
UPDATE sku_stock
SET available = available + #{num},
    locked    = locked - #{num},
    version = version + 1
WHERE sku_id = #{skuId} AND warehouse_id = #{whId}
  AND locked >= #{num};
```

**每条 SQL 都带业务条件**（`available >= n`、`locked >= n`），这不是装饰——它是防止"重复回补导致库存虚高"的最后防线。即使 MQ 投递了两次取消消息，第二次 `locked >= n` 也会失败。

### 5.1 防死锁：加锁顺序

批量预占多个 SKU 时，**必须按 `(warehouse_id, sku_id)` 升序排列**后再依次更新。否则两个订单以相反顺序锁同一组 SKU 会死锁。

```java
items.sort(comparing(Item::getWarehouseId).thenComparing(Item::getSkuId));
for (Item it : items) {
    int rows = stockMapper.lock(it);       // 每行独立 UPDATE，短事务
    if (rows == 0) { throw new StockShortageException(it); }
}
```

事务保持**尽可能短**：预占操作不加任何远程调用，不写日志表，只更新库存行。审计流水走异步。

## 6. 回补的触发点

| 触发点 | 回补量 | 幂等键 | 备注 |
|---|---|---|---|
| 订单超时未支付（30min） | locked → available | `cancel:{orderSubNo}` | 延迟消息 + 定时扫描双保险 |
| 用户主动取消 | locked → available | `cancel:{orderSubNo}` | 立即回补 |
| 支付失败 | locked → available | `payfail:{payNo}` | 由支付服务发事件 |
| 预占成功但订单写库失败 | Redis 侧回补 | `rollback:{requestId}` | 补偿事务 |
| 退货入库 | total += n（质检合格） | `return:{refundNo}` | **这是退货的唯一回补点**，详见 [08](08-aftersale.md) |
| 退货质检不合格 | 不回补 | - | 商品报废，库存不恢复 |

**回补必须是"从 Redis 和 DB 各减一次"**：Redis `INCRBY` 恢复分片库存，DB `UPDATE` 恢复 `available`。两者用同一个幂等键，重复执行无副作用。

## 7. 定时对账：修正漂移

每 5 分钟跑一次（大促期间 1 分钟）：

```
1. 从 DB 读一批 sku_stock（version 变化过的，或全量抽样）
2. 计算 Redis 应存量 = sum(各分片)
3. 对比：
   - Redis 值 == DB.available → 正常
   - Redis 值 > DB.available  → 危险（会超卖）→ 以 DB 为准 SET，并告警
   - Redis 值 < DB.available  → 少卖（用户看到售罄但实际有货）→ 以 DB 为准 SET，记录损失
4. 校验恒等式 total == available + locked + frozen
5. 全量重建：每天凌晨低峰期，把 DB 的 available 重新推到 Redis（SET 而非 INCR）
```

**告警阈值**：单次对账发现 > 10 个 SKU 不一致，立即告警到值班群。

## 8. 库存流水分表

```sql
CREATE TABLE `stock_flow` (
  `id`             BIGINT      NOT NULL AUTO_INCREMENT,
  `sku_id`         BIGINT      NOT NULL,
  `warehouse_id`   BIGINT      NOT NULL,
  `order_no`       VARCHAR(32) DEFAULT NULL,
  `change_type`    TINYINT     NOT NULL COMMENT '1预占 2实扣 3回补 4发货扣减 5退货入库 6手工调整 7初始化',
  `num`            INT         NOT NULL COMMENT '正数增加，负数减少',
  `before_qty`     INT         NOT NULL COMMENT '变更前可售量',
  `after_qty`      INT         NOT NULL COMMENT '变更后可售量',
  `biz_key`        VARCHAR(64) NOT NULL COMMENT '幂等键',
  `operator`       VARCHAR(64) DEFAULT NULL COMMENT '操作人/系统',
  `remark`         VARCHAR(255) DEFAULT NULL,
  `created_at`     DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_biz_key` (`biz_key`) COMMENT '★ 幂等的最终保证',
  KEY `idx_sku_time` (`sku_id`, `created_at`)
) ENGINE=InnoDB COMMENT='库存流水'
PARTITION BY RANGE (TO_DAYS(`created_at`)) (
  PARTITION p202609 VALUES LESS THAN (TO_DAYS('2026-10-01')),
  PARTITION p202610 VALUES LESS THAN (TO_DAYS('2026-11-01'))
);
```

**`uk_biz_key` 是防重复回补的终极武器**：`biz_key` 形如 `RETURN:{refundNo}:{skuId}`，即使所有上游幂等都失效，唯一索引也会拦住第二次写入。

## 9. 库存展示与"少卖"的取舍

| 场景 | 展示策略 | 理由 |
|---|---|---|
| 库存 > 100 | 只显示"有货" | 避免竞品爬取真实库存 |
| 10 < 库存 ≤ 100 | 显示"仅剩 87 件" | 制造紧迫感，提升转化 |
| 0 < 库存 ≤ 10 | 显示"仅剩 3 件" | 强紧迫感 |
| 库存 = 0 | "已售罄" | - |

**真实的低库存提示是双刃剑**：提醒用户的同时，也暴露了真实数据给爬虫和竞品。方案：**展示值 = 真实值向上取整到有意义的档位**（真实 3 → 展示"仅剩 5 件以内"），既保留紧迫感又模糊真实值。

## 10. 边界场景清单

| 场景 | 处理 |
|---|---|
| 用户下单 10 件，Redis 只有 7 件 | 整单失败（不支持部分成功），返回 `STOCK_INSUFFICIENT` 带可用量提示 |
| 用户下单 10 件，Redis 分片 A 有 3、分片 B 有 8 | 从 B 扣 7 + 从 A 扣 3？→ **不允许跨分片凑**。整单落到 B（按 userId 散列的片），失败则回落到其他片整片扣；都用整片，不拆分 |
| 预占成功，但订单落库失败 | 补偿：删除 Redis 预占记录 + `INCRBY` 恢复 + DB 回滚（本地事务已回滚）。由 requestId 幂等 |
| Redis 主从切换丢失预占 | DB 仍有 `locked`，对账任务按 DB 重建 Redis。**DB 是账本** |
| 商家突发下架 SKU 但有 5 个 locked | 允许已预占的订单继续支付（不强制取消），只影响新订单 |
| 退货入库，但该 SKU 已下架 | 库存照常回补（`available += n`），但不下发到 Redis 前端展示（下架商品不展示库存） |
| 负数库存告警 | 任何 SQL 执行后 `available < 0` → 立即触发告警 + 冻结该 SKU 下单 |

## 11. 与其它模块的接口

| 调用方 | 接口 | 语义 |
|---|---|---|
| 交易服务 | `POST /inventory/lock` | 批量预占，`bizKey` 幂等，返回逐项结果 |
| 交易服务 | `POST /inventory/release` | 释放预占（取消/超时） |
| 支付服务 | `POST /inventory/confirm` | 预占转实扣（`locked→frozen`） |
| WMS | `POST /inventory/deliver` | 发货，`frozen→` 扣 `total` |
| 售后 | `POST /inventory/return-in` | 退货入库回补（**唯一**退货回补入口） |
| 商品 | `POST /inventory/init` | 初始化/覆盖库存（幂等 SET） |
| 商家后台 | `POST /inventory/adjust` | 手工调整，需审批流 |

**所有接口都必须接受 `bizKey` 参数**，没有例外。这是把幂等责任放在最底层的设计选择——底层可靠，上层就可以简化。
