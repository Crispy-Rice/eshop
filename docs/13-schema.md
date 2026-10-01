# 13 数据库 DDL 与数据字典（PostgreSQL 17）

> 本文档补充前文未完整列出的表结构、索引、分区与数据库配置。各业务表的完整 DDL 分布在对应文档中，这里给出全局约定与总览。

## 0. 全局约定

### 0.1 一库多 schema

单个数据库 `eshop`，**每个模块一个 schema**，模块只读写自己 schema 下的表（[01 §2](01-overview.md)）：

| schema | 模块 | 说明 |
|---|---|---|
| `account` | user | 账号、角色、店铺、地址、积分。不用 `user` 作 schema 名，因为它是 PG 保留字 |
| `product` | product | 类目、SPU、SKU、规格 |
| `inventory` | inventory | 仓库、库存、库存流水 |
| `cart` | cart | 购物车 |
| `promotion` | promotion | 券、活动、叠加规则 |
| `freight` | freight | 运费模板与规则 |
| `trade` | trade | 母单、子单、订单项、发货单、状态流水 |
| `payment` | payment | 支付单、回调日志、资金退款、对账、模拟渠道 |
| `aftersale` | aftersale | 售后单 |
| `review` | review | 评价、回复 |
| `settlement` | settlement | 商家账单（二期细化） |
| `notify` | notify | 站内信 |
| `core` | 基础设施 | 本地消息表（outbox） |
| `ops` | 运维 | 告警、降级开关 |

**外键**：同一 schema 内可以建外键；**跨 schema 不建外键**（如 `trade.order_item.sku_id` 不引用 `product.sku`），保持模块可拆分。

### 0.2 类型与命名

| 项 | 约定 | 说明 |
|---|---|---|
| 金额 | `BIGINT`，单位分 | 禁止 `NUMERIC` 存元、`REAL/DOUBLE`、`MONEY`（[11 §6.1](11-price-consistency.md)） |
| 时间 | `TIMESTAMPTZ(3)`，存 UTC | 连接参数 `timezone=UTC`；展示与"按日"统计时转 `Asia/Shanghai` |
| 主键 | 业务实体用雪花 `BIGINT`；流水/日志类用 `BIGINT GENERATED ALWAYS AS IDENTITY` | 雪花 ID 见 §0.4 |
| 状态/类型枚举 | `SMALLINT` | PG 没有 `TINYINT`；不用 PG 原生 `ENUM`（增删值需要 DDL，迁移不灵活） |
| 布尔 | `BOOLEAN` | |
| 半结构化 | `JSONB` | 不用 `JSON`（不可索引、保留重复键） |
| 字符串 | `VARCHAR(n)` 或 `TEXT` | PG 中两者性能相同，`VARCHAR(n)` 用来表达长度约束 |
| IP | `INET` | |
| 注释 | `COMMENT ON TABLE/COLUMN` | PG 不支持列定义内的 `COMMENT`，文档中用行尾 `--` 注释示意 |
| 约束命名 | `uk_表_列`、`ck_表_含义`、`idx_表_列` | 索引名在同一 schema 内必须唯一，所以带表名前缀 |
| 唯一性 + 可空列 | 唯一约束中 `NULL` 互不相等 | 需要"空值也算一个值"时用 `NOT NULL DEFAULT 0` 或 PG 15+ 的 `UNIQUE NULLS NOT DISTINCT` |

**建库**：

```sql
CREATE DATABASE eshop
  ENCODING 'UTF8'
  LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'
  TEMPLATE template0;
\c eshop
CREATE EXTENSION IF NOT EXISTS pg_trgm;              -- 商品/评价模糊搜索（02 §7）
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;   -- 慢 SQL 分析（需 shared_preload_libraries）
```

> 排序规则用 `C.UTF-8`：比较按码点进行，`LIKE 'M2026%'` 前缀查询可以直接用普通 B-tree 索引，行为也不随操作系统 locale 变化。代价是中文按 Unicode 码点而不是拼音排序——需要按拼音排序的场景（如类目名）在查询中显式指定 `ORDER BY name COLLATE "zh-x-icu"`（官方 Docker 镜像带 ICU）。

### 0.3 迁移

- 用 **Alembic** 管理，所有 DDL 必须走迁移脚本，禁止在生产库手工改表。
- 迁移以 `eshop_owner` 角色执行，应用以 `eshop_app` 角色运行（权限见 [16 §4](16-deployment.md)）。
- 大表加索引用 `CREATE INDEX CONCURRENTLY`（Alembic 中需 `op.get_context().autocommit_block()`）；加 `NOT NULL` 列必须带 `DEFAULT`（PG 11+ 不重写表）。
- 部署流程中迁移作为独立的一次性容器先执行，成功后再启动新版本应用（[16 §5](16-deployment.md)）。

### 0.4 雪花 ID

```
 1 bit  符号位(0) | 41 bit 毫秒时间戳（起点 2026-01-01） | 10 bit worker id | 12 bit 序列
```

