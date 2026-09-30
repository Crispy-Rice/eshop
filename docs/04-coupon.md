# 04 优惠券：Redis 原子发券与超发防护

## 1. 券超发的成因

**超发**：模板限量 10000 张，实际发出 10012 张。

| 成因 | 说明 |
|---|---|
| **检查与扣减非原子** | `GET stock` → 判断 > 0 → `DECR` 三步之间被并发插入，10000 个请求都判断"有货" |
| **多节点各自计数** | 用 JVM 内存计数器，8 个实例各发 10000 张 |
| **DB 无唯一约束** | 同一用户并发领了 3 张（唯一索引缺失） |
| **重试放大** | 接口超时，客户端重试，每次重试都发一张 |
| **回滚未配对** | 领券后订单取消，券退回但库存计数没退，或退多了 |

**解决思路**：把"检查+扣减+记录"压进**一条 Redis Lua 脚本**。Redis 单线程执行 Lua，脚本内不存在并发交错，天然原子。

## 2. 数据模型

### 2.1 券模板（券的定义）

```sql
CREATE TABLE `coupon_template` (
  `id`                 BIGINT       NOT NULL,
  `shop_id`            BIGINT       NOT NULL DEFAULT 0 COMMENT '0=平台券，>0=店铺券',
  `name`               VARCHAR(64)  NOT NULL COMMENT '如"满200减30"',
  `type`               TINYINT      NOT NULL COMMENT '1满减券 2折扣券 3无门槛券 4兑换券 5运费券',
  `get_type`           TINYINT      NOT NULL COMMENT '1主动领取 2系统发放 3兑换码 4活动赠送 5新客自动发',
  -- 面额规则
  `discount_value`     BIGINT       NOT NULL COMMENT '满减=减免额(分)；折扣=折扣率(如8500表示85折)；无门槛=减免额',
  `max_discount`       BIGINT       NOT NULL DEFAULT 0 COMMENT '折扣券的封顶金额(分)，0=不限',
  `threshold`          BIGINT       NOT NULL DEFAULT 0 COMMENT '使用门槛(分)，0=无门槛',
  -- 发放规则
  `total_count`        INT          NOT NULL COMMENT '发行总量',
  `issued_count`       INT          NOT NULL DEFAULT 0 COMMENT '已发放（DB账本）',
  `used_count`         INT          NOT NULL DEFAULT 0 COMMENT '已使用',
  `per_user_limit`     INT          NOT NULL DEFAULT 1 COMMENT '每人限领',
  -- 有效期
  `valid_type`         TINYINT      NOT NULL COMMENT '1固定区间 2领取后N天',
  `valid_start`        DATETIME(3)  DEFAULT NULL,
  `valid_end`          DATETIME(3)  DEFAULT NULL,
  `valid_days`         INT          DEFAULT NULL COMMENT '领取后有效天数',
  -- 适用范围（见 §4）
  `scope_type`         TINYINT      NOT NULL DEFAULT 1 COMMENT '1全场 2指定商品 3指定类目 4指定店铺',
  `scope_value`        TEXT         DEFAULT NULL COMMENT 'JSON数组，存 spuId/skuId/categoryId/shopId',
  `exclude_value`      TEXT         DEFAULT NULL COMMENT '排除范围',
  -- 叠加规则
  `stackable`          TINYINT      NOT NULL DEFAULT 0 COMMENT '1可与店铺券叠加 0不可',
  `exclusive_with_promo` TINYINT    NOT NULL DEFAULT 0 COMMENT '1与活动互斥',
  `status`             TINYINT      NOT NULL DEFAULT 1 COMMENT '1未开始 2进行中 3已结束 4已作废',
  `created_at`         DATETIME(3)  NOT NULL,
  `updated_at`         DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_status_time` (`status`, `valid_end`)
) ENGINE=InnoDB COMMENT='优惠券模板';
```

### 2.2 用户券实例

```sql
CREATE TABLE `coupon_code` (
  `id`                BIGINT       NOT NULL COMMENT '券实例ID = 券码',
  `coupon_template_id` BIGINT      NOT NULL,
  `user_id`           BIGINT       NOT NULL,
  `code`              VARCHAR(32)  NOT NULL COMMENT '可读券码，用于客服/线下核销',
  `status`            TINYINT      NOT NULL DEFAULT 1 COMMENT '1未使用 2已锁定(下单占用) 3已使用 4已过期 5已作废',
  `valid_start`       DATETIME(3)  NOT NULL COMMENT '冗余自模板，解决"领取后N天"',
  `valid_end`         DATETIME(3)  NOT NULL,
  `locked_order_no`   VARCHAR(32)  DEFAULT NULL COMMENT '锁定它的订单号',
  `locked_at`         DATETIME(3)  DEFAULT NULL,
  `used_order_no`     VARCHAR(32)  DEFAULT NULL,
  `used_at`           DATETIME(3)  DEFAULT NULL,
  `use_amount`        BIGINT       DEFAULT NULL COMMENT '实际抵扣金额(分)',
  `source`            TINYINT      NOT NULL DEFAULT 1 COMMENT '1主动领取 2系统发放 3兑换 4活动 5退回',
  `received_at`       DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code` (`code`),
  KEY `idx_user_status` (`user_id`, `status`, `valid_end`) COMMENT '我的券列表主查询',
  KEY `idx_template_user` (`coupon_template_id`, `user_id`) COMMENT '限领校验',
  KEY `idx_locked_order` (`locked_order_no`)
) ENGINE=InnoDB COMMENT='用户优惠券实例';
```

```sql
-- 领取记录：用于限领与防刷（与 coupon_code 分离，因为可能领了但未生成券）
CREATE TABLE `coupon_receive_log` (
  `id`                 BIGINT      NOT NULL AUTO_INCREMENT,
  `coupon_template_id` BIGINT      NOT NULL,
  `user_id`            BIGINT      NOT NULL,
  `coupon_code_id`     BIGINT      DEFAULT NULL,
  `channel`            VARCHAR(32) NOT NULL COMMENT '渠道来源，用于归因',
  `ip`                 VARCHAR(64) DEFAULT NULL,
  `device_id`          VARCHAR(64) DEFAULT NULL,
  `idempotency_key`    VARCHAR(64) DEFAULT NULL,
  `created_at`         DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_idem` (`idempotency_key`),
  KEY `idx_tpl_user` (`coupon_template_id`, `user_id`),
  KEY `idx_ip_time` (`ip`, `created_at`) COMMENT '刷券分析'
) ENGINE=InnoDB COMMENT='领券流水';
```

## 3. Redis 结构

```
# 模板级计数（发券总量控制）
coupon:tpl:{templateId}:stock      → int   剩余可发量（初始 = total_count - issued_count）
coupon:tpl:{templateId}:user_count → hash  {userId: 已领张数}
coupon:tpl:{templateId}:meta       → hash  {total, issued, validEnd, status, threshold, ...}  缓存模板，避免每次查 DB

