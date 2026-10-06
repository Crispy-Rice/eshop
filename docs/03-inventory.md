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

消费者（worker 容器中的 asyncio 任务，每个活动一个协程）：
```

```python
async def consume_seckill(activity_id: int, redis: Redis) -> None:
    queue = f"seckill:queue:{activity_id}"
    while True:
        # BRPOP 阻塞等待，避免空转；超时返回 None
        popped = await redis.brpop(queue, timeout=1)
        if popped is None:
            if await activity_ended(activity_id):
                break
            continue
        item = SeckillItem.model_validate_json(popped[1])
        # 此时才真正进入扣减逻辑
        result = await try_deduct(item.sku_id, item.num, biz_key=item.request_id)
        # 写结果到 Redis，用户轮询可查
        await redis.set(f"seckill:result:{activity_id}:{item.user_id}", result.value, ex=300)
        if result is DeductResult.SUCCESS:
            await trade_service.create_seckill_order(item)   # 同进程调用 trade 模块
        else:
            # 库存已耗尽：批量给队列剩余元素返回失败，清空队列
            await drain_and_fail(activity_id)
            break
```

**关键设计点**：

1. **入队是原子的，出队是原子的**：`LPUSH`/`RPOP` 天然原子，无需额外锁。
2. **同一用户去重**：用 `HSETNX seckill:uid_set:{activityId} {userId} 1` 保证一人一单（或限购 N 单）。在入队前做，避免刷子把队列塞满。
3. **队列有界**：`LLEN` 超过阈值（如库存 × 10）时，`Lua` 内直接返回"排队已满"。防止队列无限增长把 Redis 内存打爆。
4. **令牌与库存的配比**：发放的令牌数 > 库存数（打余量给未支付回补的），但队列长度上限受上面第 3 点控制。
5. **消费者单实例**：同一活动只能被一个消费者消费（否则顺序失效）→ 消费协程启动前先抢 `SET seckill:consumer:{activityId} {workerId} NX EX 30` 并定期续期，抢不到的 worker 不启动该活动的消费。
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

第一期是单实例 Redis，分片的意义不是分散到多个节点，而是**降低单 key 上的冲突**：所有请求挤在一个 key 上时，一个分片售罄的判断、对账、展示都集中在热点上。分片设计同时为将来切 Redis Cluster 预留（届时不同分片自然落到不同节点）。做法：

```
分片数 N = 8（可配）  ← ★ 实现取了 N=1，原因见 §11.1
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

Redis 无论多可靠，都要假设它会出错（AOF 丢最后一秒写入、内存淘汰、脚本 Bug）。所以 DB 必须二次校验（SQL 中 `:name` 为 SQLAlchemy `text()` 绑定参数）：

```sql
-- 预占（下单时，与订单写入同一事务）
UPDATE inventory.sku_stock
SET available  = available - :num,
    locked     = locked + :num,
    version    = version + 1,
    updated_at = now()
WHERE sku_id = :sku_id
  AND warehouse_id = :wh_id
  AND available >= :num;
-- rowcount == 0 → 预占失败 → 事务回滚 + Redis 回补 + 订单创建失败
```

```sql
-- 实扣（支付成功时）
UPDATE inventory.sku_stock
SET locked = locked - :num,
    frozen = frozen + :num,
    version = version + 1, updated_at = now()
WHERE sku_id = :sku_id AND warehouse_id = :wh_id
  AND locked >= :num;
```

```sql
-- 发货（商家后台确认发货时）
UPDATE inventory.sku_stock
SET frozen = frozen - :num,
    total  = total - :num,
    version = version + 1, updated_at = now()
WHERE sku_id = :sku_id AND warehouse_id = :wh_id
  AND frozen >= :num;
```

```sql
-- 取消/超时回补（从未支付）
UPDATE inventory.sku_stock
SET available = available + :num,
    locked    = locked - :num,
    version = version + 1, updated_at = now()
WHERE sku_id = :sku_id AND warehouse_id = :wh_id
  AND locked >= :num;
```

**每条 SQL 都带业务条件**（`available >= n`、`locked >= n`），这不是装饰——它是防止"重复回补导致库存虚高"的最后防线。即使超时任务和用户取消同时触发，第二次 `locked >= n` 也会失败。表上的 `CHECK` 约束（见 [02](02-domain-model.md) §2.4）是再下一层的保险。

### 5.1 防死锁：加锁顺序