- 每个**进程**都需要唯一的 worker id。Uvicorn 多 worker、`docker compose --scale` 多容器时，靠环境变量无法区分进程，因此 worker id 在进程启动时向 Redis **租用**：依次尝试 `SET snowflake:worker:{i} {hostname}:{pid} NX EX 60`（`i ∈ [0, 1024)`），成功后每 20 秒续期；续期失败（租约丢失）时进程停止发号并退出，由 Docker 重启后重新租用。
- 时钟回拨：检测到当前毫秒 < 上次发号毫秒时，回拨 ≤ 5ms 则等待，> 5ms 则拒绝发号并告警。
- JSON 中以字符串返回（[15 §1.3](15-api-and-errors.md)）。

## 1. 表清单总览

第一期单库，量级为 3 年预估，超过千万行的表在"策略"列给出处理方式。

| 模块 | 表名 | 说明 | 预估量级（3年） | 策略 |
|---|---|---|---|---|
| account | `user` | 用户账号 | 百万 | |
| | `refresh_token` | 刷新令牌（哈希存储） | 百万 | 定期清理过期 |
| | `shop` / `shop_member` | 店铺、商家账号与店铺关系 | 万 | |
| | `user_address` | 收货地址 | 百万 | |
| | `user_points` | 积分账户 | 百万 | |
| | `points_biz_key` / `points_flow` | 积分幂等键 / 积分流水 | 千万 | 流水按月分区 |
| product | `category` | 类目 | 万 | |
| | `spu` / `sku` | 商品 / 最小售卖单元 | 百万 | |
| | `spec_group` / `spec_value` / `sku_spec` | 规格 | 千万 | |
| | `spu_attr` | 商品参数 | 千万 | |
| inventory | `warehouse` | 仓库 | 千 | |
| | `sku_stock` | 分仓库存 | 百万 | `fillfactor = 80`（§9.2） |
| | `stock_biz_key` / `stock_flow` | 库存幂等键 / 库存流水 | 亿 | 流水按月分区；幂等键 90 天清理 |
| cart | `cart_item` | 购物车项 | 千万 | |
| promotion | `coupon_template` | 券模板 | 万 | |
| | `coupon_code` | 券实例 | 千万 | |
| | `coupon_user_quota` | 用户领取计数 | 千万 | |
| | `coupon_receive_log` / `coupon_flow` | 领券流水 / 券状态流水 | 千万 | |
| | `promo_activity` / `promo_stack_rule` | 促销活动 / 叠加规则 | 万 / 百 | |
| freight | `freight_template` / `freight_region_rule` / `sku_freight_bind` / `freight_exclude_region` | 运费 | 百万 | |
| trade | `order_main` / `order_sub` | 母单 / 子单 | 千万 | 3 年后归档（§7） |
| | `order_item` | 订单项 | 亿 | 同上 |
| | `order_discount_snapshot` | 优惠快照 | 亿 | 同上 |
| | `order_state_flow` | 状态流水 | 亿 | 只追加 |
| | `delivery_order` / `delivery_item` | 发货单 | 千万 | |
| payment | `payment` | 支付单 | 千万 | |
| | `pay_notify_log` | 回调日志 | 千万 | 3 个月删除 |
| | `payment_refund` | 资金退款单 | 百万 | |
| | `reconcile_diff` | 对账差异 | 万 | |
| | `mock_channel_trade` | 模拟渠道交易（仅 mock 模式） | — | |
| aftersale | `refund_order` / `refund_item` / `refund_logistics` | 售后 | 百万 | |
| review | `review` / `review_reply` / `stat_biz_key` | 评价 | 千万 | |
| notify | `site_message` | 站内信 | 千万 | 1 年删除 |
| core | `local_message` | 本地消息表（outbox） | 千万/周 | 7 天删除 |
| ops | `alert` / `switch` | 告警 / 降级开关 | 万 / 十 | |

## 2. 用户域

