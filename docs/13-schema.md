# 13 数据库 DDL 与数据字典

> 本文档补充前文未完整列出的表结构、索引与分片规划。
> 金额字段统一 `BIGINT`（单位：分），时间统一 `DATETIME(3)`。

## 1. 表清单总览

| 域 | 表名 | 说明 | 分片键 | 预估量级（3年） |
|---|---|---|---|---|
| 用户 | `user` | 用户账号 | user_id | 千万 |
| | `user_address` | 收货地址 | user_id | 亿 |
| | `user_points` | 积分账户 | user_id | 千万 |
| | `points_flow` | 积分流水 | user_id | 百亿 |
| 商品 | `category` | 类目 | - | 万 |
| | `spu` | 商品 | - | 千万 |
| | `sku` | 最小售卖单元 | - | 亿 |
| | `spec_group` / `spec_value` / `sku_spec` | 规格 | - | 亿 |
| | `spu_attr` | 商品参数 | - | 十亿 |
| 库存 | `sku_stock` | 分仓库存 | sku_id | 亿 |
| | `stock_flow` | 库存流水 | sku_id + 月分区 | 百亿 |
| | `warehouse` | 仓库 | - | 万 |
| 购物车 | `cart_item` | 购物车项 | user_id | 十亿 |
| 营销 | `coupon_template` | 券模板 | - | 百万 |
| | `coupon_code` | 券实例 | coupon_template_id | 百亿 |
| | `coupon_receive_log` | 领券流水 | - | 百亿 |
| | `promo_activity` | 促销活动 | - | 百万 |
| | `promo_stack_rule` | 叠加规则 | - | 百 |
| 运费 | `freight_template` | 运费模板 | - | 百万 |
| | `freight_region_rule` | 区域规则 | - | 千万 |
| | `sku_freight_bind` | SKU-模板绑定 | sku_id | 亿 |
| | `freight_exclude_region` | 排除区域 | - | 百万 |
| 订单 | `order_main` | 母单 | user_id | 十亿 |
| | `order_sub` | 子单 | user_id | 十亿 |
| | `order_item` | 订单项 | order_sub_no | 百亿 |
| | `order_discount_snapshot` | 优惠快照 | order_main_no | 百亿 |
| | `order_state_flow` | 状态流水 | order_no | 百亿 |
| | `delivery_order` | 发货单 | order_sub_no | 十亿 |
| | `delivery_item` | 发货明细 | delivery_no | 百亿 |
| 支付 | `payment` | 支付单 | order_main_no | 十亿 |
| | `pay_notify_log` | 回调日志 | - | 百亿 |
| | `payment_refund` | 资金退款单 | order_main_no | 十亿 |
| | `reconcile_diff` | 对账差异 | - | 千万 |
| 售后 | `refund_order` | 售后单 | user_id | 十亿 |
| | `refund_item` | 售后明细 | refund_no | 百亿 |
| | `refund_logistics` | 退货物流 | refund_no | 十亿 |
| 评论 | `review` | 评价 | spu_id | 十亿 |
| | `review_reply` | 评价回复 | review_id | 百亿 |
| 基础 | `idempotent_record` | 幂等记录（可选，DB 版） | - | 百亿 |
| | `local_message` | 本地消息表 | - | 百亿 |

## 2. 用户域

```sql
CREATE TABLE `user` (
  `id`            BIGINT       NOT NULL,
  `phone`         VARCHAR(20)  NOT NULL,
  `phone_hash`    VARCHAR(64)  NOT NULL COMMENT '手机号哈希，用于加密查询',
  `nickname`      VARCHAR(64)  NOT NULL,
  `avatar`        VARCHAR(255) DEFAULT NULL,
  `gender`        TINYINT      NOT NULL DEFAULT 0 COMMENT '0未知 1男 2女',
  `birthday`      DATE         DEFAULT NULL,
  `member_level`  TINYINT      NOT NULL DEFAULT 1 COMMENT '会员等级',
  `member_expire` DATETIME(3)  DEFAULT NULL,
  `credit_score`  INT          NOT NULL DEFAULT 700 COMMENT '信用分（售后用）',
  `status`        TINYINT      NOT NULL DEFAULT 1 COMMENT '1正常 2冻结 3注销',
  `register_time` DATETIME(3)  NOT NULL,
  `created_at`    DATETIME(3)  NOT NULL,
  `updated_at`    DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_phone` (`phone`),
  UNIQUE KEY `uk_phone_hash` (`phone_hash`),
  KEY `idx_status_time` (`status`, `register_time`)
) ENGINE=InnoDB COMMENT='用户';