# 券实例级状态（下单锁券）
coupon:code:{codeId}               → hash  {userId, templateId, status, validEnd}

# 幂等
coupon:idem:{idempotencyKey}       → string  结果，EX 24h

# 库存预热标记
coupon:tpl:{templateId}:warm       → 1 表示 Redis 已预热
```

## 4. 核心：原子发券 Lua 脚本

```lua
-- ============================================================
-- 领券脚本：检查库存 + 限领 + 扣减 + 记录 全部原子
-- KEYS[1] = coupon:tpl:{tplId}:stock
-- KEYS[2] = coupon:tpl:{tplId}:user_count
-- KEYS[3] = coupon:idem:{idempotencyKey}
-- KEYS[4] = coupon:tpl:{tplId}:meta
-- ARGV[1] = userId
-- ARGV[2] = perUserLimit
-- ARGV[3] = 幂等键
-- ARGV[4] = 结果缓存TTL（秒）
-- ARGV[5] = 当前时间戳（秒，由应用传入，保证脚本内时间一致）
-- ARGV[6] = 券模板有效期结束时间戳（用于提前拒绝过期活动）
-- 返回：{code, data}
--   code: 0=成功  1=已售罄  2=超出限领  3=幂等命中  4=活动未开始/已结束  5=未预热
-- ============================================================