```sql
CREATE TABLE account."user" (
  id             BIGINT       PRIMARY KEY,
  phone_hash     CHAR(64)     NOT NULL,          -- HMAC-SHA256(手机号, PHONE_HASH_KEY)，用于登录查询与唯一性
  phone_cipher   BYTEA        NOT NULL,          -- AES-GCM(手机号, PHONE_ENC_KEY)，用于展示/客服
  phone_masked   VARCHAR(20)  NOT NULL,          -- 138****8888，列表展示用，无需解密
  password_hash  VARCHAR(255) NOT NULL,          -- Argon2id
  nickname       VARCHAR(64)  NOT NULL,
  avatar         VARCHAR(255),
  gender         SMALLINT     NOT NULL DEFAULT 0, -- 0未知 1男 2女
  birthday       DATE,
  member_level   SMALLINT     NOT NULL DEFAULT 1,
  member_expire  TIMESTAMPTZ(3),
  credit_score   INT          NOT NULL DEFAULT 700, -- 信用分（售后用）
  role           VARCHAR(16)  NOT NULL DEFAULT 'buyer', -- buyer/merchant/admin/finance
  status         SMALLINT     NOT NULL DEFAULT 1, -- 1正常 2冻结 3注销
  failed_logins  SMALLINT     NOT NULL DEFAULT 0,
  locked_until   TIMESTAMPTZ(3),
  register_time  TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  created_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT uk_user_phone_hash UNIQUE (phone_hash)
);
CREATE INDEX idx_user_status_time ON account."user" (status, register_time);

CREATE TABLE account.user_address (
  id             BIGINT        PRIMARY KEY,
  user_id        BIGINT        NOT NULL REFERENCES account."user" (id),
  receiver_name  VARCHAR(64)   NOT NULL,
  phone          VARCHAR(20)   NOT NULL,          -- 收货电话（随订单快照，第一期明文，展示时脱敏）
  province       VARCHAR(32)   NOT NULL,
  city           VARCHAR(32)   NOT NULL,
  district       VARCHAR(32)   NOT NULL,
  detail         VARCHAR(255)  NOT NULL,
  region_code    VARCHAR(16)   NOT NULL,          -- 行政区划码，运费计算用
  tag            VARCHAR(16),                     -- 家/公司/学校
  is_default     BOOLEAN       NOT NULL DEFAULT false,
  status         SMALLINT      NOT NULL DEFAULT 1,
  created_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now()
);
CREATE INDEX idx_user_address_user ON account.user_address (user_id, status);
-- 每个用户最多一个默认地址
CREATE UNIQUE INDEX uk_user_address_default ON account.user_address (user_id) WHERE is_default AND status = 1;
```

> 表名 `user` 是保留字，必须写成 `account."user"`。SQLAlchemy 模型中设置 `__tablename__ = "user"`、`__table_args__ = {"schema": "account"}`，生成的 SQL 会自动加引号。

**`phone_hash` 的作用**：手机号是敏感信息，需要加密存储，但加密后无法用 `WHERE phone = ?` 查询。解决方案：存一份**确定性哈希**（HMAC-SHA256，密钥 `PHONE_HASH_KEY`）用于查询与唯一约束，原文用 AES-GCM 加密（密钥 `PHONE_ENC_KEY`，`cryptography` 库）存储用于客服查看。两个密钥都在服务器 `.env` 中（[16 §8](16-deployment.md)），**丢失 `PHONE_ENC_KEY` 将无法解密历史手机号**，必须单独备份。

`refresh_token`、`shop`、`shop_member`、积分相关表的 DDL 见 [08 §5.1](08-aftersale.md)（积分）与代码中的 Alembic 迁移，这里不再展开。

## 3. 售后域

```sql
CREATE TABLE aftersale.refund_order (
  id                   BIGINT       PRIMARY KEY,
  refund_no            VARCHAR(32)  NOT NULL,       -- 售后单号
  order_sub_no         VARCHAR(32)  NOT NULL,
  order_main_no        VARCHAR(32)  NOT NULL,
  user_id              BIGINT       NOT NULL,
  shop_id              BIGINT       NOT NULL,
  refund_type          SMALLINT     NOT NULL,       -- 1仅退款 2退货退款 3换货
  reason_type          SMALLINT     NOT NULL,       -- 1质量问题 2不想要 3发错货 4少发 5假货 6其他
  reason_desc          VARCHAR(255),
  images               JSONB        NOT NULL DEFAULT '[]'::jsonb, -- 凭证图片
  -- 金额（商品款与运费分开记，见 08 §2.1）
  refund_amount        BIGINT       NOT NULL CHECK (refund_amount >= 0),  -- 商品退款金额（不含运费）
  refund_freight       BIGINT       NOT NULL DEFAULT 0 CHECK (refund_freight >= 0), -- 退还的运费
  freight_bearer       SMALLINT     NOT NULL DEFAULT 2, -- 退货运费承担方 1用户 2商家 3平台
  refund_points        BIGINT       NOT NULL DEFAULT 0,
  -- 状态机
  status               SMALLINT     NOT NULL DEFAULT 10, -- 见 08 §3.1
  source_status        SMALLINT     NOT NULL,       -- ★ 申请时子单的状态，拒绝/撤销时恢复
  -- 处理
  merchant_remark      VARCHAR(255),
  platform_remark      VARCHAR(255),
  reject_reason        VARCHAR(255),
  -- 质检
  quality_result       SMALLINT,                    -- 1合格 2不合格 3部分合格
  quality_remark       VARCHAR(255),
  quality_images       JSONB        NOT NULL DEFAULT '[]'::jsonb,
  -- 物流
  return_express       VARCHAR(32),                 -- 退货快递公司
  return_express_no    VARCHAR(64),                 -- 退货单号
  -- 时间节点
  apply_time           TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  merchant_handle_time TIMESTAMPTZ(3),
  return_time          TIMESTAMPTZ(3),
  receive_time         TIMESTAMPTZ(3),
  quality_time         TIMESTAMPTZ(3),
  refund_time          TIMESTAMPTZ(3),              -- 退款完成时间
  close_time           TIMESTAMPTZ(3),
  deadline             TIMESTAMPTZ(3),              -- ★ 当前环节的截止时间（驱动超时任务），终态为 NULL
  created_at           TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  version              INT          NOT NULL DEFAULT 0,
  CONSTRAINT uk_refund_no UNIQUE (refund_no)
);
CREATE INDEX idx_refund_user_status ON aftersale.refund_order (user_id, status, created_at DESC);
CREATE INDEX idx_refund_shop_status ON aftersale.refund_order (shop_id, status, created_at DESC);
CREATE INDEX idx_refund_main        ON aftersale.refund_order (order_main_no);
CREATE INDEX idx_refund_sub         ON aftersale.refund_order (order_sub_no);
-- ★ 超时扫描：只索引有截止时间的（进行中的）售后单
CREATE INDEX idx_refund_deadline    ON aftersale.refund_order (status, deadline) WHERE deadline IS NOT NULL;
-- ★ 同一子单同时只能有一个进行中的售后
CREATE UNIQUE INDEX uk_refund_sub_active ON aftersale.refund_order (order_sub_no)
  WHERE status IN (10, 20, 30, 40, 50, 51, 60, 90);

CREATE TABLE aftersale.refund_item (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  refund_no        VARCHAR(32)  NOT NULL REFERENCES aftersale.refund_order (refund_no),
  order_item_id    BIGINT       NOT NULL,
  sku_id           BIGINT       NOT NULL,
  refund_num       INT          NOT NULL CHECK (refund_num > 0),
  refund_amount    BIGINT       NOT NULL CHECK (refund_amount >= 0), -- 该行商品退款金额
  refund_points    BIGINT       NOT NULL DEFAULT 0,
  -- 快照
  spu_title_snap   VARCHAR(120) NOT NULL,
  sku_spec_snap    VARCHAR(255) NOT NULL,
  cover_image_snap VARCHAR(255) NOT NULL,
  -- 库存恢复标记
  stock_restored   BOOLEAN      NOT NULL DEFAULT false, -- ★ 库存是否已恢复（便于排查，幂等由 stock_biz_key 保证）
  restore_time     TIMESTAMPTZ(3),
  CONSTRAINT uk_refund_item UNIQUE (refund_no, order_item_id)
);
CREATE INDEX idx_refund_item_order_item ON aftersale.refund_item (order_item_id);
```