批量预占多个 SKU 时，**必须按 `(warehouse_id, sku_id)` 升序排列**后再依次更新。否则两个订单以相反顺序锁同一组 SKU 会死锁。

```python
LOCK_SQL = text("""
    UPDATE inventory.sku_stock
    SET available = available - :num, locked = locked + :num,
        version = version + 1, updated_at = now()
    WHERE sku_id = :sku_id AND warehouse_id = :wh_id AND available >= :num
""")

async def lock_stock_in_db(session: AsyncSession, items: list[LockItem]) -> None:
    """在调用方的事务内执行；任何一行失败抛异常，由调用方回滚整个事务。"""
    for it in sorted(items, key=lambda x: (x.warehouse_id, x.sku_id)):
        result = await session.execute(
            LOCK_SQL, {"num": it.num, "sku_id": it.sku_id, "wh_id": it.warehouse_id}
        )
        if result.rowcount == 0:
            raise StockShortageError(it.sku_id)
```

事务保持**尽可能短**：事务内不做任何 HTTP 调用、不等待 Redis 以外的外部资源。审计流水写在同一事务内（只是 INSERT，成本低）或写 outbox 异步落库。

## 6. 回补的触发点

| 触发点 | 回补量 | 幂等键 | 备注 |
|---|---|---|---|
| 订单超时未支付（30min） | locked → available | `cancel:{orderSubNo}` | ARQ 延迟任务 + 定时扫描双保险 |
| 用户主动取消 | locked → available | `cancel:{orderSubNo}` | 立即回补 |
| 支付失败 | locked → available | `payfail:{payNo}` | 由 payment 模块发事件 |
| 预占成功但订单写库失败 | Redis 侧回补 | `rollback:{requestId}` | 补偿事务 |
| 退货入库 | total += n（质检合格） | `return:{refundNo}` | **这是退货的唯一回补点**，详见 [08](08-aftersale.md) |
| 退货质检不合格 | 不回补 | - | 商品报废，库存不恢复 |

**回补必须是"从 Redis 和 DB 各减一次"**：Redis `INCRBY` 恢复分片库存，DB `UPDATE` 恢复 `available`。两者用同一个幂等键，重复执行无副作用。

## 7. 定时对账：修正漂移

ARQ cron 任务，每 5 分钟跑一次（活动期间 1 分钟）：

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

流水表不分区（PG 分区表的唯一约束必须包含分区键，会破坏 `biz_key` 的全局唯一性）。改为**幂等表与流水表分离**：

```sql
-- 幂等键表：小、全局唯一，按 created_at 定期清理 90 天前的记录
CREATE TABLE inventory.stock_biz_key (
  biz_key     VARCHAR(64) PRIMARY KEY,      -- ★ 幂等的最终保证
  created_at  TIMESTAMPTZ(3) NOT NULL DEFAULT now()
);

-- 流水表：按月分区，只追加
CREATE TABLE inventory.stock_flow (
  id            BIGINT GENERATED ALWAYS AS IDENTITY,
  sku_id        BIGINT      NOT NULL,
  warehouse_id  BIGINT      NOT NULL,
  order_no      VARCHAR(32),
  change_type   SMALLINT    NOT NULL,   -- 1预占 2实扣 3回补 4发货扣减 5退货入库 6手工调整 7初始化
  num           INT         NOT NULL,   -- 正数增加，负数减少
  before_qty    INT         NOT NULL,   -- 变更前可售量
  after_qty     INT         NOT NULL,   -- 变更后可售量
  biz_key       VARCHAR(64) NOT NULL,
  operator      VARCHAR(64),            -- 操作人/系统
  remark        VARCHAR(255),
  created_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE inventory.stock_flow_202610 PARTITION OF inventory.stock_flow
  FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE inventory.stock_flow_202611 PARTITION OF inventory.stock_flow
  FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE INDEX idx_stock_flow_sku_time ON inventory.stock_flow (sku_id, created_at);
```

未来月份的分区由 ARQ cron 任务每月 25 日提前创建（`CREATE TABLE IF NOT EXISTS ... PARTITION OF`）。

**`stock_biz_key` 是防重复回补的终极武器**：每次库存变更在同一事务里先执行

```sql
INSERT INTO inventory.stock_biz_key (biz_key) VALUES (:biz_key)
ON CONFLICT DO NOTHING;
```