local stockKey  = KEYS[1]
local ucntKey   = KEYS[2]
local idemKey   = KEYS[3]
local metaKey   = KEYS[4]
local userId    = ARGV[1]
local limit     = tonumber(ARGV[2])
local idem      = ARGV[3]
local ttl       = tonumber(ARGV[4])
local nowTs     = tonumber(ARGV[5])
local endTs     = tonumber(ARGV[6])

-- ① 幂等：同一个 Idempotency-Key 已处理过，直接返回上次结果
local prev = redis.call('GET', idemKey)
if prev then
    return {3, prev}
end

-- ② 预热校验：Redis 没预热就别发，避免"看起来没库存"或"绕过限领"
if redis.call('EXISTS', metaKey) == 0 then
    return {5, 'NOT_WARMED'}
end

-- ③ 活动时间校验（脚本内判断，防止前端改时间）
if nowTs > endTs then
    return {4, 'EXPIRED'}
end
local startTs = tonumber(redis.call('HGET', metaKey, 'startTs') or '0')
if nowTs < startTs then
    return {4, 'NOT_STARTED'}
end
local tplStatus = tonumber(redis.call('HGET', metaKey, 'status') or '0')
if tplStatus ~= 2 then
    return {4, 'STOPPED'}
end

-- ④ 限领校验
local got = tonumber(redis.call('HGET', ucntKey, userId) or '0')
if limit > 0 and got >= limit then
    return {2, got}
end

-- ⑤ 库存校验与扣减（同一脚本内，无并发窗口）
local stock = tonumber(redis.call('GET', stockKey) or '-1')
if stock < 0 then
    return {5, 'NOT_WARMED'}
end
if stock <= 0 then
    return {1, 0}
end

redis.call('DECR', stockKey)
redis.call('HINCRBY', ucntKey, userId, 1)

-- ⑥ 写幂等结果（真正生成券码在 DB 侧完成，这里返回令牌）
local token = cjson.encode({tplId = redis.call('HGET', metaKey, 'tplId'), userId = userId, ts = nowTs})
redis.call('SET', idemKey, token, 'EX', ttl)

return {0, token}
```

**这个脚本解决了超发的全部四个成因**：

| 成因 | 脚本中的应对 |
|---|---|
| 检查与扣减非原子 | ②④⑤ 全在同一脚本内，Redis 单线程无交错 |
| 多节点各计数 | 计数在 Redis，所有节点共享 |
| 无唯一约束 | API 层的 `Idempotency-Key` + DB 的 `uk_idem` |
| 重试放大 | ① 幂等命中直接返回首次结果 |
| — | ⑥ 幂等键在**扣减后立即写入**，重试不会造成二次扣减 |

### 4.1 脚本为什么用 `nowTs` 而不是 `redis.call('TIME')`

Redis 的 `TIME` 命令在 Lua 里是随机命令（Replication 不一致风险），且脚本持久化后重放结果不同。**由应用传入时间戳**，所有实例时间同步由 NTP 保证。代价是客户端可能传假时间，所以：

- 脚本同时校验 `endTs`（从 meta 读，服务端写入）；
- **DB 侧二次校验时间**（`INSERT` 前用 DB 的 `NOW()` 再判一次），防止客户端时间被篡改。

### 4.2 库存为负的容错

```lua
local stock = tonumber(redis.call('GET', stockKey) or '-1')
if stock <= 0 then return {1, 0} end
redis.call('DECR', stockKey)
```

即使出现极端情况（脚本 Bug 或极端并发），`DECR` 后库存可能变 -1。**必须靠"先判断再扣"**，此脚本已是先判后扣，所以不会为负。对账任务额外校验：`GET stock < 0` → 立即告警 + 从 DB 重建计数。

## 5. 发券完整链路

```
① 用户点击"立即领取"
   │
② 网关：Idempotency-Key 校验（前端每次点击生成 UUID，重复点击复用同一个）
   │
③ 营销服务：本地缓存查模板 → 未命中查 Redis meta → 未命中查 DB 并回填
   │
④ 执行 Lua 脚本
   │  ├─ code 1 (售罄) → 返回"已被抢光"
   │  ├─ code 2 (超限) → 返回"每人限领 N 张"
   │  ├─ code 3 (幂等) → 返回首次结果
   │  ├─ code 4 (时机) → 返回"活动未开始/已结束"
   │  └─ code 0 (成功) → 进入⑤
   │