**`uk_refund_sub_active`**：原方案只有 `has_aftersale` 标志位，并发申请时可能同时创建两个售后单。PG 的部分唯一索引直接表达"一个子单同时只有一个进行中的售后"，终态（11 商家拒绝、70 成功、80 关闭、81 撤销）不占用。

**`deadline` 字段的价值**：把"当前环节该在什么时候完成"显式存下来，超时扫描只需要 `WHERE status = :s AND deadline < now()`，一个部分索引搞定所有超时场景，不用为每个环节写不同的时间字段判断。

```python
# 状态流转时一并更新 deadline
DEADLINES: dict[RefundStatus, timedelta] = {
    RefundStatus.APPLYING: timedelta(hours=48),         # 商家 48h 内审核
    RefundStatus.WAIT_RETURN: timedelta(days=7),        # 用户 7 天内寄回
    RefundStatus.WAIT_RECEIVE: timedelta(days=7),       # 商家 7 天内收货
    RefundStatus.QUALITY_CHECKING: timedelta(hours=48), # 质检 48h
}

def next_deadline(status: RefundStatus, now: datetime) -> datetime | None:
    delta = DEADLINES.get(status)
    return now + delta if delta else None               # 其他状态不超时（NULL，不进部分索引）
```

## 4. 本地消息表（outbox，最终一致的基础设施）

```sql
CREATE TABLE core.local_message (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  topic         VARCHAR(64)  NOT NULL,        -- 如 trade.order_paid，对应 Redis Stream 名 stream:{topic}
  biz_key       VARCHAR(160) NOT NULL,        -- ★ 幂等键，消费者据此去重
  payload       JSONB        NOT NULL,
  status        SMALLINT     NOT NULL DEFAULT 0, -- 0待发送 1已发送 2发送失败(待重试) 3放弃(超过重试上限)
  retry_count   INT          NOT NULL DEFAULT 0,
  next_retry_at TIMESTAMPTZ(3) NOT NULL DEFAULT now(), -- ★ 下次重试时间（指数退避）
  error_msg     VARCHAR(512),
  created_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT uk_local_message_biz UNIQUE (topic, biz_key)   -- 同一事件不会被写入两次
);
-- ★ 扫描待发送消息：部分索引只包含未完成的消息，体积始终很小
CREATE INDEX idx_local_message_pending ON core.local_message (next_retry_at) WHERE status IN (0, 2);
COMMENT ON TABLE core.local_message IS '本地消息表（事务消息的替代）';
```

**写入**（`app/core/outbox.py`）：在业务事务内执行，与业务数据一起提交。

```python
async def add(session: AsyncSession, *, topic: str, biz_key: str, payload: dict) -> None:
    await session.execute(
        pg_insert(LocalMessage)
        .values(topic=topic, biz_key=biz_key, payload=payload)
        .on_conflict_do_nothing(constraint="uk_local_message_biz")
    )
```

