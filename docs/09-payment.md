# 09 支付：异步回调与对账补偿

## 1. 支付链路全景

```
用户点击"去支付"
   │
   ▼
① 创建支付单（payment）  ─── 幂等键：order_main_no
   │
   ▼
② 调用渠道统一下单（微信/支付宝）
   │  返回：prepay_id / 支付链接 / 二维码
   ▼
③ 返回前端唤起支付
   │
   ▼
④ 用户在渠道完成支付
   │
   ├──► ⑤ 渠道异步回调我方服务器（主路径，秒级）
   │
   └──► ⑥ 前端同步返回后主动查询（辅助，防回调丢失）
              │
              ▼
        ⑦ 定时对账任务（兜底，分钟级/日级）
```

**三条路径都必须能推动订单状态**，且都幂等。这是"支付掉单"问题的最小代价解法。

## 2. 支付单模型

```sql
CREATE TABLE `payment` (
  `id`                BIGINT       NOT NULL,
  `pay_no`            VARCHAR(32)  NOT NULL COMMENT '支付单号（我们生成）',
  `order_main_no`     VARCHAR(32)  NOT NULL COMMENT '母单号',
  `user_id`           BIGINT       NOT NULL,
  `channel`           TINYINT      NOT NULL COMMENT '1微信 2支付宝 3银联 4余额 5苹果内购',
  `channel_app_id`    VARCHAR(64)  DEFAULT NULL,
  `channel_merchant_id` VARCHAR(64) DEFAULT NULL,
  `amount`            BIGINT       NOT NULL COMMENT '应付金额（分）',
  `paid_amount`       BIGINT       NOT NULL DEFAULT 0 COMMENT '实付金额（分）',
  `currency`          VARCHAR(8)   NOT NULL DEFAULT 'CNY',
  `status`            TINYINT      NOT NULL DEFAULT 0 COMMENT '0待支付 1支付中 2支付成功 3支付失败 4已关闭 5已退款',
  -- 渠道返回
  `out_trade_no`      VARCHAR(64)  DEFAULT NULL COMMENT '渠道交易号（微信transaction_id等）',
  `prepay_id`         VARCHAR(128) DEFAULT NULL,
  `channel_response`  JSON         DEFAULT NULL COMMENT '渠道返回原文（审计用）',
  -- 回调
  `notify_time`       DATETIME(3)  DEFAULT NULL COMMENT '收到回调的时间',
  `notify_count`      INT          NOT NULL DEFAULT 0 COMMENT '回调次数（含重复回调）',
  -- 时间
  `create_time`       DATETIME(3)  NOT NULL,
  `expire_time`       DATETIME(3)  NOT NULL COMMENT '支付单过期（= 订单支付截止）',
  `pay_time`          DATETIME(3)  DEFAULT NULL,
  `close_time`        DATETIME(3)  DEFAULT NULL,
  `updated_at`        DATETIME(3)  NOT NULL,
  `version`           INT          NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_pay_no` (`pay_no`),
  UNIQUE KEY `uk_main_order` (`order_main_no`) COMMENT '★ 一个母单只能有一个有效支付单',
  UNIQUE KEY `uk_channel_trade` (`channel`, `out_trade_no`) COMMENT '★ 防同一渠道交易号重复入账',
  KEY `idx_status_time` (`status`, `create_time`) COMMENT '对账扫描',
  KEY `idx_pay_time` (`pay_time`) COMMENT '日对账'
) ENGINE=InnoDB COMMENT='支付单';
```

**两个唯一索引是关键防线**：

- `uk_main_order`：防止同一订单创建多个支付单（用户连点"去支付"）。
- `uk_channel_trade`：防止同一个渠道交易号入账两次（回调重复、或对账和回调同时处理）。

**注意**：如果允许"待支付→关闭→重新支付"（换渠道），`uk_main_order` 就要改成 `uk_main_order_status` 部分索引（MySQL 不支持部分唯一索引，改用 `biz_key = mainOrderNo + '_' + attempt` 方案）。**简化方案：不允许换渠道重付，关闭后需重新下单**。

## 3. 渠道对接抽象

```java
public interface PaymentChannel {
    /** 渠道编码 */
    ChannelType getType();