`rowcount == 0` 说明已处理过，直接返回成功、不再更新库存。`biz_key` 形如 `RETURN:{refundNo}:{skuId}`，即使所有上游幂等都失效，主键也会拦住第二次写入。

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
| Redis 重启丢失最后一秒写入 | DB 仍有 `locked`，对账任务按 DB 重建 Redis。**DB 是账本** |
| 商家突发下架 SKU 但有 5 个 locked | 允许已预占的订单继续支付（不强制取消），只影响新订单 |
| 退货入库，但该 SKU 已下架 | 库存照常回补（`available += n`），但不下发到 Redis 前端展示（下架商品不展示库存） |
| 负数库存告警 | 任何 SQL 执行后 `available < 0` → 立即触发告警 + 冻结该 SKU 下单 |

## 11. 与其它模块的接口

模块化单体中，这些是 `app/modules/inventory/service.py` 暴露给其他模块的 **Python 函数**，不是 HTTP 接口（只有商家后台的手工调整对外暴露 HTTP）。凡是需要与调用方同事务的函数，都接收调用方的 `AsyncSession`。

| 调用方 | 函数 | 语义 |
|---|---|---|
| trade | `lock(session, items, biz_key)` | 批量预占（Redis 预扣在事务外，DB 预占在调用方事务内），返回逐项结果 |
| trade | `release(session, items, biz_key)` | 释放预占（取消/超时） |
| payment | `confirm(session, items, biz_key)` | 预占转实扣（`locked→frozen`） |
| trade（发货） | `deliver(session, items, biz_key)` | 发货，`frozen→` 扣 `total` |
| aftersale | `return_in(session, items, biz_key)` | 退货入库回补（**唯一**退货回补入口） |
| product | `init(session, sku_id, wh_id, qty, biz_key)` | 初始化/覆盖库存（幂等 SET） |
| 商家后台 | `POST /api/merchant/inventory/adjust` | 手工调整 |

**所有函数都必须接受 `biz_key` 参数**，没有例外。这是把幂等责任放在最底层的设计选择——底层可靠，上层就可以简化。

### 11.1 实现状态与两处有意偏离

本期已按上表实现（`app/modules/inventory/`），另有两处与本文原设计不同，以实现为准：

| 项 | 本文原写法 | 实现 | 原因 |
|---|---|---|---|
| 手工调整 | "需审批" | **直接调整**，记 `operator` + 写 `stock_flow` 流水 | 目前没有审批模块；流水已能回答"谁改的"，审批留到有审批流时再接 |
| 分片数 | §3.3 写 N=8 | **默认 N=1**（`INVENTORY_SHARD_COUNT`） | 见下 |

**为什么默认不分片**：跨片不凑整（§10）意味着"超过单片余量的订单"必然失败——
10 件库存分 4 片（3/3/2/2）时买 4 件就下不了单，尽管库存充足，这是正确性问题。
而分片想换的收益（降低单 key 冲突）在单实例 Redis 上并不成立：Lua 脚本本就串行执行。
多分片的代码路径保留，供将来切 Redis Cluster 时启用，但那条限制依然存在。

### 11.2 HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/merchant/warehouses` | 仓库列表（含每个仓覆盖的区划） |
| POST | `/api/merchant/warehouses` | 建仓（首个自动设为默认仓）；**顺手给该店全部 SKU 在这个仓补 0 库存行** |
| PUT | `/api/merchant/warehouses/{id}` | 改仓：名称 / 地址 / 联系人（部分更新） |
| POST | `/api/merchant/warehouses/{id}/default` | 设为默认仓（先摘掉旧的 —— 唯一索引在管） |
| POST | `/api/merchant/warehouses/{id}/status` | 启用 / 停用。**默认仓不允许停用** |
| PUT | `/api/merchant/warehouses/{id}/regions` | 设置这个仓覆盖的区划（**整体替换**） |
| GET | `/api/merchant/inventory` | 库存列表；会先为该店铺**还没有库存记录的 SKU 补 0 库存行**，否则商家发布商品后在库存页看不到它 |
| POST | `/api/merchant/inventory/adjust` | 手工调整，**需要 `Idempotency-Key`** |
| GET | `/api/merchant/inventory/flows` | 库存流水 |
| GET | `/api/skus/{sku_id}/stock` | 买家侧库存档位（**不回传真实库存**，见 §9） |

## 12. 多仓与发货仓路由