**工作方式**：

```
① 业务事务内：INSERT core.local_message (status=0)
   → 与业务数据同一个事务，保证"业务成功则消息一定存在"

② 投递循环（worker 进程中的常驻协程，每 1 秒一轮；api 在事务提交后也会立即触发一轮，降低延迟）
   多个 worker 并发投递时用 SKIP LOCKED 互不阻塞

③ XADD 到 Redis Stream stream:{topic}，成功则 status = 1

④ 消费者幂等消费（用 biz_key 去重），见 14 §6

⑤ 失败重试：retry_count++，next_retry_at = now + min(2^retry * 10s, 1h)；
   超过 15 次置为 3，写 ops.alert 告警
```

**投递 SQL**：

```sql
-- 在一个事务中：锁定一批 → 逐条 XADD → 更新状态 → 提交
SELECT id, topic, biz_key, payload
FROM core.local_message
WHERE status IN (0, 2) AND next_retry_at <= now()
ORDER BY id
LIMIT 200
FOR UPDATE SKIP LOCKED;

-- XADD 成功
UPDATE core.local_message SET status = 1, updated_at = now() WHERE id = ANY(:ids);
```

`XADD` 成功但事务提交失败时，消息会被再次投递——这就是"至少一次"语义，由消费端 `biz_key` 去重兜底。

**指数退避的 SQL**：

```sql
UPDATE core.local_message
SET status        = CASE WHEN retry_count + 1 >= 15 THEN 3 ELSE 2 END,
    retry_count   = retry_count + 1,
    next_retry_at = now() + make_interval(secs => LEAST(power(2, retry_count) * 10, 3600)),
    error_msg     = :err,
    updated_at    = now()
WHERE id = :id;
```

## 5. 索引设计要点

### 5.1 订单表的核心索引

```sql
-- 用户订单列表（最常用）
CREATE INDEX idx_order_sub_user ON trade.order_sub (user_id, status, create_time DESC);
-- 查询："我的待付款订单，按时间倒序"
-- WHERE user_id = :uid AND status = 10 ORDER BY create_time DESC
-- ★ 等值条件列在前，排序/范围列放最后

-- 商家订单列表
CREATE INDEX idx_order_sub_shop ON trade.order_sub (shop_id, status, create_time DESC);

-- 超时扫描：部分索引
CREATE INDEX idx_order_main_pay_deadline ON trade.order_main (pay_deadline) WHERE status = 10;
-- WHERE status = 10 AND pay_deadline < now()
```

**为什么超时扫描用部分索引而不是 `(status, pay_deadline)` 或单独 `pay_deadline`**：

- 单独 `pay_deadline`：会扫到所有历史订单（包括已完成的），因为绝大多数订单的 `pay_deadline` 都早于当前时间。
- `(status, pay_deadline)` 复合索引：能快速定位，但索引里包含**所有**订单的条目，体积随订单总量增长。
- **部分索引 `WHERE status = 10`**：索引里**只有**待付款订单（最近 30 分钟内的少量记录），订单支付或关闭后条目自动移出。扫描量和索引体积都极小。

同样的思路用于：未使用券的过期扫描、进行中售后的超时扫描、待发送 outbox 消息、待处理回调日志。

### 5.2 避免索引失效的写法（PostgreSQL 版）

```sql
-- ❌ 索引失效：列上用了函数（且按 UTC 切日，结果也不对）
WHERE date(create_time) = '2026-09-30'

-- ✅ 范围查询（边界按东八区换算）
WHERE create_time >= '2026-09-30 00:00:00+08'
  AND create_time <  '2026-10-01 00:00:00+08'

-- ❌ 索引失效：参数类型与列类型不一致，PG 会把列转换成参数的类型
--    例：bigint 列与 numeric 参数比较（Python 传了 Decimal）
WHERE user_id = 88001.0
-- 注意：未指定类型的字面量 '88001' 会被当作 bigint，这一点与 MySQL 不同，不会失效

-- ❌ 索引失效：前导模糊
WHERE order_main_no LIKE '%1234'
-- （需要后缀/包含匹配时用 pg_trgm 的 GIN 索引）

-- ✅ 前缀匹配：数据库排序规则为 C.UTF-8（§0.2），普通 B-tree 索引即可支持
WHERE order_main_no LIKE 'M20260930%'

-- ⚠️ OR 连接不同列：PG 可以用 BitmapOr 合并两个索引，不一定失效，但代价高于单索引，
--    高频查询仍应改写为 UNION ALL 或拆成两次查询
WHERE user_id = 1 OR shop_id = 2
```

**验证手段**：所有新增的高频查询在 code review 时附上 `EXPLAIN (ANALYZE, BUFFERS)` 输出；`pg_stat_statements` 每周看一次 Top 20 慢 SQL。

### 5.3 联合索引的最左前缀