CREATE TABLE `user_address` (
  `id`            BIGINT       NOT NULL,
  `user_id`       BIGINT       NOT NULL,
  `receiver_name` VARCHAR(64)  NOT NULL,
  `phone`         VARCHAR(20)  NOT NULL,
  `province`      VARCHAR(32)  NOT NULL,
  `city`          VARCHAR(32)  NOT NULL,
  `district`      VARCHAR(32)  NOT NULL,
  `detail`        VARCHAR(255) NOT NULL,
  `region_code`   VARCHAR(16)  NOT NULL COMMENT '行政区划码，运费计算用',
  `longitude`     DECIMAL(10,7) DEFAULT NULL,
  `latitude`      DECIMAL(10,7) DEFAULT NULL,
  `tag`           VARCHAR(16)  DEFAULT NULL COMMENT '家/公司/学校',
  `is_default`    TINYINT      NOT NULL DEFAULT 0,
  `status`        TINYINT      NOT NULL DEFAULT 1,
  `created_at`    DATETIME(3)  NOT NULL,
  `updated_at`    DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_user` (`user_id`, `status`),
  KEY `idx_phone` (`phone`)
) ENGINE=InnoDB COMMENT='收货地址';
```

**`phone_hash` 的作用**：手机号是敏感信息，需要加密存储。但加密后无法用 `WHERE phone = ?` 查询。解决方案：存一份**确定性哈希**（HMAC-SHA256 加固定盐）用于查询，原文用 AES 加密存储用于展示。

## 3. 售后域

```sql
CREATE TABLE `refund_order` (
  `id`                BIGINT       NOT NULL,
  `refund_no`         VARCHAR(32)  NOT NULL COMMENT '售后单号',
  `order_sub_no`      VARCHAR(32)  NOT NULL,
  `order_main_no`     VARCHAR(32)  NOT NULL,
  `user_id`           BIGINT       NOT NULL COMMENT '分片键',
  `shop_id`           BIGINT       NOT NULL,
  `refund_type`       TINYINT      NOT NULL COMMENT '1仅退款 2退货退款 3换货',
  `reason_type`       TINYINT      NOT NULL COMMENT '1质量问题 2不想要 3发错货 4少发 5假货 6其他',
  `reason_desc`       VARCHAR(255) DEFAULT NULL,
  `images`            JSON         DEFAULT NULL COMMENT '凭证图片',
  -- 金额
  `refund_amount`     BIGINT       NOT NULL COMMENT '商品退款金额（不含运费）',
  `refund_freight`    BIGINT       NOT NULL DEFAULT 0 COMMENT '退还的运费',
  `freight_bearer`    TINYINT      NOT NULL DEFAULT 2 COMMENT '退货运费承担方 1用户 2商家 3平台',
  `refund_points`     BIGINT       NOT NULL DEFAULT 0 COMMENT '返还积分',
  -- 状态机
  `status`            TINYINT      NOT NULL DEFAULT 10 COMMENT '见 08-aftersale §3.1',
  `source_status`     TINYINT      NOT NULL COMMENT '★ 申请时子单的状态，拒绝时恢复',
  -- 处理
  `merchant_remark`   VARCHAR(255) DEFAULT NULL,
  `platform_remark`   VARCHAR(255) DEFAULT NULL,
  `reject_reason`     VARCHAR(255) DEFAULT NULL,
  -- 质检
  `quality_result`    TINYINT      DEFAULT NULL COMMENT '1合格 2不合格 3部分合格',
  `quality_remark`    VARCHAR(255) DEFAULT NULL,
  `quality_images`    JSON         DEFAULT NULL,
  -- 物流
  `return_express`    VARCHAR(32)  DEFAULT NULL COMMENT '退货快递公司',
  `return_express_no` VARCHAR(64)  DEFAULT NULL COMMENT '退货单号',
  -- 时间节点
  `apply_time`        DATETIME(3)  NOT NULL,
  `merchant_handle_time` DATETIME(3) DEFAULT NULL COMMENT '商家审核时间',
  `return_time`       DATETIME(3)  DEFAULT NULL COMMENT '用户寄回时间',
  `receive_time`      DATETIME(3)  DEFAULT NULL COMMENT '商家收货时间',
  `quality_time`      DATETIME(3)  DEFAULT NULL COMMENT '质检时间',
  `refund_time`       DATETIME(3)  DEFAULT NULL COMMENT '退款完成时间',
  `close_time`        DATETIME(3)  DEFAULT NULL,
  `deadline`          DATETIME(3)  NOT NULL COMMENT '★ 当前环节的截止时间（驱动超时任务）',
  `created_at`        DATETIME(3)  NOT NULL,
  `updated_at`        DATETIME(3)  NOT NULL,
  `version`           INT          NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_refund_no` (`refund_no`),
  KEY `idx_user_status` (`user_id`, `status`, `created_at`),
  KEY `idx_shop_status` (`shop_id`, `status`, `created_at`),
  KEY `idx_main` (`order_main_no`),
  KEY `idx_deadline` (`status`, `deadline`) COMMENT '★ 超时扫描'
) ENGINE=InnoDB COMMENT='售后单';