`sku_stock` 从第一天就是**分仓**的（唯一键 `(sku_id, warehouse_id)`），但"选哪个仓发货"
一期是写死的：取 `warehouse_id` 最小的一条记录。多仓要成立，得把"从哪发"变成一个
**显式决策**并记下来。

### 12.1 地基：规则优先，缺货按候选链兜底

路由的**主体**仍是纯规则（`inventory/routing.py` 的 `warehouse_candidates`）：
**命中的规则仓 → 默认仓 → 其余启用仓（按 id 升序）**。在此之上，
`inventory/service.route_warehouse` 按这个顺序挑**第一个能一次盖住整单**的仓
（`need = {sku_id: num}` 全都要够，判据用 DB 的 `available`）—— 规则仓没货就从别的仓发。

候选链是**纯函数、不看库存**；库存是 IO，只在 service 里查一次。这样"什么顺序"只有
一处定义，单测也不必连库。

#### v1 那句"绝不看库存"错在哪

v1 这里写的是"路由**不得**因为某仓有货而改选仓"，理由是"算价时选 A、下单时选 B
会导致运费变化，**误触发 priceToken 的价格一致性校验**"。这条理由**双重不成立**：

1. **那套校验根本没实现。** 商城的 `calcPrice` 请求/响应里没有 token；下单请求
   （`OrderCreateRequest`）里也**没有金额字段**，服务端无从比对；
   `price_token_secret` / `PriceChangedError` / `PRICE_TOKEN_EXPIRED` 全是**声明未用**。
   全项目唯一抛 `PRICE_CHANGED` 的地方是 `trade` 的**母子单守恒断言**，与"用户看到的
   价格变了"无关。（[11](11-price-consistency.md) 描述的是目标，与实现之间的差距是
   **另一个**议题，见本文末尾。）
2. **即使它实现了，兜底也不改价**：模板按 `sku_id` 取（与仓无关）、包裹按仓分组，
   而"一个地址落一个仓"意味着子单的所有商品本来就在同一个仓 —— 换仓之后**还是**在
   同一个仓，包裹数、每包重量与模板逐项不变。集成测试
   `test_fallback_does_not_change_freight` 把这条钉住了。

★★ **第 2 条有个前提，改这一带之前先读**：它成立是因为
`freight.repository.list_binds_by_skus` **只按 `sku_id` 过滤、不看 `warehouse_id`**。
而 `SkuFreightBind` 的模型注释与 [docs/06 §1](06-freight.md) 都写着要"按实际发货仓
筛选" —— 那列是**半成品**（见 §12.5）。**哪天真把按仓绑定接上，"兜底换仓"就会改价**：
届时要么只在运费相同时才换仓，要么给订单加价格确认。

#### 不变的推论

1. **同一个子单的所有商品必然发往同一个仓**（同店 + 同地址 + 整单择仓）。所以仓记在
   `order_sub` 上就够，也**不会**出现"一个子单要拆成多张发货单"。
2. 商家事后改区域规则，不影响在途订单 —— 那些订单读的是记录值（§12.3）。
3. ★ **失败形态换了**：v1 是"规则仓没货就整单失败"，现在是**"没有哪个仓能一次盖住整单
   才失败"**（见 §12.6）。**不做逐件挑仓** —— 那会把交易粒度降到 `(子单, SKU, 仓)`，
   分摊/退款/回补/发货单全要改。

### 12.2 规则怎么配

`warehouse_region_rule` 描述"这个仓发往哪些区划"，按 `region_code` **前缀匹配**
（收货码 `440305` 命中规则 `44` 广东）。唯一键是 `(shop_id, region_code)` ——
**一个区划只能由一个仓发货**，于是整张表**不需要优先级**：具体性由**码长**决定
（`4403` 深圳比 `44` 广东具体，两条都命中取码长的那个）。

地址都没命中任何规则 → **默认仓**。所以"每店必有且仅有一个默认仓"是一条不变量，
且**默认仓不允许停用**（要换先设另一个为默认）。

★ 上面的"一个区划只能由一个仓发货"是**首选**意义上的：规则仓没货时会按 §12.1 的候选链
兜到别的仓（默认仓 → 其余启用仓）。规则表的唯一键仍然保证**不会有两个仓抢同一个区划**
—— 那是一条配置上的确定性，与"缺货兜底"不冲突。

### 12.3 下单时决定一次，之后一律读记录值