```sql
-- 索引：(user_id, status, create_time)

-- ✅ 命中
WHERE user_id = 1
WHERE user_id = 1 AND status = 10
WHERE user_id = 1 AND status = 10 AND create_time > '...'

-- ⚠️ 部分命中（只用 user_id 定位，create_time 作为索引内过滤条件）
WHERE user_id = 1 AND create_time > '...'

-- ❌ 基本不命中（PG 17 不支持 skip scan；PG 18 起对前导列基数低的情况可跳跃扫描）
WHERE status = 10
WHERE create_time > '...'
```

## 6. 分区与将来的分片

第一期**不分库分表**（[01 §8](01-overview.md)）。PostgreSQL 在合理索引下单表支撑千万到亿级行没有问题，真正的瓶颈出现之前，不引入分片的复杂度。

### 6.1 现在就做的：按月分区的流水表

| 表 | 分区键 | 说明 |
|---|---|---|
| `inventory.stock_flow` | `created_at` RANGE 按月 | 幂等键已拆到 `stock_biz_key`（[03 §8](03-inventory.md)） |
| `account.points_flow` | `created_at` RANGE 按月 | 幂等键已拆到 `points_biz_key` |

**分区表的限制**：主键和唯一约束必须包含分区键。所以"全局唯一的幂等键"不能放在分区表上，必须拆成独立的小表——这是两张 `*_biz_key` 表存在的原因。`pay_notify_log` 需要全局去重，因此不分区，改为按时间批量删除。

未来分区由 ARQ cron 每月 25 日提前创建：

```sql
CREATE TABLE IF NOT EXISTS inventory.stock_flow_202612 PARTITION OF inventory.stock_flow
  FOR VALUES FROM ('2026-12-01 00:00:00+00') TO ('2027-01-01 00:00:00+00');
```

另建一个 `DEFAULT` 分区兜底（防止分区忘建导致写入失败），并对 `DEFAULT` 分区非空告警。

### 6.2 将来再做的：分片

为将来按 `user_id` 分片保留的约定（现在遵守，成本为零）：

- 订单、支付、售后表都带 `user_id`，用户侧查询都带上它；
- 母单、子单、订单项、支付单、售后单之间的关联查询，都能以 `user_id` 为条件；
- 订单号带日期前缀，可按时间路由。

届时可选方案：Citus 扩展（`user_id` 为分布键，母子单等表 co-locate），或腾讯云 TDSQL-C PostgreSQL 版的读写分离 + 应用层路由。商家按 `shop_id` 查订单的跨分片问题届时再引入只读副本或分析库。

## 7. 归档策略

| 表 | 在线保留 | 归档方式 |
|---|---|---|
| `order_main` / `order_sub` / `order_item` / `order_discount_snapshot` | 3 年 | 移入 `archive` schema 的同结构表（`INSERT ... SELECT` + 分批 `DELETE`），再 `pg_dump` 导出存档 |
| `stock_flow` | 6 个月 | `DETACH PARTITION` → `pg_dump` 导出 → `DROP TABLE` |
| `points_flow` | 2 年 | 同上 |
| `stock_biz_key` / `points_biz_key` | 90 天 | 分批删除（对应业务早已终结） |
| `pay_notify_log` | 3 个月 | 分批删除（回调原文仅用于排查） |
| `reconcile_diff` | 3 年 | 财务凭证，长期保留 |
| `local_message` | 7 天 | 已发送的分批删除 |
| `cart_item` | 永久（用户自己删） | - |

**归档的实现**：

```sql
-- 分区表：先摘下分区（秒级，不影响在线写入），导出后删除
ALTER TABLE inventory.stock_flow DETACH PARTITION inventory.stock_flow_202603 CONCURRENTLY;
-- 宿主机执行：pg_dump -t inventory.stock_flow_202603 ... > stock_flow_202603.dump
DROP TABLE inventory.stock_flow_202603;

-- 非分区表：分批 DELETE（PG 的 DELETE 不支持 LIMIT，用子查询限定批次，避免大事务和长时间锁）
DELETE FROM core.local_message
WHERE id IN (
  SELECT id FROM core.local_message
  WHERE status = 1 AND updated_at < now() - interval '7 days'
  ORDER BY id
  LIMIT 2000
);
-- ARQ cron 循环执行直到 rowcount = 0，每批之间 sleep 100ms
```

大量删除后表会膨胀，依赖 autovacuum 回收空间；`local_message` 这类高频写删的表单独调低 `autovacuum_vacuum_scale_factor`（§9.2）。

## 8. 数据字典：关键枚举

枚举统一定义在 `app/core/enums.py`，数据库存 `SMALLINT` 值；前端从 OpenAPI 生成的类型中获取同名常量。