⑤ 生成券实例（DB 事务）
   INSERT INTO coupon_code (...) VALUES (...);           -- 券实例
   INSERT INTO coupon_receive_log (...) VALUES (...);     -- 领取记录
   UPDATE coupon_template SET issued_count = issued_count + 1 WHERE id = ?;
   │
   │  ★ DB 事务失败怎么办？→ 补偿
   │     - 重试 3 次（本地消息表 / MQ 延迟重试）
   │     - 仍失败 → 回滚 Redis（INCR stock, HINCRBY user_count -1）
   │       用 coupon:rollback:{idemKey} 做幂等，避免重复回滚
   │
⑥ 返回券信息，异步推送通知
```

### 5.1 为什么先 Redis 后 DB

反过来（先 DB 后 Redis）的话，DB 事务提交后 Redis 扣减失败 → 券发出去了但计数没减 → 超发。

当前顺序的风险是"Redis 扣了但 DB 没写" → 少发（库存浪费）。**少发和超发之间，宁可少发**：少发可通过人工补发或对账修正，超发无法回收（用户已经拿到券了）。

### 5.2 DB 侧的唯一约束兜底

即使 Redis 完全失效，DB 层还必须有最后一道：

```sql
-- 限领的 DB 级保证（1 人 1 张时）
ALTER TABLE coupon_receive_log ADD UNIQUE KEY uk_tpl_user (coupon_template_id, user_id);
-- ↑ 注意：仅当 per_user_limit = 1 时可用；限领 N 张则不能建这个索引
```

限领 N（N > 1）时的 DB 兜底：

```sql
-- 悲观锁 + 计数校验（低并发场景）或
SELECT COUNT(*) FROM coupon_receive_log
WHERE coupon_template_id = ? AND user_id = ? FOR UPDATE;
-- 配合 coupon_template.issued_count 也做条件更新：
UPDATE coupon_template
SET issued_count = issued_count + 1
WHERE id = ? AND issued_count < total_count;   -- ★ 条件更新，RowsAffected=0 即超发拦截
```

**`WHERE issued_count < total_count` 这一句是 DB 层的超发防线**。Redis 是拦第一道，这是第二道。

## 6. 券的生命周期状态机

```
                   ┌─────────────────┐
                   │  1 未使用        │◄──── 退回（整单退）
                   └────────┬────────┘
                            │ ① 下单锁定
                            ▼
                   ┌─────────────────┐
                   │  2 已锁定        │──── 订单取消/超时 ──► 1 未使用
                   └────────┬────────┘
                            │ ② 支付成功
                            ▼
                   ┌─────────────────┐
                   │  3 已使用        │
                   └─────────────────┘

   定时任务：1 未使用 && valid_end < now ──► 4 已过期
   运营/风控：任意状态 ──► 5 已作废（刷券账号、活动终止）
```

| 转换 | 触发 | 幂等键 |
|---|---|---|
| 1 → 2 | 提交订单时锁券 | `LOCK:{orderMainNo}:{couponCodeId}` |
| 2 → 1 | 订单取消/超时/支付失败 | `UNLOCK:{orderMainNo}:{couponCodeId}` |
| 2 → 3 | 支付成功回调 | `USE:{orderMainNo}:{couponCodeId}` |
| 3 → 1 | 整单退款成功 | `REFUND:{refundNo}:{couponCodeId}` |
| 1 → 4 | 过期任务 | `EXPIRE:{couponCodeId}` |

**锁券的 SQL（乐观锁，防止并发用同一张券）**：

```sql
UPDATE coupon_code
SET status = 2, locked_order_no = #{orderMainNo}, locked_at = NOW(3)
WHERE id = #{codeId}
  AND user_id = #{userId}
  AND status = 1                        -- ★ 只有未使用才能锁
  AND valid_start <= NOW(3) AND valid_end >= NOW(3);