`order_sub.warehouse_id` 记下这次路由的结果。**发货、支付确认、关单回补、售后退款回补
全部读它**，不再重新路由 —— 否则商家事后改了区域规则，就会让"从 A 仓预占、往 B 仓回补"
成为可能，账面越滚越乱。

历史老单（本轮迁移前）该列为空，代码回退到**该店默认仓**：一期单仓时代那正是它当时
用的仓，而默认仓是显式配置、不随规则变，所以回退不会"入错仓"。

### 12.4 库存行从哪来（新仓怎么"铺货"）

`adjust` 要求库存记录已存在，所以：

| 时机 | 做什么 |
|---|---|
| 商家**建仓** | 给该店**全部** SKU 在这个仓补 0 库存行（商家只需去库存页填数量） |
| 商家**打开库存页**（筛到某个仓） | `sync_missing` 补齐那个仓缺的行 |

★ **刻意不放在下单路径上。** 曾经想在"下单前给路由到的仓补这次要买的 SKU"，
但那跑在**订单事务里**：行不存在 → 预占必然失败 → 事务回滚 → 补出来的行也一起没了，
**恰好在唯一需要它的场合不生效**。所以正确做法是"库存页补齐 + 把话说清楚"。

★ 在择仓眼里"没有库存行"与"存量不够"是**同一件事**（都按 `available = 0` 算），
所以一个从没铺过货的新仓对某件商品就等于"这个仓没货"，候选链会自然跳过它。

### 12.5 不需要改的东西

- **对账 cron**：Redis key 与 `sku_stock` 行本来就是 `(sku_id, warehouse_id)` 粒度。
- **加锁顺序**：§5.1 已要求按 `(warehouse_id, sku_id)` 升序，多仓天然安全。
- **运费**：新仓没绑定的 SKU 会回落到**店铺默认模板**（`freight.estimate` 的既有兜底），
  不必给每个新仓重绑一遍。
  ★ 但 `SkuFreightBind.warehouse_id` **目前是一列死数据** —— `list_binds_by_skus` 只按
  `sku_id` 过滤、取优先级最高的那条，**不看仓**。所以"给同一个 SKU 在不同仓绑不同模板、
  按仓计费"**实际做不到**（[docs/06 §1](06-freight.md) 的写法是**目标**而不是现状）。
  这与"兜底不改运费"直接相关，见 §12.1 的前提警告。

### 12.6 失败形态（v2 之后）

| 场景 | 结果 |
|---|---|
| 规则仓没货、别的仓有货 | ✅ **兜底发货**，仓记在子单上 |
| 全部启用仓都不够（每条商品都"哪儿都没货"） | ❌ `STOCK_SOLD_OUT`(410)：「这单的商品库存不足……可以联系商家补货」。★ **不再提"更换收货地址"** —— 兜底已经把该店所有仓试过了，那句是假话 |
| 没有单仓能盖住整单，但每条商品各自有仓够 | ❌ `STOCK_SOLD_OUT`(410)：提示「**把商品分开下单**」 |
| 该店一个启用的仓都没有 | ❌ `SKU_NOT_SUPPORTED`(422)：「「店名」还没有配置发货仓库」 |
| 择仓时够、预占时不够（并发抢空） | ❌ `STOCK_SOLD_OUT`(410)：「库存刚刚被抢完了，请重新提交」（顺带接管了原先会**原样漏出**的 `STOCK_INSUFFICIENT`，那条带着数字 SKU id、没有仓名） |

★ **"分开下单"真的可行**：择仓是**按这一单**判的（`need` 就是这一单的 SKU 与数量），
所以把其中一件单独下一单时，它自己的候选链会兜到有那个货的仓。落地方式是购物车
**取消勾选**其余商品。注意它只解决"分散在不同仓"，不解决"某个仓的数量不够 `num`"。

★ 结算页（`POST /api/checkout/calc`）用**同一套**判断并给出 `canSubmit`：**没有哪个仓能
一次发齐**时为 `false`，理由在 `notices` 里，前端据此禁用提交按钮 —— 于是不再出现
"结算说能买、下单说没货"。缺货**不会**让算价本身失败（用户可能只是想看看多少钱）。

★ **残留的口径差异（有意保留）**：商品详情页与购物车的库存是**全局**口径（跨仓求和 →
`soldOut` / `无货`），结算页是**按仓**。两者只在"多件商品凑不进同一个仓"这一种情况下
不一致（全局有货、但这单发不出）；单 SKU 时一致。