```python
class SubOrderStatus(IntEnum):
    WAIT_PAY = 10
    WAIT_DELIVER = 20
    WAIT_RECEIVE = 30
    FINISHED = 40
    CLOSED = 50
    REFUNDING = 60
    REFUNDED = 70


class RefundStatus(IntEnum):
    APPLYING = 10
    MERCHANT_REJECTED = 11
    WAIT_REFUND = 20
    WAIT_RETURN = 30
    WAIT_RECEIVE = 40
    QUALITY_CHECKING = 50
    QUALITY_FAILED = 51
    REFUNDING = 60
    SUCCESS = 70
    CLOSED = 80
    USER_REVOKED = 81
    PLATFORM_INTERVENING = 90


class PayStatus(IntEnum):
    WAIT_PAY = 0
    PAYING = 1
    SUCCESS = 2
    FAILED = 3
    CLOSED = 4
    REFUNDED = 5        # 已全额退款


class ChannelType(IntEnum):
    MOCK = 0
    WECHAT = 1
    ALIPAY = 2


class CouponStatus(IntEnum):
    UNUSED = 1
    LOCKED = 2
    USED = 3
    EXPIRED = 4
    VOIDED = 5


class StockChangeType(IntEnum):
    LOCK = 1
    CONFIRM = 2
    RELEASE = 3
    DELIVER = 4
    RETURN_IN = 5
    MANUAL = 6
    INIT = 7


class PointsChangeType(IntEnum):
    ORDER_USE = 1
    ORDER_FREEZE = 2
    PAY_CONFIRM = 3
    REFUND = 4
    SHOPPING_AWARD = 5
    REVIEW_AWARD = 6
    EXPIRE = 7
    SIGN_IN = 8
    MANUAL = 9


class ReviewStatus(IntEnum):
    PENDING_AUDIT = 0
    PUBLISHED = 1
    BLOCKED = 2
    REJECTED = 3
```

SQLAlchemy 模型中这些字段声明为 `Mapped[SubOrderStatus] = mapped_column(SmallInteger)`，并用 `TypeDecorator` 在读写时与 `IntEnum` 互转，避免业务代码里出现裸数字。

## 9. 数据库配置基线

### 9.1 postgresql.conf（4C8G 单机，与应用同机部署）

PG 容器的内存预算约 3GB（其余留给 api、worker、Redis 与系统）：

```conf
# deploy/postgres/postgresql.conf
listen_addresses = '*'                  # 容器内监听；对外只通过 docker 网络 / 127.0.0.1 端口映射暴露
max_connections = 100                   # api 4 进程 × 池 10 + worker 池 10 + 运维余量
shared_buffers = 1GB
effective_cache_size = 2560MB
work_mem = 16MB
maintenance_work_mem = 256MB
wal_buffers = 16MB
random_page_cost = 1.1                  # SSD 云硬盘
effective_io_concurrency = 200

# 持久性（资金数据，不可放宽）
fsync = on
synchronous_commit = on
full_page_writes = on

# WAL 与检查点
wal_level = replica                     # 为将来加只读副本、做 PITR 预留
max_wal_size = 2GB
checkpoint_completion_target = 0.9

# 时区与超时
timezone = 'UTC'
idle_in_transaction_session_timeout = '60s'   # 防止事务忘记提交长时间持锁
lock_timeout = '5s'

# 日志与统计
shared_preload_libraries = 'pg_stat_statements'
log_min_duration_statement = 500        # 慢 SQL：500ms
log_lock_waits = on
log_checkpoints = on
log_autovacuum_min_duration = '1s'
```

应用角色单独设置语句超时，迁移角色不受限：

```sql
ALTER ROLE eshop_app SET statement_timeout = '5s';
```

**事务隔离级别**：PostgreSQL 默认就是 `READ COMMITTED`，无需修改。原方案中"用 RC 代替 RR 减少间隙锁"的考虑在 PG 中不存在——PG 基于 MVCC，**没有 InnoDB 那样的间隙锁**。本方案的一致性依赖**显式的条件更新（`WHERE available >= n`）、`CHECK` 约束、唯一约束和 `SELECT ... FOR UPDATE`**，不依赖更高的隔离级别。

**死锁**：PG 仍会因为行锁顺序不一致而死锁（自动检测并回滚其中一个事务，报 `40P01`）。批量更新库存时按 `(warehouse_id, sku_id)` 排序（[03 §5.1](03-inventory.md)）；应用对 `40P01` 与 `40001` 做有限次重试（最多 3 次，带随机退避）。

### 9.2 高频更新表的存储参数

```sql
-- 库存、券模板这类"同一行被频繁 UPDATE"的表：预留页内空间，让更新尽量走 HOT（不更新索引）
ALTER TABLE inventory.sku_stock       SET (fillfactor = 80);
ALTER TABLE promotion.coupon_template SET (fillfactor = 80);

-- 高频写删的队列型表：更积极地 vacuum
ALTER TABLE core.local_message SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE payment.pay_notify_log SET (autovacuum_vacuum_scale_factor = 0.05);
```

`sku_stock` 上不要给 `available`、`locked`、`frozen` 建索引：被索引的列一旦更新就不能走 HOT，热点 SKU 的扣减会产生大量索引写入。

### 9.3 连接池