-- RowsAffected = 0 → 券不可用（已被其他订单占用/已过期）
```

**锁券同时也要在 Redis 标记**，让结算页能快速判断：

```lua
-- 锁券脚本（幂等）
-- KEYS[1]=coupon:code:{codeId}  ARGV[1]=orderMainNo  ARGV[2]=userId
local st = redis.call('HGET', KEYS[1], 'status')
if st == 'USED' then return 2 end                    -- 已使用
if st == 'LOCKED' and redis.call('HGET',KEYS[1],'orderNo') ~= ARGV[1] then
    return 3                                          -- 被其他订单锁定
end
redis.call('HSET', KEYS[1], 'status', 'LOCKED', 'orderNo', ARGV[1])
redis.call('EXPIRE', KEYS[1], 1800)                   -- 30 分钟，与订单超时一致
return 1
```

## 7. 券的适用范围解析

结算页要判断"哪些券可用"，这是最容易出性能问题的地方。

### 7.1 分层校验（从快到慢）

```
第 1 层：用户券列表缓存（Redis Hash，用户维度，TTL 5min）
   → 过滤：status=1、未过期

第 2 层：模板规则缓存（本地 Caffeine + Redis，模板维度，TTL 10min）
   → 过滤：店铺匹配、门槛满足

第 3 层：作用域匹配（scope_type/scope_value）
   → 全场 → 直接通过
   → 指定店铺 → 比对 shop_id（购物车内店铺集合）
   → 指定类目 → 比对购物车内商品的类目（需 category 缓存）
   → 指定商品 → 比对 spuId/skuId 集合（用 Redis SET 或 Bloom，避免大 JSON 解析）

第 4 层：精确计算该券能抵多少（见 05-promotion-engine）
```

**关键优化**：`scope_value` 不要每次都解析 JSON。做法：
- 存 DB 时同时写一份到 Redis Set：`coupon:tpl:{tplId}:spu_set`、`coupon:tpl:{tplId}:sku_set`
- 结算时用 `SISMEMBER` / `SMISMEMBER` 批量判断，O(1) 复杂度
- 适用商品数超过 5000 时，**反转逻辑**：不存白名单，存"排除名单"，默认全场可用——避免集合膨胀

### 7.2 券可用的判定条件清单

一张券能在当前购物车使用，必须**全部**满足：

```
[ ] 券状态 = 未使用
[ ] 当前时间 ∈ [valid_start, valid_end]
[ ] 券的 user_id = 当前用户
[ ] 店铺匹配：店铺券 → 购物车含该店铺商品
[ ] 作用域匹配：购物车内至少一个商品在 scope 内
[ ] 门槛满足：作用域内商品的"参与优惠金额"合计 >= threshold
[ ] 排除范围：作用域内商品不在 exclude_value 中
[ ] 叠加规则：与已选券/活动不冲突（见 05）
[ ] 未与其它订单冲突：status != LOCKED by other order
```

## 8. 领券的防刷设计

刷券是最常见的黑产行为（领了转卖 / 领了套现）。多层防护：

| 层级 | 手段 |
|---|---|
| 账号 | 实名限制、新注册账号 24h 内不可领高价值券 |
| 用户维度 | `per_user_limit`（Redis Hash 计数 + DB 唯一索引） |
| IP 维度 | `coupon:ip:{ip}:count`，单 IP 单活动限 20 张 |
| 设备维度 | `coupon:device:{deviceId}:count`，单设备限 5 张 |
| 行为 | 领券接口 QPS 限流（用户 5 QPS、IP 50 QPS） |
| 风控 | 接入风控服务，异常账号（同 IP 多账号、新号集中领券）直接拒绝 |
| 事后 | `coupon_receive_log` 按 IP/device 聚合分析，批量作废异常券 |

**假成功策略（重要）**：风控判定为刷子时，**返回"领取成功"，但不真正发券**——券写入一个"影子表"，风控人工复核后才决定是否放出。这样刷子无法通过"成功/失败"来探测风控规则，避免对抗升级。

## 9. 券过期处理

```sql
-- 分批处理，避免大事务
UPDATE coupon_code
SET status = 4
WHERE status = 1 AND valid_end < NOW(3)
LIMIT 1000;
```

- **不要用 `UPDATE ... WHERE valid_end < NOW()` 全表扫**。用 `idx_user_status(user_id, status, valid_end)` 索引，但全表过期扫描应按 `valid_end` 建单独索引分批跑。
- **过期前 3 天 / 1 天提醒**：定时任务扫 `valid_end BETWEEN now+23h AND now+24h AND status=1`，发推送。
- **过期是惰性 + 主动结合**：查询券列表时，若发现 `valid_end < now && status = 1`，顺手改状态（惰性）；同时定时任务主动跑（兜底）。

## 10. 对账：券的核心一致性检查

每 10 分钟跑一次：

```
① 模板级对账
   DB 真值：SELECT COUNT(*) FROM coupon_code WHERE coupon_template_id = ?
   Redis 值：total_count - GET coupon:tpl:{tplId}:stock
   差值 != 0 → 告警 + 按 DB 重建 Redis（SET stock = total_count - db_count）

