# 04 优惠券：Redis 原子发券与超发防护

## 1. 券超发的成因

**超发**：模板限量 10000 张，实际发出 10012 张。

| 成因 | 说明 |
|---|---|
| **检查与扣减非原子** | `GET stock` → 判断 > 0 → `DECR` 三步之间被并发插入，10000 个请求都判断"有货" |
| **多进程各自计数** | 用进程内存计数器，4 个 Uvicorn worker 各发 10000 张 |
| **DB 无唯一约束** | 同一用户并发领了 3 张（唯一索引缺失） |
| **重试放大** | 接口超时，客户端重试，每次重试都发一张 |
| **回滚未配对** | 领券后订单取消，券退回但库存计数没退，或退多了 |

**解决思路**：把"检查+扣减+记录"压进**一条 Redis Lua 脚本**。Redis 单线程执行 Lua，脚本内不存在并发交错，天然原子。

## 2. 数据模型

### 2.1 券模板（券的定义）

```sql
CREATE TABLE promotion.coupon_template (
  id                   BIGINT       PRIMARY KEY,
  shop_id              BIGINT       NOT NULL DEFAULT 0,   -- 0=平台券，>0=店铺券
  name                 VARCHAR(64)  NOT NULL,             -- 如"满200减30"
  type                 SMALLINT     NOT NULL,             -- 1满减券 2折扣券 3无门槛券 4兑换券 5运费券
  get_type             SMALLINT     NOT NULL,             -- 1主动领取 2系统发放 3兑换码 4活动赠送 5新客自动发
  -- 面额规则
  discount_value       BIGINT       NOT NULL,             -- 满减=减免额(分)；折扣=折扣率(如8500表示85折)；无门槛=减免额
  max_discount         BIGINT       NOT NULL DEFAULT 0,   -- 折扣券的封顶金额(分)，0=不限
  threshold            BIGINT       NOT NULL DEFAULT 0,   -- 使用门槛(分)，0=无门槛
  -- 发放规则
  total_count          INT          NOT NULL,             -- 发行总量
  issued_count         INT          NOT NULL DEFAULT 0,   -- 已发放（DB 账本）
  used_count           INT          NOT NULL DEFAULT 0,
  per_user_limit       INT          NOT NULL DEFAULT 1,   -- 每人限领
  -- 有效期
  valid_type           SMALLINT     NOT NULL,             -- 1固定区间 2领取后N天
  valid_start          TIMESTAMPTZ(3),
  valid_end            TIMESTAMPTZ(3),
  valid_days           INT,                               -- 领取后有效天数
  -- 适用范围（见 §7）
  scope_type           SMALLINT     NOT NULL DEFAULT 1,   -- 1全场 2指定商品 3指定类目 4指定店铺
  scope_value          JSONB,                             -- 数组，存 spuId/skuId/categoryId/shopId
  exclude_value        JSONB,                             -- 排除范围
  -- 叠加规则
  stackable            BOOLEAN      NOT NULL DEFAULT false, -- 可与店铺券叠加
  exclusive_with_promo BOOLEAN      NOT NULL DEFAULT false, -- 与活动互斥
  status               SMALLINT     NOT NULL DEFAULT 1,   -- 1未开始 2进行中 3已结束 4已作废
  created_at           TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  -- ★ DB 层的超发硬约束
  CONSTRAINT ck_coupon_tpl_issued CHECK (issued_count <= total_count)
);
CREATE INDEX idx_coupon_tpl_status_time ON promotion.coupon_template (status, valid_end);
COMMENT ON TABLE promotion.coupon_template IS '优惠券模板';
```

`status` 的 `1/2/3` 是**有效期的投影**，由 `promotion.tasks.refresh_promo_status` 每分钟推进
（建模板时一律先写"进行中"，由它在一分钟内校正）—— 没有这一步，**过期的券在运营列表里
永远挂着"进行中"**，而运营就是照着这一列判断"这张券还能不能领"。`valid_start` / `valid_end`
为 `NULL` 的「领取后 N 天」型没有统一时间窗，恒为进行中。