    /** 统一下单 */
    PrepayResult prepay(PrepayRequest req);

    /** 主动查单（对账/补偿用） */
    QueryResult query(String payNo, String outTradeNo);

    /** 关闭渠道订单 */
    void close(String payNo, String outTradeNo);

    /** 申请退款 */
    RefundResult refund(RefundRequest req);

    /** 查询退款 */
    RefundQueryResult queryRefund(String refundNo);

    /** 解析并验签回调 */
    NotifyResult parseNotify(HttpServletRequest request) throws SignatureException;

    /** 回调应答报文（必须原样返回给渠道） */
    String notifyAck(boolean success);
}
```

**统一金额单位**：所有渠道统一用**分**。微信、支付宝都用分，无需转换。**唯一例外是某些渠道用元**——在实现类内部转换，接口层不暴露。

## 4. 异步回调的处理（核心）

### 4.1 回调接口

```java
@RestController
@RequestMapping("/pay/notify")
public class PayNotifyController {

    @PostMapping("/{channel}")
    public String notify(@PathVariable String channel, HttpServletRequest request) {
        PaymentChannel ch = channelFactory.get(channel);
        NotifyResult result;
        try {
            // ① 验签（必须！否则可被伪造回调刷单）
            result = ch.parseNotify(request);
        } catch (SignatureException e) {
            log.warn("支付回调验签失败: channel={}, ip={}", channel, getIp(request));
            return ch.notifyAck(false);
        }

        try {
            // ② 幂等 + 业务处理（异步化：先落库，立刻应答）
            boolean processed = payService.handleNotify(ch.getType(), result);
            return ch.notifyAck(processed);
        } catch (Exception e) {
            log.error("支付回调处理异常: {}", result, e);
            // ★ 返回失败，让渠道重试
            return ch.notifyAck(false);
        }
    }
}
```

### 4.2 回调处理的三个铁律

**铁律 1：验签**

无验签 = 任何人 curl 一下就能把订单标记为已支付。**必须用渠道提供的 SDK 验签**，不要自己实现。

**铁律 2：立刻应答，业务异步**

渠道回调有超时（微信 5s、支付宝 5s）。业务处理（改订单、扣库存、算积分、发通知）可能超过 5s。做法：

```java
public boolean handleNotify(ChannelType channel, NotifyResult result) {
    // ① 快速落库（回调原文），一次 INSERT，10ms 内完成
    //    用 uk_channel_trade 唯一索引去重
    PayNotifyLog log = new PayNotifyLog();
    log.setChannel(channel);
    log.setOutTradeNo(result.getOutTradeNo());
    log.setPayNo(result.getPayNo());
    log.setRawBody(result.getRawBody());
    log.setStatus(0);   // 待处理

    try {
        notifyLogMapper.insert(log);
    } catch (DuplicateKeyException e) {
        // ★ 重复回调，直接返回成功（渠道不再重试）
        log.info("重复回调，忽略: outTradeNo={}", result.getOutTradeNo());
        return true;
    }

    // ② 发 MQ，异步处理
    mqTemplate.send("pay-notify-topic", new PayNotifyMsg(log.getId()));
    return true;
}
```

**铁律 3：处理必须幂等**

即使有落库去重，MQ 也可能重复投递。核心去重：

```java
@RocketMQMessageListener(topic = "pay-notify-topic")
public void onMessage(PayNotifyMsg msg) {
    PayNotifyLog log = notifyLogMapper.selectById(msg.getLogId());

    // ① 状态守卫：已处理过直接返回
    if (log.getStatus() == 1) return;

    // ② CAS 抢占处理权
    if (notifyLogMapper.casToProcessing(log.getId(), 0, 1) == 0) {
        return;   // 另一个消费者在处理
    }

    try {
        doHandle(log);
        notifyLogMapper.markProcessed(log.getId(), 2);
    } catch (Exception e) {
        notifyLogMapper.markFailed(log.getId(), 3, e.getMessage());
        throw e;   // 让 MQ 重试
    }
}
```

### 4.3 回调的业务处理

```java
@Transactional
public void doHandle(PayNotifyLog notifyLog) {
    Payment pay = payMapper.selectByPayNo(notifyLog.getPayNo());
    if (pay == null) throw new BusinessException(PAY_NOT_FOUND);

    // ① 金额校验（★ 最重要的校验！防止渠道金额被篡改）
    if (!pay.getAmount().equals(notifyLog.getPaidAmount())) {
        log.error("★ 支付金额不一致! payNo={}, 期望={}, 实际={}",
            pay.getPayNo(), pay.getAmount(), notifyLog.getPaidAmount());
        throw new AmountMismatchException(pay.getPayNo());
    }

    // ② 状态机守卫：只有待支付/支付中才能转成功
    if (pay.getStatus() == PayStatus.SUCCESS) {
        log.info("支付单已是成功状态，幂等返回: {}", pay.getPayNo());
        return;   // ★ 幂等核心
    }
    if (pay.getStatus() != PayStatus.WAIT_PAY && pay.getStatus() != PayStatus.PAYING) {
        log.warn("支付单状态异常，忽略回调: payNo={}, status={}", pay.getPayNo(), pay.getStatus());
        return;
    }

    // ③ 更新支付单（CAS）
    int rows = payMapper.markSuccess(pay.getPayNo(),
        notifyLog.getOutTradeNo(), notifyLog.getPaidAmount(), notifyLog.getNotifyTime());
    if (rows == 0) return;   // 并发下已被处理

    // ④ 更新订单（CAS：只有待付款才能转待发货）
    int orderRows = mainMapper.markPaid(pay.getOrderMainNo(),
        notifyLog.getPaidAmount(), notifyLog.getNotifyTime());
    if (orderRows == 0) {
        // 订单状态不是待付款 → 可能是超时关单后又收到支付成功
        // ★ 这是"多付"场景，必须处理！
        handleLatePayment(pay, notifyLog);
        return;
    }

    // ⑤ 子单状态推进
    List<OrderSub> subs = subMapper.listByMain(pay.getOrderMainNo());
    for (OrderSub sub : subs) {
        subService.transit(sub.getOrderSubNo(), OrderSubEvent.PAY_SUCCESS, ctx);
    }

    // ⑥ 库存：预占转实扣
    inventoryClient.confirm(pay.getOrderMainNo(), buildItems(subs));

    // ⑦ 积分：冻结转实扣
    pointsClient.confirm(pay.getOrderMainNo(), main.getPointUsed());

    // ⑧ 优惠券：锁定转已使用
    couponClient.use(pay.getOrderMainNo());

    // ⑨ 发领域事件（异步：通知、结算、ES、风控）
    eventPublisher.publish(new OrderPaidEvent(pay.getOrderMainNo(), pay.getPaidAmount()));
}
```

**第 ④ 步的 `rows == 0` 是最容易漏掉的边界**（见 §7）。

## 5. 主动查单补偿

**为什么需要**：回调可能永远收不到（网络分区、渠道故障、我们服务重启）。

**触发方式**：

```java
// 方式 1：前端同步返回时触发（用户视角最快的补偿）
@PostMapping("/pay/query")
public PayStatusVO query(@RequestParam String payNo) {
    Payment pay = payMapper.selectByPayNo(payNo);
    if (pay.getStatus() == PayStatus.SUCCESS) return toVO(pay);

    // 距创建超过 5 秒才真正查渠道（避免用户刚点就查，渠道还没结果）
    if (Duration.between(pay.getCreateTime(), now()).getSeconds() >= 5) {
        paymentChannel.query(payNo, null);   // 结果由统一处理器落库，幂等
        pay = payMapper.selectByPayNo(payNo);
    }
    return toVO(pay);
}
```

```java
// 方式 2：定时扫描（兜底）
@Scheduled(fixedDelay = 30000)
public void scanPayingOrders() {
    // 找出"支付中"状态超过 30 秒的支付单
    List<Payment> list = payMapper.selectStale(PayStatus.PAYING,
        now().minusSeconds(30), 200);

    for (Payment pay : list) {
        try {
            QueryResult r = channelFactory.get(pay.getChannel())
                .query(pay.getPayNo(), pay.getOutTradeNo());

            if (r.isPaid()) {
                // ★ 走与回调完全相同的处理逻辑
                payService.handleChannelResult(pay.getPayNo(), r);
            } else if (r.isClosed() || r.isFailed()) {
                payService.markFailed(pay.getPayNo(), r.getFailReason());
            }
            // 未支付 → 什么都不做，等下次扫描或超时关单
        } catch (Exception e) {
            log.error("查单失败: {}", pay.getPayNo(), e);
        }
    }
}
```

**关键设计：查单结果和回调结果走同一个处理方法**。这样无论哪条路径先到，结果一致，而且天然幂等。

```java
// 统一入口
public boolean handleChannelResult(String payNo, ChannelResult result) {
    // 构造一个"伪回调"，复用 doHandle 的全部逻辑
    PayNotifyLog pseudo = PayNotifyLog.fromChannelResult(result);
    pseudo.setSource(Source.QUERY);   // 标记来源，便于排查
    doHandle(pseudo);
    return true;
}
```

## 6. 对账补偿（最终的兜底）

### 6.1 三层对账

| 层级 | 频率 | 内容 | 动作 |
|---|---|---|---|
| **实时对账** | 30 秒 | 本地"支付中"的支付单，主动查渠道 | 修正状态 |
| **准实时对账** | 5 分钟 | 近 1 小时的支付单，拉渠道流水比对 | 补单/冲正 |
| **日对账** | 每日 02:00 | 全量拉取前一日渠道账单，与本地逐笔比对 | 生成差异单，人工/自动处理 |

### 6.2 日对账的实现

```java
@Scheduled(cron = "0 0 2 * * ?")
public void dailyReconcile() {
    LocalDate bizDate = LocalDate.now().minusDays(1);

    for (ChannelType channel : ChannelType.values()) {
        // ① 拉取渠道账单文件（下载对账文件 / 调对账接口）
        List<ChannelBill> bills = channel.getBill(bizDate);

        // ② 拉取本地支付单（该日期成功支付的）
        List<Payment> locals = payMapper.selectByPayDate(channel, bizDate);

        // ③ 双向比对
        Map<String, ChannelBill> billMap = bills.stream()
            .collect(toMap(ChannelBill::getOutTradeNo, identity()));
        Map<String, Payment> localMap = locals.stream()
            .collect(toMap(Payment::getOutTradeNo, identity()));

        List<DiffRecord> diffs = new ArrayList<>();

        // 3.1 本地有、渠道无 → "本地多单"
        for (Payment p : locals) {
            if (!billMap.containsKey(p.getOutTradeNo())) {
                diffs.add(DiffRecord.localOnly(p));
            }
        }
        // 3.2 渠道有、本地无 → "本地少单"（掉单，最严重）
        for (ChannelBill b : bills) {
            if (!localMap.containsKey(b.getOutTradeNo())) {
                diffs.add(DiffRecord.channelOnly(b));
            }
        }
        // 3.3 两边都有但金额/状态不一致 → "金额不符"
        for (Payment p : locals) {
            ChannelBill b = billMap.get(p.getOutTradeNo());
            if (b != null && !p.getPaidAmount().equals(b.getAmount())) {
                diffs.add(DiffRecord.amountMismatch(p, b));
            }
        }

        // ④ 处理差异
        ReconcileResult result = reconcileService.process(diffs);
        log.info("日对账完成: channel={}, date={}, 差异={}", channel, bizDate, diffs.size());
    }
}
```

### 6.3 差异类型的处理策略

| 差异类型 | 含义 | 处理 |
|---|---|---|
| **本地有，渠道无** | 我方记了成功，渠道说没这笔。可能是伪造回调（但已验签，概率极低）或渠道对账文件不全 | 不自动冲正，**挂起人工核查**。同时标记用户风险 |
| **渠道有，本地无**（掉单） | 用户付了钱，我方不知道 | ★ **自动补单**。构造 `pseudoNotify` 走 `handleChannelResult` |
| **金额不符** | 用户实付 ≠ 订单应付 | 挂起，人工处理；同时通知用户 |
| **本地失败，渠道成功** | 我方标了失败但渠道成功了 | 自动修正为成功 + 补单 |
| **本地成功，渠道退款** | 渠道侧有退款我们不知道 | 触发本地退款流程 |

**自动补单的实现**：

```java
public void autoRepair(ChannelBill bill) {
    // ① 通过商户订单号找到支付单
    Payment pay = payMapper.selectByPayNo(bill.getPayNo());  // 我们传给渠道的就是 payNo
    if (pay == null) {
        log.error("★ 对账发现未知支付单: outTradeNo={}", bill.getOutTradeNo());
        return;
    }

    // ② 构造伪回调，走正常处理链路（复用全部校验与幂等）
    NotifyResult pseudo = NotifyResult.builder()
        .payNo(pay.getPayNo())
        .outTradeNo(bill.getOutTradeNo())
        .paidAmount(bill.getAmount())
        .notifyTime(bill.getPayTime())
        .rawBody(bill.getRawJson())
        .source(Source.RECONCILE)
        .build();

    handleChannelResult(pay.getPayNo(), pseudo);

    // ③ 记录修复日志（用于统计掉单率）
    reconcileLogMapper.insert(new ReconcileLog(bill, "AUTO_REPAIR"));
}
```

### 6.4 对账的幂等

对账任务**必须可重跑**（同一天跑两次结果一致）：

```sql
-- 每次对账前，先删除该日期的旧差异记录（或按 (channel, biz_date, out_trade_no) 唯一索引 upsert）
DELETE FROM reconcile_diff
WHERE channel = ? AND biz_date = ? AND status = 0;   -- 只删未处理的
```

**唯一索引**：

```sql
CREATE TABLE `reconcile_diff` (
  `id`            BIGINT      NOT NULL AUTO_INCREMENT,
  `biz_date`      DATE        NOT NULL,
  `channel`       TINYINT     NOT NULL,
  `out_trade_no`  VARCHAR(64) DEFAULT NULL,
  `pay_no`        VARCHAR(32) DEFAULT NULL,
  `diff_type`     TINYINT     NOT NULL COMMENT '1本地多单 2本地少单 3金额不符 4本地失败渠道成功 5渠道退款',
  `local_amount`  BIGINT      DEFAULT NULL,
  `channel_amount` BIGINT     DEFAULT NULL,
  `status`        TINYINT     NOT NULL DEFAULT 0 COMMENT '0待处理 1已自动修复 2人工已处理 3已忽略',
  `handle_remark` VARCHAR(255) DEFAULT NULL,
  `created_at`    DATETIME(3) NOT NULL,
  `handled_at`    DATETIME(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_diff` (`biz_date`, `channel`, `diff_type`, `out_trade_no`) COMMENT '★ 重跑幂等',
  KEY `idx_status` (`status`, `biz_date`)
) ENGINE=InnoDB COMMENT='对账差异记录';
```

## 7. 最危险的场景：超时关闭后收到支付成功

**时序**：

```
T+0    用户下单，支付截止 T+30min
T+29m  用户发起支付，渠道开始处理
T+30m  我方超时关单（延迟消息触发）
T+30m30s 用户实际支付成功
T+31m  渠道回调到达
```

**此时订单已关闭，库存已释放，券已退回。**

处理方案（按优先级）：

| 方案 | 做法 | 评价 |
|---|---|---|
| **A. 自动退款** | 识别到"已关单但支付成功"，立即发起原路退款 | ✅ **推荐**。用户体验：钱退回来了，损失最小 |
| B. 自动重开订单 | 重新预占库存，若成功则恢复订单 | ❌ 库存可能已卖给别人，且用户可能不要了 |
| C. 挂起人工 | 生成差异单等待客服 | ❌ 用户体验差，客服成本高 |

**方案 A 的实现**：

```java
private void handleLatePayment(Payment pay, PayNotifyLog notifyLog) {
    OrderMain main = mainMapper.selectByNo(pay.getOrderMainNo());

    if (main.getStatus() == OrderMainStatus.CLOSED) {
        log.warn("★ 关单后收到支付成功，发起自动退款: payNo={}, mainNo={}",
            pay.getPayNo(), pay.getOrderMainNo());

        // ① 支付单标记成功（钱确实收了）
        payMapper.markSuccess(pay.getPayNo(), ...);

        // ② 标记订单为"已关单已付款"，禁止任何发货动作
        mainMapper.markClosedPaid(pay.getOrderMainNo());

        // ③ 自动发起全额退款（走退款服务，幂等键 REFUND_LATE:{payNo}）
        refundService.createAutoRefund(
            pay.getOrderMainNo(),
            pay.getPaidAmount(),
            "订单超时关闭后支付成功，自动退款");

        // ④ 通知用户（重要！不要让用户蒙在鼓里）
        notifyService.sendLatePayRefund(main.getUserId(), main.getOrderMainNo());

        // ⑤ 告警（这是异常，要监控频率）
        alertService.warn("LATE_PAYMENT", pay.getPayNo());
    } else if (main.getStatus() == OrderMainStatus.PAID) {
        // 已被其他路径（如并发回调）处理，幂等忽略
        log.info("订单已支付，忽略重复回调: {}", pay.getPayNo());
    }
}
```

**降低发生率的措施**：

1. **关单前二次确认**：超时关单前，先主动查一次渠道。如果渠道说"支付中"，则**延迟 2 分钟再关**。
2. **支付截止时间留缓冲**：渠道侧的订单过期时间设得比我们短（我们 30min，渠道 25min）。这样渠道先关，不会出现我们关了、渠道还能付的情况。
3. **关单宽限期**：`pay_deadline` 到了之后，等 2 分钟再真正关单。

```java
// 关单前的二次确认
public void closeIfUnpaid(String mainOrderNo) {
    OrderMain main = mainMapper.selectByNo(mainOrderNo);
    if (main.getStatus() != WAIT_PAY) return;

    // ★ 二次查单
    Payment pay = payMapper.selectByMain(mainOrderNo);
    if (pay != null && pay.getStatus() == PayStatus.PAYING) {
        QueryResult r = channel.query(pay.getPayNo(), pay.getOutTradeNo());
        if (r.isPaid()) {
            handleChannelResult(pay.getPayNo(), r);   // 支付成功，不关单
            return;
        }
        if (r.isPaying()) {
            // 渠道还在处理，延后 2 分钟再关
            scheduleRetryClose(mainOrderNo, Duration.ofMinutes(2));
            return;
        }
    }

    // 真正的关单
    doClose(mainOrderNo);
}
```

## 8. 退款（资金侧）

**退款单与售后单分离**：

- `refund_order`（售后单）：业务视角，用户/商家/平台的处理流程
- `payment_refund`（资金退款单）：技术视角，一次渠道退款调用

**一个售后单可能对应多次资金退款**（如分次退款），但通常 1:1。

```sql
CREATE TABLE `payment_refund` (
  `id`             BIGINT      NOT NULL,
  `refund_no`      VARCHAR(32) NOT NULL COMMENT '资金退款单号',
  `refund_biz_no`  VARCHAR(32) NOT NULL COMMENT '售后单号（业务）',
  `pay_no`         VARCHAR(32) NOT NULL COMMENT '原支付单号',
  `order_main_no`  VARCHAR(32) NOT NULL,
  `user_id`        BIGINT      NOT NULL,
  `amount`         BIGINT      NOT NULL COMMENT '退款金额（分）',
  `channel`        TINYINT     NOT NULL,
  `status`         TINYINT     NOT NULL DEFAULT 0 COMMENT '0待退款 1退款中 2退款成功 3退款失败 4已关闭',
  `channel_refund_no` VARCHAR(64) DEFAULT NULL COMMENT '渠道退款单号',
  `fail_reason`    VARCHAR(255) DEFAULT NULL,
  `retry_count`    INT         NOT NULL DEFAULT 0,
  `create_time`    DATETIME(3) NOT NULL,
  `success_time`   DATETIME(3) DEFAULT NULL,
  `updated_at`     DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_refund_no` (`refund_no`),
  UNIQUE KEY `uk_biz_no` (`refund_biz_no`) COMMENT '★ 一个售后单只能有一次资金退款',
  KEY `idx_status_retry` (`status`, `retry_count`, `create_time`) COMMENT '重试扫描',
  KEY `idx_pay_no` (`pay_no`)
) ENGINE=InnoDB COMMENT='资金退款单';
```

**退款的重试机制**：

```java
@Scheduled(fixedDelay = 60000)
public void retryRefunds() {
    // 退款的失败重试：最多 10 次，间隔指数退避
    List<PaymentRefund> list = refundMapper.selectRetryable(
        PaymentRefundStatus.FAILED, 10, 200);

    for (PaymentRefund r : list) {
        // 退避：第 n 次重试间隔 = min(2^n 分钟, 6 小时)
        if (now().isBefore(r.getUpdatedAt().plusMinutes(
                Math.min(1L << r.getRetryCount(), 360)))) {
            continue;
        }
        try {
            refundService.execute(r.getRefundNo());
        } catch (Exception e) {
            log.error("退款重试失败: {}", r.getRefundNo(), e);
            refundMapper.incrRetry(r.getRefundNo());
        }
    }
}
```

**退款也可能掉单**：渠道退款异步返回，我们需要等回调或主动查询。同样用"回调 + 查单 + 对账"三件套。

## 9. 幂等的完整清单（支付链路）

| 环节 | 幂等键 | 实现 |
|---|---|---|
| 创建支付单 | `order_main_no` | 唯一索引 `uk_main_order` |
| 渠道统一下单 | `pay_no` | 渠道侧用商户订单号幂等；本地先查再建 |
| 收到回调 | `(channel, out_trade_no)` | 唯一索引 `uk_channel_trade` + 落库去重 |
| 处理回调 | `notify_log_id` | 状态 CAS |
| 更新支付单 | `pay_no + status` | CAS |
| 更新订单 | `order_main_no + status` | CAS |
| 库存实扣 | `PAY:{mainOrderNo}` | `stock_flow.uk_biz_key` |
| 积分实扣 | `ORDER_USE:{mainOrderNo}` | `points_flow.uk_biz_key` |
| 用券 | `USE:{mainOrderNo}:{codeId}` | coupon flow 唯一索引 |
| 发起退款 | `refund_biz_no` | `uk_biz_no` |
| 渠道退款 | `refund_no` | 渠道侧幂等 + 本地 CAS |

**资金链路上每一环都有幂等键**，这不是过度设计——支付回调天然会重复（渠道重试机制就是重发），没有幂等必然出事故。

## 10. 安全清单

| 项 | 要求 |
|---|---|
| 回调验签 | **必须**。用渠道 SDK，不自己实现 |
| 金额校验 | 回调金额必须与支付单金额一致，不一致拒绝 |
| 订单归属校验 | 回调的商户订单号必须是我们的格式且存在 |
| 幂等重复回调 | 返回成功（让渠道停止重试），但不再处理业务 |
| 回调频率限制 | 同一 `out_trade_no` 每分钟最多 20 次，超出直接拒绝 |
| 密钥管理 | 渠道密钥放配置中心/KMS，不入代码库，定期轮换 |
| 日志脱敏 | 日志不打印完整密钥、用户身份证、完整银行卡号 |
| 内网限制 | 回调接口只允许渠道 IP 段访问（在网关配置 IP 白名单） |
| 敏感操作二次校验 | 大额退款（> 5000 元）需人工审核 |

## 11. 监控指标

| 指标 | 告警阈值 |
|---|---|
| 支付成功率 | < 95% 告警 |
| 回调处理失败率 | > 1% 告警 |
| 对账差异数（掉单） | > 0 立即告警（每条都查） |
| 对账差异数（金额不符） | > 0 立即告警 |
| 晚付退款笔数 | > 5 笔/小时告警 |
| 退款失败重试次数 | 单笔 > 5 次告警 |
| 支付耗时 P99 | > 3s 告警 |
| 回调到订单更新延迟 | P99 > 5s 告警 |