- 应用侧用 SQLAlchemy 的连接池（`pool_size=10, max_overflow=5, pool_pre_ping=True, pool_recycle=1800`），每个 Uvicorn 进程一个池。
- 第一期不引入 PgBouncer。进程数增加到连接数逼近 `max_connections` 时再加（届时注意 asyncpg 的预编译语句缓存与 PgBouncer transaction 模式的兼容性，需设置 `statement_cache_size=0` 或使用 PgBouncer 1.21+ 的 `max_prepared_statements`）。

## 10. 对账 SQL 清单（每日跑）

由 ARQ cron 在每日 04:00 执行，任何一条有结果都写入 `ops.alert`（[08 §10](08-aftersale.md)）。带 `CHECK` 约束的项在正常情况下不可能出现，保留它们是为了发现"约束被误删"或"数据被绕过应用直接修改"。

```sql
-- ① 库存恒等式（CHECK 约束已保证，这里做监控）
SELECT sku_id, warehouse_id, total, available, locked, frozen
FROM inventory.sku_stock
WHERE total <> available + locked + frozen
   OR available < 0 OR locked < 0 OR frozen < 0;
-- 必须为空

-- ② 券不超发
SELECT id, name, issued_count, total_count
FROM promotion.coupon_template
WHERE issued_count > total_count;
-- 必须为空

-- ②' 券发放计数与实例数一致
SELECT t.id, t.issued_count, COUNT(c.id) AS actual
FROM promotion.coupon_template t
LEFT JOIN promotion.coupon_code c
       ON c.coupon_template_id = t.id AND c.source <> 2   -- 客服补发不占活动额度（04 §11）
GROUP BY t.id, t.issued_count
HAVING t.issued_count <> COUNT(c.id);
-- 必须为空

-- ③ 订单金额守恒
SELECT order_main_no, payable_amount,
       total_amount - item_discount - shop_discount - platform_discount
                    - point_deduction + freight_amount AS calc
FROM trade.order_main
WHERE payable_amount <> total_amount - item_discount - shop_discount
                       - platform_discount - point_deduction + freight_amount;
-- 必须为空

-- ④ 母子单金额守恒
SELECT m.order_main_no, m.payable_amount, SUM(s.payable_amount) AS sub_sum
FROM trade.order_main m
JOIN trade.order_sub s ON m.order_main_no = s.order_main_no
GROUP BY m.order_main_no, m.payable_amount
HAVING m.payable_amount <> SUM(s.payable_amount);
-- 必须为空

-- ⑤ 订单项分摊守恒（PG 的 HAVING 不能引用 SELECT 别名，也不能引用未分组的列，故先聚合再比较）
SELECT s.order_sub_no, i.item_disc,
       s.item_discount + s.shop_discount + s.platform_discount + s.point_deduction AS sub_disc
FROM trade.order_sub s
JOIN (
  SELECT order_sub_no, SUM(discount_amount) AS item_disc
  FROM trade.order_item
  GROUP BY order_sub_no
) i ON i.order_sub_no = s.order_sub_no
WHERE i.item_disc <> s.item_discount + s.shop_discount + s.platform_discount + s.point_deduction;
-- 必须为空

-- ⑥ 退款不超付
SELECT order_sub_no, payable_amount, refunded_amount
FROM trade.order_sub
WHERE refunded_amount > payable_amount;
-- 必须为空

-- ⑦ 积分账实相符（全量较慢，按 user_id 取模抽样，每天跑 1/7）
SELECT up.user_id, up.balance, COALESCE(f.flow_sum, 0) AS flow_sum
FROM account.user_points up
LEFT JOIN (
  SELECT user_id, SUM(points) AS flow_sum
  FROM account.points_flow
  WHERE user_id % 7 = :bucket
  GROUP BY user_id
) f ON f.user_id = up.user_id
WHERE up.user_id % 7 = :bucket
  AND up.balance <> COALESCE(f.flow_sum, 0);
-- 必须为空（注意：已归档的积分流水分区需要以"期初余额"记录参与计算，归档时写入一条汇总流水）

-- ⑧ 支付单与订单状态一致
SELECT p.pay_no, p.status AS pay_status, m.status AS order_status, m.pay_status
FROM payment.payment p
JOIN trade.order_main m ON p.order_main_no = m.order_main_no
WHERE (p.status IN (0, 1) AND m.status = 50 AND p.create_time < now() - interval '1 hour')
                                                -- 订单已关闭但支付单仍待支付/支付中
   OR (p.status = 2 AND m.status = 10);        -- 支付成功但订单仍待付款
-- 必须为空（"订单已关闭 + 支付成功"是 09 §7 的晚付场景，m.pay_status = 1，不在此列）

-- ⑨ outbox 积压与放弃
SELECT topic, status, COUNT(*), MIN(created_at)
FROM core.local_message
WHERE (status IN (0, 2) AND created_at < now() - interval '10 minutes') OR status = 3
GROUP BY topic, status;
-- 必须为空

-- ⑩ DEFAULT 分区不应有数据（说明月度分区没有按时创建）
SELECT 'stock_flow' AS t, COUNT(*) FROM inventory.stock_flow_default
UNION ALL
SELECT 'points_flow', COUNT(*) FROM account.points_flow_default;
-- 必须都为 0
```