**`4`（已作废）只由运营的 `POST /api/admin/coupons/templates/{id}/void` 写入** ——
它是**下线**，不是删除，而且**只止住新的领取**：`claim_template_quota` 那道 SQL 本来就要求
`status = 2`，所以作废当场生效；但已经发出去的券照旧能用 —— 券一旦发出去就是承诺，
运营反悔不能把用户手上的券作废掉。同理不提供删除：券码里记着 `coupon_template_id`，
删模板会让那些券查不到来源。促销活动走的是同一条路（见 [05 §9](05-promotion-engine.md)）。

### 2.2 用户券实例

```sql
CREATE TABLE promotion.coupon_code (
  id                 BIGINT       PRIMARY KEY,          -- 券实例 ID
  coupon_template_id BIGINT       NOT NULL REFERENCES promotion.coupon_template (id),
  user_id            BIGINT       NOT NULL,
  code               VARCHAR(32)  NOT NULL,             -- 可读券码，用于客服核销
  status             SMALLINT     NOT NULL DEFAULT 1,   -- 1未使用 2已锁定(下单占用) 3已使用 4已过期 5已作废
  valid_start        TIMESTAMPTZ(3) NOT NULL,           -- 冗余自模板，解决"领取后N天"
  valid_end          TIMESTAMPTZ(3) NOT NULL,
  locked_order_no    VARCHAR(32),                       -- 锁定它的订单号
  locked_at          TIMESTAMPTZ(3),
  used_order_no      VARCHAR(32),
  used_at            TIMESTAMPTZ(3),
  use_amount         BIGINT,                            -- 实际抵扣金额(分)
  source             SMALLINT     NOT NULL DEFAULT 1,   -- 1主动领取 2系统发放 3兑换 4活动 5退回
  received_at        TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT uk_coupon_code UNIQUE (code)
);
CREATE INDEX idx_coupon_code_user_status ON promotion.coupon_code (user_id, status, valid_end); -- 我的券列表
CREATE INDEX idx_coupon_code_tpl_user    ON promotion.coupon_code (coupon_template_id, user_id); -- 限领校验
CREATE INDEX idx_coupon_code_locked      ON promotion.coupon_code (locked_order_no) WHERE locked_order_no IS NOT NULL;
-- 过期扫描：只索引"未使用"的券（部分索引，体积小）
CREATE INDEX idx_coupon_code_expire      ON promotion.coupon_code (valid_end) WHERE status = 1;
COMMENT ON TABLE promotion.coupon_code IS '用户优惠券实例';
```