CREATE TABLE `refund_item` (
  `id`             BIGINT      NOT NULL AUTO_INCREMENT,
  `refund_no`      VARCHAR(32) NOT NULL,
  `order_item_id`  BIGINT      NOT NULL,
  `sku_id`         BIGINT      NOT NULL,
  `refund_num`     INT         NOT NULL COMMENT '退货数量',
  `refund_amount`  BIGINT      NOT NULL COMMENT '该行退款金额',
  `refund_points`  BIGINT      NOT NULL DEFAULT 0,
  -- 快照
  `spu_title_snap` VARCHAR(120) NOT NULL,
  `sku_spec_snap`  VARCHAR(255) NOT NULL,
  `cover_image_snap` VARCHAR(255) NOT NULL,
  -- 库存恢复标记
  `stock_restored` TINYINT     NOT NULL DEFAULT 0 COMMENT '★ 库存是否已恢复（幂等标记）',
  `restore_time`   DATETIME(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_refund_item` (`refund_no`, `order_item_id`),
  KEY `idx_order_item` (`order_item_id`)
) ENGINE=InnoDB COMMENT='售后商品明细';
```

**`refund_order.deadline` 字段的价值**：把"当前环节该在什么时候完成"显式存下来，超时扫描只需要 `WHERE status = ? AND deadline < NOW()`，一个索引搞定所有超时场景，不用为每个环节写不同的时间字段判断。

```java
// 状态流转时更新 deadline
switch (newStatus) {
    case APPLYING        -> deadline = now().plusHours(48);   // 商家 48h 内审核
    case WAIT_RETURN     -> deadline = now().plusDays(7);     // 用户 7 天内寄回
    case WAIT_RECEIVE    -> deadline = now().plusDays(7);     // 商家 7 天内收货
    case QUALITY_CHECKING -> deadline = now().plusHours(48);  // 质检 48h
    default -> deadline = now().plusYears(10);                // 终态不超时
}
```

## 4. 本地消息表（最终一致的基础设施）

```sql
CREATE TABLE `local_message` (
  `id`           BIGINT      NOT NULL AUTO_INCREMENT,
  `msg_id`       VARCHAR(64) NOT NULL COMMENT '消息唯一 ID（雪花）',
  `topic`        VARCHAR(64) NOT NULL,
  `tag`          VARCHAR(64) DEFAULT NULL,
  `biz_key`      VARCHAR(64) NOT NULL COMMENT '★ 幂等键',
  `payload`      TEXT        NOT NULL,
  `status`       TINYINT     NOT NULL DEFAULT 0 COMMENT '0待发送 1已发送 2发送失败 3已消费',
  `retry_count`  INT         NOT NULL DEFAULT 0,
  `next_retry_at` DATETIME(3) NOT NULL COMMENT '★ 下次重试时间（指数退避）',
  `error_msg`    VARCHAR(512) DEFAULT NULL,
  `created_at`   DATETIME(3) NOT NULL,
  `updated_at`   DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_msg_id` (`msg_id`),
  KEY `idx_status_retry` (`status`, `next_retry_at`) COMMENT '★ 扫描待发送消息',
  KEY `idx_biz_key` (`biz_key`)
) ENGINE=InnoDB COMMENT='本地消息表（事务消息的替代）';
```

**工作方式**：

```
① 业务事务内：INSERT local_message (status=0)
   → 与业务数据同一个事务，保证"业务成功则消息一定存在"

② 独立的发送任务（每 1 秒扫一次）
   SELECT * FROM local_message
   WHERE status IN (0, 2) AND next_retry_at <= NOW()
   ORDER BY id LIMIT 200;

③ 发送到 MQ，成功则 status = 1

④ 消费端幂等消费（用 biz_key 去重），成功后：
   → 可选的回调：把 local_message.status 改为 3

⑤ 失败重试：retry_count++，next_retry_at = now + min(2^retry * 10s, 1h)
```

**指数退避的 SQL**：

```sql
UPDATE local_message
SET status = 2,
    retry_count = retry_count + 1,
    next_retry_at = DATE_ADD(NOW(3), INTERVAL LEAST(POW(2, retry_count) * 10, 3600) SECOND),
    error_msg = ?
WHERE id = ?;
```

## 5. 索引设计要点

### 5.1 订单表的核心索引

```sql
-- 用户订单列表（最常用）
KEY `idx_user_status_time` (`user_id`, `status`, `create_time`)
-- 查询："我的待付款订单，按时间倒序"
-- WHERE user_id = ? AND status = 10 ORDER BY create_time DESC
-- ★ 索引顺序必须是 (user_id, status, create_time)，range 条件放最后

-- 商家订单列表
KEY `idx_shop_status` (`shop_id`, `status`, `create_time`)

-- 超时扫描
KEY `idx_pay_deadline` (`status`, `pay_deadline`)
-- WHERE status = 10 AND pay_deadline < NOW()
-- ★ 这个查询的区分度很低（永远只有少量 status=10 的记录），
--   靠"status 在前"来快速定位，且这个索引是"活的"（数量小）
```

**为什么超时扫描索引要用 `(status, pay_deadline)` 而不是单独 `pay_deadline`**：

- 单独 `pay_deadline`：会扫到所有历史订单（包括已完成的），因为 90% 的订单 `pay_deadline` 都小于当前时间。
- `(status, pay_deadline)`：`status = 10` 的记录很少（只有最近 30 分钟内的未支付订单），索引扫描量极小。

**维护成本**：`status = 10` 的记录数量少，所以这个索引的写入性能开销也小。

### 5.2 避免索引失效的写法

```sql
-- ❌ 索引失效：status 上用了函数
WHERE DATE(create_time) = '2026-09-30'

-- ✅ 范围查询
WHERE create_time >= '2026-09-30 00:00:00'
  AND create_time <  '2026-10-01 00:00:00'

-- ❌ 索引失效：隐式类型转换（user_id 是 BIGINT，传了字符串）
WHERE user_id = '88001'

-- ❌ 索引失效：前导模糊
WHERE order_main_no LIKE '%1234'

-- ✅ 前缀匹配
WHERE order_main_no LIKE 'M20260930%'

-- ❌ 索引失效：OR 连接不同列
WHERE user_id = 1 OR shop_id = 2
```

### 5.3 联合索引的最左前缀

```sql
-- 索引：(user_id, status, create_time)

-- ✅ 命中
WHERE user_id = 1
WHERE user_id = 1 AND status = 10
WHERE user_id = 1 AND status = 10 AND create_time > '...'

-- ⚠️ 部分命中（只用到 user_id）
WHERE user_id = 1 AND create_time > '...'

-- ❌ 不命中
WHERE status = 10
WHERE create_time > '...'
```

## 6. 分库分表方案

### 6.1 分片键选择

| 表 | 分片键 | 理由 |
|---|---|---|
| `order_main` | `user_id` | 用户查自己的订单是最高频操作 |
| `order_sub` | `user_id` | 与母单同库，便于事务与 join |
| `order_item` | `order_sub_no`（或同库 user_id） | 订单项永远按子单查 |
| `payment` | `order_main_no`（或同库 user_id） | 按母单查支付单 |
| `refund_order` | `user_id` | 与订单同库 |
| `coupon_code` | `coupon_template_id` | 券模板维度的运营查询 |
| `stock_flow` | `sku_id` + 月分区 | 按 SKU 排查库存问题 |

**同库原则**：母单、子单、订单项、支付单、售后单必须**在同一个库**（都用 `user_id` 分片），这样才能用本地事务，避免跨库事务。

### 6.2 分片算法

```
库号 = user_id % 64
表号 = (user_id / 64) % 16
→ 64 库 × 16 表 = 1024 张表，每张表存约 1000 万订单（10 亿总量）

配置化（避免硬编码）：
  sharding:
    tables:
      order_main:
        actual-data-nodes: ds_${0..63}.order_main_${0..15}
        database-strategy:
          standard:
            sharding-column: user_id
            sharding-algorithm-name: db-inline
        table-strategy:
          standard:
            sharding-column: user_id
            sharding-algorithm-name: tbl-inline
```

### 6.3 跨分片查询：ES 异构索引

**问题**：商家按 `shop_id` 查订单，但分片键是 `user_id` → 需要扫 1024 张表。

**方案**：写订单时同步一份精简记录到 ES。

```java
@EventListener
public void onOrderStatusChanged(OrderStatusChangedEvent e) {
    // 异步写 ES（通过 MQ 解耦，不阻塞订单事务）
    mqTemplate.send("order-es-sync-topic", EsOrderDoc.from(e));
}
```

```java
// ES 消费者
@RocketMQMessageListener(topic = "order-es-sync-topic")
public void syncToEs(EsOrderDoc doc) {
    // 用 orderSubNo 作为 _id，天然幂等（upsert）
    esClient.index(i -> i.index("order").id(doc.getOrderSubNo()).document(doc));
    // ★ 乱序问题：携带 version（= order_sub.version），
    //   ES 的 external version 机制会拒绝旧版本
}
```

**ES 的乐观并发控制**：

```java
esClient.index(i -> i
    .index("order")
    .id(doc.getOrderSubNo())
    .version(doc.getVersion())            // ★ 外部版本号
    .versionType(VersionType.EXTERNAL_GTE) // 只有 >= 才接受
    .document(doc));
```

**最终一致性保障**：ES 可能丢失同步（MQ 消息丢失）。补偿：每日全量/增量对照（按 `updated_at` 拉取最近变更的订单，补齐 ES）。

## 7. 归档策略

| 表 | 保留期 | 归档方式 |
|---|---|---|
| `order_main` / `order_sub` / `order_item` | 3 年在线 | 3 年前的数据归档到冷库（TiDB / ClickHouse / OSS 文件） |
| `stock_flow` | 6 个月在线 | 月分区，超期 `ALTER TABLE ... DROP PARTITION` |
| `points_flow` | 2 年在线 | 同上 |
| `pay_notify_log` | 3 个月在线 | 归档（回调原文仅用于排查，超期无价值） |
| `reconcile_diff` | 3 年 | 财务凭证，需长期保留 |
| `local_message` | 7 天 | 成功后立即删除（定时任务） |
| `cart_item` | 永久（用户自己删） | - |

**归档的实现**：

```sql
-- 分区表：直接 DROP PARTITION（秒级完成，不产生 binlog 风暴）
ALTER TABLE stock_flow DROP PARTITION p202603;

-- 非分区表：分批 DELETE（避免大事务）
DELETE FROM local_message
WHERE status = 3 AND updated_at < NOW() - INTERVAL 7 DAY
LIMIT 2000;
-- 循环执行直到 affected rows = 0
```

## 8. 数据字典：关键枚举

```java
// 订单状态
public enum SubOrderStatus {
    WAIT_PAY(10), WAIT_DELIVER(20), WAIT_RECEIVE(30),
    FINISHED(40), CLOSED(50), REFUNDING(60), REFUNDED(70);
}

// 退款状态
public enum RefundStatus {
    APPLYING(10), MERCHANT_REJECTED(11), WAIT_REFUND(20),
    WAIT_RETURN(30), WAIT_RECEIVE(40), QUALITY_CHECKING(50),
    QUALITY_FAILED(51), REFUNDING(60), SUCCESS(70),
    CLOSED(80), USER_REVOKED(81), PLATFORM_INTERVENING(90);
}

// 支付状态
public enum PayStatus {
    WAIT_PAY(0), PAYING(1), SUCCESS(2), FAILED(3), CLOSED(4), REFUNDED(5);
}

// 券状态
public enum CouponStatus {
    UNUSED(1), LOCKED(2), USED(3), EXPIRED(4), VOIDED(5);
}

// 库存变更类型
public enum StockChangeType {
    INIT(7), LOCK(1), CONFIRM(2), RELEASE(3), DELIVER(4), RETURN_IN(5), MANUAL(6);
}

// 积分变更类型
public enum PointsChangeType {
    ORDER_USE(1), ORDER_FREEZE(2), PAY_CONFIRM(3), REFUND(4),
    SHOPPING_AWARD(5), REVIEW_AWARD(6), EXPIRE(7), SIGN_IN(8), MANUAL(9);
}

// 评价状态
public enum ReviewStatus {
    PENDING_AUDIT(0), PUBLISHED(1), BLOCKED(2), REJECTED(3);
}
```

## 9. 数据库配置基线

```ini
# InnoDB
innodb_buffer_pool_size = 物理内存的 70%
innodb_flush_log_at_trx_commit = 1        # 资金表必须为 1
innodb_io_capacity = 2000
innodb_flush_neighbors = 0                # SSD 环境

# 连接
max_connections = 2000
wait_timeout = 600
interactive_timeout = 600

# Binlog
binlog_format = ROW
binlog_row_image = MINIMAL                # 减少 binlog 体积
expire_logs_days = 7

# 事务
transaction_isolation = READ-COMMITTED    # 用 RC 而非 RR，减少间隙锁
                                          # ★ 资金表依赖显式 SQL 条件（条件更新），不依赖 RR

# 慢查询
slow_query_log = 1
long_query_time = 0.5
```

**为什么用 `READ-COMMITTED`**：

| 隔离级别 | 间隙锁 | 死锁概率 | 适用 |
|---|---|---|---|
| REPEATABLE-READ（MySQL 默认） | 有 | 高 | 需要可重复读的业务 |
| **READ-COMMITTED** | 基本无 | 低 | **高并发交易系统（本方案）** |

本方案的一致性**不依赖 RR**，而是依赖**显式的条件更新（`WHERE available >= n`）和唯一索引**。用 RC 能大幅降低死锁概率。

**注意**：使用 RC 时，binlog 必须是 `ROW` 格式（否则主从会不一致）。已在配置中指定。

## 10. 对账 SQL 清单（每日跑）

```sql
-- ① 库存恒等式
SELECT sku_id, warehouse_id, total, available, locked, frozen
FROM sku_stock
WHERE total <> available + locked + frozen
   OR available < 0 OR locked < 0 OR frozen < 0;
-- 必须为空

-- ② 券不超发
SELECT id, name, issued_count, total_count
FROM coupon_template WHERE issued_count > total_count;
-- 必须为空

-- ③ 订单金额守恒
SELECT order_main_no,
       payable_amount,
       total_amount - item_discount - shop_discount - platform_discount
                     - point_deduction + freight_amount AS calc
FROM order_main
WHERE payable_amount <> total_amount - item_discount - shop_discount
                       - platform_discount - point_deduction + freight_amount;
-- 必须为空

-- ④ 母子单金额守恒
SELECT m.order_main_no, m.payable_amount, SUM(s.payable_amount) AS sub_sum
FROM order_main m JOIN order_sub s ON m.order_main_no = s.order_main_no
GROUP BY m.order_main_no, m.payable_amount
HAVING m.payable_amount <> sub_sum;
-- 必须为空

-- ⑤ 订单项分摊守恒
SELECT oi.order_sub_no, SUM(oi.discount_amount) AS item_disc,
       s.item_discount + s.shop_discount + s.platform_discount + s.point_deduction AS sub_disc
FROM order_item oi JOIN order_sub s ON oi.order_sub_no = s.order_sub_no
GROUP BY oi.order_sub_no
HAVING item_disc <> sub_disc;
-- 必须为空

-- ⑥ 退款不超付
SELECT order_sub_no, payable_amount, refunded_amount
FROM order_sub WHERE refunded_amount > payable_amount;
-- 必须为空

-- ⑦ 积分账实相符
SELECT up.user_id, up.balance,
       COALESCE(SUM(pf.points), 0) AS flow_sum
FROM user_points up
LEFT JOIN points_flow pf ON up.user_id = pf.user_id
GROUP BY up.user_id, up.balance
HAVING up.balance <> flow_sum;
-- 必须为空（抽样跑，全量太慢）

-- ⑧ 支付单与订单状态一致
SELECT p.pay_no, p.status AS pay_status, m.status AS order_status
FROM payment p JOIN order_main m ON p.order_main_no = m.order_main_no
WHERE (p.status IN (0, 1) AND m.status = 50)   -- 订单已关闭但支付单仍待支付/支付中
   OR (p.status = 2 AND m.status = 10);        -- 支付成功但订单仍待付款
-- 必须为空（或只有 [09-payment §7] 的晚付场景）
```