② 用户限领对账
   DB：SELECT user_id, COUNT(*) FROM coupon_receive_log WHERE tpl = ? GROUP BY user_id
   Redis：HGETALL coupon:tpl:{tplId}:user_count
   不一致 → 以 DB 为准 HSET 修正

③ 状态对账
   找出"Redis 说 LOCKED 但 DB 是 1 未使用，且锁定超过 40 分钟"的券
   → 强制解锁（说明订单早已消失，锁没释放）

④ 超发检测（最重要）
   SELECT coupon_template_id, issued_count, total_count
   FROM coupon_template WHERE issued_count > total_count;  -- 必须为空
   有结果 → P0 告警，立即冻结该模板
```

## 11. 关键边界场景

| 场景 | 处理 |
|---|---|
| 用户领券时 Redis 挂了 | 返回"活动太火爆，请稍后再试"，**不降级到 DB 直发**（DB 无法承受这个 QPS，且降级会造成绕过限领） |
| Redis 扣减成功，DB 写入失败 | 重试 3 次，仍失败则回滚 Redis（幂等回滚），记录异常日志人工核查 |
| 同一用户并发领 1 张券（限领 1） | Lua 脚本保证只成功一次，第二次返回 code 2 或 code 3 |
| 用户领券后立即在另一订单使用，同时原订单在支付 | 券状态为 LOCKED，第二个订单锁券失败（`status=1` 条件不满足） |
| 券锁定后订单超时取消，券未解锁 | 定时任务扫描 `locked_at < now - 40min` 的券，强制解锁（Redis + DB 双写） |
| 客服手工补发券 | 走单独的 `adminIssue` 接口，**不走 Redis stock 计数**（不占用活动额度），直接 INSERT coupon_code + log，source=2 |
| 发券活动提前终止 | 置 `status=3`，同时 `DEL coupon:tpl:{id}:meta` 让脚本返回 code 4 |
| 券模板修改了 threshold | 已发出的券**必须使用领取时的快照**。方案：`coupon_code` 增冗余列（threshold_snap, discount_value_snap），或者**规则禁止修改进行中的模板**（推荐，运营只能新建） |

**推荐做法**：优惠券模板一旦有券发出，**核心字段（面额、门槛、有效期、适用范围）不可修改**，只能作废后新建。这条规则消灭了一整类"已发券规则漂移"的 Bug。

## 12. 性能设计

| 指标 | 目标 | 手段 |
|---|---|---|
| 领券 TPS | 20000 | 纯 Redis Lua，无 DB 同步写（DB 异步化） |
| 领券 P99 | < 30ms | Lua 脚本 4 次 Redis 操作，一次网络往返 |
| 券列表 P99 | < 50ms | 用户券 Hash 缓存 + 模板本地缓存 |
| 结算页券可用计算 | < 30ms | 分层过滤，前 3 层全内存 |

**领券 TPS 20000 的关键**：DB 写入不在请求链路上。返回给用户"领取成功"后，券实例通过 MQ 异步生成（用户 3 秒后能看到券即可）。异步失败由本地消息表重试保证。

但要处理"领了但券还没生成，用户立刻去结算"的场景：**Redis 里先写入一个"预发券"标记**，结算时若发现券实例未生成但预发标记存在，则同步生成（补偿路径）。

```lua
-- 领券成功后立即写预发标记（同一个 Lua 脚本内或紧随其后）
SET coupon:pending:{userId}:{tplId}:{idemKey} {token} EX 300
```

结算时：`券列表 = DB已生成券 ∪ Redis预发券（触发同步补偿）`。