```sql
-- 领取记录：用于限领与防刷（与 coupon_code 分离，因为可能领了但未生成券）
CREATE TABLE promotion.coupon_receive_log (
  id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  coupon_template_id BIGINT       NOT NULL,
  user_id            BIGINT       NOT NULL,
  coupon_code_id     BIGINT,
  channel            VARCHAR(32)  NOT NULL,             -- 渠道来源，用于归因
  ip                 INET,
  device_id          VARCHAR(64),
  idempotency_key    VARCHAR(64),
  created_at         TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT uk_coupon_receive_idem UNIQUE (idempotency_key)
);
CREATE INDEX idx_coupon_receive_tpl_user ON promotion.coupon_receive_log (coupon_template_id, user_id);
CREATE INDEX idx_coupon_receive_ip_time  ON promotion.coupon_receive_log (ip, created_at); -- 刷券分析
COMMENT ON TABLE promotion.coupon_receive_log IS '领券流水';
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

Redis 7 起脚本按"效果复制"，`TIME` 已可安全使用；但**由应用传入时间戳**能让脚本结果可测试（单测里注入固定时间），并且与 DB 判定用的是同一时刻。时间戳由服务端 `time.time()` 生成，不接受前端传入，宿主机时间同步由腾讯云 CVM 默认的 NTP 保证。同时：

- 脚本同时校验 `endTs`（从 meta 读，服务端写入）；
- **DB 侧二次校验时间**（`INSERT` 前用 DB 的 `now()` 再判一次）。

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
② FastAPI 依赖：Idempotency-Key 校验（前端每次点击生成 UUID，重复点击复用同一个）
   │
③ promotion 模块：进程内缓存查模板 → 未命中查 Redis meta → 未命中查 DB 并回填
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
   │     - 重试 3 次（投递 ARQ 重试任务，指数退避）
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

限领的 DB 级保证不能直接在流水表上建 `(coupon_template_id, user_id)` 唯一索引（限领 N 张时会误拦）。改用一张**用户领取计数表**，限领 1 张和 N 张统一处理：

```sql
CREATE TABLE promotion.coupon_user_quota (
  coupon_template_id BIGINT NOT NULL,
  user_id            BIGINT NOT NULL,
  received           INT    NOT NULL DEFAULT 0,
  PRIMARY KEY (coupon_template_id, user_id)
);
```

领券的 DB 事务（Redis 脚本成功之后执行）：

```sql
-- ① 用户限领：upsert + 条件，超限时 rowcount = 0
INSERT INTO promotion.coupon_user_quota (coupon_template_id, user_id, received)
VALUES (:tpl_id, :user_id, 1)
ON CONFLICT (coupon_template_id, user_id)
DO UPDATE SET received = coupon_user_quota.received + 1
WHERE coupon_user_quota.received < :per_user_limit;

-- ② 模板总量：条件更新，rowcount = 0 即超发拦截
UPDATE promotion.coupon_template
SET issued_count = issued_count + 1, updated_at = now()
WHERE id = :tpl_id AND issued_count < total_count
  AND status = 2 AND (valid_end IS NULL OR valid_end > now());

-- ③ 两步都成功才写券实例与流水
INSERT INTO promotion.coupon_code (...) VALUES (...);
INSERT INTO promotion.coupon_receive_log (...) VALUES (...)
ON CONFLICT (idempotency_key) DO NOTHING;
```

任何一步 `rowcount = 0` → 抛异常回滚整个事务 → 回滚 Redis 计数。

**`WHERE issued_count < total_count` 和表上的 `CHECK (issued_count <= total_count)` 是 DB 层的超发防线**。Redis 是拦第一道，这是第二道。

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
UPDATE promotion.coupon_code
SET status = 2, locked_order_no = :order_main_no, locked_at = now()
WHERE id = :code_id
  AND user_id = :user_id
  AND status = 1                        -- ★ 只有未使用才能锁
  AND valid_start <= now() AND valid_end >= now();
-- rowcount = 0 → 券不可用（已被其他订单占用/已过期）
```

这条 SQL 与订单写入在**同一个事务**里执行（模块化单体同库的好处），订单落库失败时券锁定自动回滚，不需要额外的解锁补偿。

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

第 2 层：模板规则缓存（进程内 TTLCache + Redis，模板维度，TTL 10min）
   → 过滤：店铺匹配、门槛满足

第 3 层：作用域匹配（scope_type/scope_value）
   → 全场 → 直接通过
   → 指定店铺 → 比对 shop_id（购物车内店铺集合）
   → 指定类目 → 比对购物车内商品的类目（需 category 缓存）
     ★ **含该类的全部子类目**：商品**只能挂在末级类目**下（`product._resolve_category`），
       所以"只匹配所选类目本身"在父类目上永远匹配不到任何东西 —— 而活动/券在列表上
       和正常的没区别。展开发生在 `promotion.checkout._expand_category_scopes`
       （引擎是纯函数，读不了类目树）
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
| 风控 | 第一期用规则（同 IP 多账号、新号集中领券）判定，二期再接第三方风控 |
| 事后 | `coupon_receive_log` 按 IP/device 聚合分析，批量作废异常券 |

**假成功策略（重要）**：风控判定为刷子时，**返回"领取成功"，但不真正发券**——券写入一个"影子表"，风控人工复核后才决定是否放出。这样刷子无法通过"成功/失败"来探测风控规则，避免对抗升级。

## 9. 券过期处理

```sql
-- 分批处理，避免大事务（PG 的 UPDATE 不支持 LIMIT，用子查询限定批次）
UPDATE promotion.coupon_code
SET status = 4
WHERE id IN (
  SELECT id FROM promotion.coupon_code
  WHERE status = 1 AND valid_end < now()
  ORDER BY valid_end
  LIMIT 1000
  FOR UPDATE SKIP LOCKED          -- 与下单锁券并发时跳过正在被锁的行
);
-- ARQ cron 任务循环执行，直到 rowcount = 0
```

- 这条查询命中部分索引 `idx_coupon_code_expire (valid_end) WHERE status = 1`，只扫描未使用的券，不会全表扫。
- **过期前 3 天 / 1 天提醒**：定时任务扫 `valid_end BETWEEN now+23h AND now+24h AND status=1`，发推送。
- **过期是惰性 + 主动结合**：查询券列表时，若发现 `valid_end < now && status = 1`，顺手改状态（惰性）；同时定时任务主动跑（兜底）。

## 10. 对账：券的核心一致性检查

ARQ cron 任务，每 10 分钟跑一次：

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
| 客服手工补发券 | 走单独的 `POST /api/admin/coupons/issue` 接口，**不走 Redis stock 计数**（不占用活动额度），直接 INSERT coupon_code + log，source=2 |
| 发券活动提前终止 | 置 `status=3`，同时 `DEL coupon:tpl:{id}:meta` 让脚本返回 code 4 |
| 券模板修改了 threshold | 已发出的券**必须使用领取时的快照**。方案：`coupon_code` 增冗余列（threshold_snap, discount_value_snap），或者**规则禁止修改进行中的模板**（推荐，运营只能新建） |

**推荐做法**：优惠券模板一旦有券发出，**核心字段（面额、门槛、有效期、适用范围）不可修改**，只能作废后新建。这条规则消灭了一整类"已发券规则漂移"的 Bug。

## 12. 性能设计

| 指标 | 目标（第一期单机） | 手段 |
|---|---|---|
| 领券 TPS | 2000 | 纯 Redis Lua，无 DB 同步写（DB 异步化） |
| 领券 P99 | < 30ms | Lua 脚本多次 Redis 操作，一次网络往返 |
| 券列表 P99 | < 50ms | 用户券 Hash 缓存 + 模板进程内缓存 |
| 结算页券可用计算 | < 30ms | 分层过滤，前 3 层全内存 |

**高领券 TPS 的关键**：DB 写入不在请求链路上。Lua 成功后，把领券令牌 `XADD` 到 Redis Stream `stream:coupon.issue`，立即返回"领取成功"；worker 中的消费组读取后执行 §5.2 的 DB 事务生成券实例（用户 1–3 秒后能看到券即可）。消费失败的消息留在 PEL（待确认列表）中，由消费者定期 `XAUTOCLAIM` 重试，重试超过 5 次转入死信 Stream 人工处理。

> 第一期流量不大时，可以先**同步写 DB**（§5 的链路），等压测显示 DB 成为瓶颈再切到异步。两种模式由配置开关 `COUPON_ISSUE_ASYNC` 控制，Lua 脚本不变。

但要处理"领了但券还没生成，用户立刻去结算"的场景：**Redis 里先写入一个"预发券"标记**，结算时若发现券实例未生成但预发标记存在，则同步生成（补偿路径）。

```lua
-- 领券成功后立即写预发标记（同一个 Lua 脚本内或紧随其后）
SET coupon:pending:{userId}:{tplId}:{idemKey} {token} EX 300
```

结算时：`券列表 = DB已生成券 ∪ Redis预发券（触发同步补偿）`。
