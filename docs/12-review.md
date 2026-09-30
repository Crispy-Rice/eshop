# 12 评论系统

## 1. 需求：购买后才能评论，且只能评论一次

| 约束 | 实现手段 |
|---|---|
| **只能购买后评论** | 订单项状态校验：子单已完成/已签收，且该订单项属于当前用户 |
| **只能评论一次** | 唯一索引 `uk_user_sku_item(user_id, sku_id, order_item_id)` |

## 2. 评论资格的判定

### 2.1 表结构

```sql
CREATE TABLE `review` (
  `id`              BIGINT       NOT NULL,
  `user_id`         BIGINT       NOT NULL,
  `spu_id`          BIGINT       NOT NULL,
  `sku_id`          BIGINT       NOT NULL,
  -- ★ 来源订单（这两列是"购后评论"的证据）
  `order_item_id`   BIGINT       NOT NULL COMMENT '订单项 ID',
  `order_sub_no`    VARCHAR(32)  NOT NULL,
  `order_main_no`   VARCHAR(32)  NOT NULL,
  -- 规格快照（用户当时买的规格）
  `sku_spec_snap`   VARCHAR(255) NOT NULL COMMENT '如"暗夜黑;256G"',
  -- 内容
  `score`           TINYINT      NOT NULL COMMENT '1-5 星',
  `content`         VARCHAR(2000) DEFAULT NULL,
  `images`          JSON         DEFAULT NULL COMMENT '图片 URL 数组，最多 9 张',
  `video_url`       VARCHAR(255) DEFAULT NULL COMMENT '最多 1 个视频',
  -- 维度评分（可选，按类目配置）
  `dimension_scores` JSON        DEFAULT NULL COMMENT '{"描述相符":5,"物流服务":4,"服务态度":5}',
  -- 匿名
  `anonymous`       TINYINT      NOT NULL DEFAULT 0,
  -- 状态
  `status`          TINYINT      NOT NULL DEFAULT 0 COMMENT '0待审核 1已发布 2已屏蔽 3审核不通过',
  `audit_remark`    VARCHAR(255) DEFAULT NULL,
  -- 互动
  `like_count`      INT          NOT NULL DEFAULT 0,
  `reply_count`     INT          NOT NULL DEFAULT 0,
  -- 追评
  `is_follow_up`    TINYINT      NOT NULL DEFAULT 0 COMMENT '是否追评',
  `parent_id`       BIGINT       DEFAULT NULL COMMENT '追评指向的首评 ID',
  `follow_up_content` VARCHAR(2000) DEFAULT NULL,
  -- 购买标记
  `buy_count`       INT          NOT NULL DEFAULT 1 COMMENT '购买数量',
  `is_verified`     TINYINT      NOT NULL DEFAULT 1 COMMENT '是否已验证购买（冗余，永远为1）',
  `created_at`      DATETIME(3)  NOT NULL,
  `updated_at`      DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  -- ★★★ 只能评论一次的核心保证
  UNIQUE KEY `uk_user_sku_item` (`user_id`, `sku_id`, `order_item_id`),
  KEY `idx_spu_status_time` (`spu_id`, `status`, `created_at`) COMMENT '商品详情页评价列表',
  KEY `idx_user` (`user_id`, `created_at`) COMMENT '我的评价',
  KEY `idx_order` (`order_main_no`),
  KEY `idx_sku_score` (`sku_id`, `score`) COMMENT 'SKU 维度好评率统计'
) ENGINE=InnoDB COMMENT='商品评价';
```

**唯一索引 `uk_user_sku_item` 的解析**：

| 索引方案 | 问题 |
|---|---|
| `uk(user_id, spu_id)` | 用户买同款 2 次只能评 1 次，不合理（第二次购买是新的体验） |
| `uk(user_id, sku_id)` | 同上，且用户换了规格（不同 SKU）也评不了 |
| **`uk(user_id, sku_id, order_item_id)`** | ✅ 每次购买（每个订单项）可评一次 |

**关键理解**：评论的粒度是**订单项（order_item）**，不是商品。用户买同一 SKU 三次（三个订单项），可以评三次。每次评论对应一次真实的购买。

### 2.2 评论资格的校验

```java
public ReviewEligibility checkEligibility(Long userId, Long orderItemId) {
    OrderItem item = orderItemMapper.selectById(orderItemId);
    if (item == null) {
        return ReviewEligibility.no(ORDER_ITEM_NOT_FOUND, "订单不存在");
    }

    // ① 归属校验：必须是自己的订单
    OrderSub sub = subMapper.selectByNo(item.getOrderSubNo());
    if (!sub.getUserId().equals(userId)) {
        return ReviewEligibility.no(NOT_YOUR_ORDER, "无权评价该订单");
    }

    // ② 订单项未被退款
    if (item.getItemStatus() == ItemStatus.REFUNDED) {
        return ReviewEligibility.no(ITEM_REFUNDED, "该商品已退款，无法评价");
    }

    // ③ 订单状态：已完成（已签收）
    //    ★ 待收货状态不能评价（还没收到货）
    if (sub.getStatus() != SubOrderStatus.FINISHED) {
        if (sub.getStatus() == SubOrderStatus.WAIT_RECEIVE) {
            return ReviewEligibility.no(NOT_RECEIVED, "确认收货后才能评价");
        }
        if (sub.getStatus() == SubOrderStatus.REFUNDING
            || sub.getStatus() == SubOrderStatus.REFUNDED) {
            return ReviewEligibility.no(IN_AFTERSALE, "售后处理中，暂不能评价");
        }
        return ReviewEligibility.no(ORDER_NOT_FINISHED, "订单完成后才能评价");
    }

    // ④ 评价窗口：签收后 30 天内（可配置）
    LocalDateTime deadline = sub.getReceiveTime().plusDays(30);
    if (LocalDateTime.now().isAfter(deadline)) {
        return ReviewEligibility.no(REVIEW_EXPIRED, "评价期限已过（签收后 30 天内）");
    }

    // ⑤ ★ 是否已评价（唯一索引的预检查）
    int count = reviewMapper.countByUserSkuItem(userId, item.getSkuId(), orderItemId);
    if (count > 0) {
        return ReviewEligibility.no(ALREADY_REVIEWED, "该商品已评价");
    }

    return ReviewEligibility.yes(item, sub);
}
```

**第 ⑤ 步是"预检查"，不是"保证"**。真正的保证是唯一索引。并发两次提交时，两个请求的预检查都通过，但只有一个能 INSERT 成功。

```java
@Transactional
public Long submitReview(Long userId, ReviewSubmitRequest req) {
    // ① 资格校验（快速失败，好体验）
    ReviewEligibility e = checkEligibility(userId, req.getOrderItemId());
    if (!e.isEligible()) {
        throw new BusinessException(e.getCode(), e.getMessage());
    }

    // ② 内容校验
    validateContent(req);   // 长度、图片数量、敏感词

    // ③ 构建评论
    Review review = buildReview(userId, e, req);
    review.setStatus(ReviewStatus.PENDING_AUDIT.getCode());

    try {
        reviewMapper.insert(review);
    } catch (DuplicateKeyException ex) {
        // ★★★ 唯一索引拦截：并发提交 / 重复提交
        //     这不是错误，而是"已经评价过了"，返回友好提示
        throw new BusinessException(ALREADY_REVIEWED, "该商品已评价，不能重复评价");
    }

    // ④ 发事件（异步：审核、更新商品评分、送积分）
    eventPublisher.publish(new ReviewCreatedEvent(review.getId()));

    return review.getId();
}
```

**`DuplicateKeyException` 不是异常，是幂等保护生效**。代码里必须捕获并转成友好的业务提示，不能让用户看到 500 错误。

## 3. 评论的展示

### 3.1 商品详情页的评价区

```sql
-- 主查询：某 SPU 的评价（已发布状态）
SELECT * FROM review
WHERE spu_id = ? AND status = 1 AND is_follow_up = 0
ORDER BY
  CASE WHEN content IS NOT NULL AND content != '' THEN 0 ELSE 1 END,  -- 有内容的优先
  like_count DESC,
  created_at DESC
LIMIT 20;
```

**排序策略**：有图/有内容 > 点赞多 > 时间新。默认评价（无内容）沉底。

### 3.2 评价统计（必须在商品表冗余）

商品详情页需要展示"好评率 98%""共 1234 条评价"，**不能每次实时聚合**（大数据量下会拖垮 DB）。

```sql
-- 在 spu 表冗余统计字段
ALTER TABLE spu ADD COLUMN review_count      INT NOT NULL DEFAULT 0;
ALTER TABLE spu ADD COLUMN avg_score         DECIMAL(3,2) NOT NULL DEFAULT 5.00;
ALTER TABLE spu ADD COLUMN good_review_rate  DECIMAL(5,2) NOT NULL DEFAULT 100.00;
ALTER TABLE spu ADD COLUMN review_with_image INT NOT NULL DEFAULT 0;

-- 在 sku 表也冗余（规格维度的评价）
ALTER TABLE sku ADD COLUMN review_count INT NOT NULL DEFAULT 0;
ALTER TABLE sku ADD COLUMN avg_score    DECIMAL(3,2) NOT NULL DEFAULT 5.00;
```

**统计的更新方式**：

```java
// 方案 A：增量更新（推荐，评论量不大时）
@EventListener
public void onReviewPublished(ReviewPublishedEvent e) {
    // 审核通过 + 已发布时，才计入统计
    spuStatsMapper.incrReviewStats(e.getSpuId(), e.getScore(), hasImage(e));
}
```

```sql
UPDATE spu SET
    review_count = review_count + 1,
    avg_score = ROUND((avg_score * review_count + #{score}) / (review_count + 1), 2),
    good_review_rate = ROUND(
        (good_review_rate * review_count + IF(#{score} >= 4, 100, 0)) / (review_count + 1), 2),
    review_with_image = review_with_image + #{hasImage}
WHERE id = #{spuId};
```

**方案 B：定时全量重算（兜底，每日）**

```sql
-- 每日 03:00 重算，修正增量更新的漂移
UPDATE spu s
JOIN (
    SELECT spu_id,
           COUNT(*) AS cnt,
           ROUND(AVG(score), 2) AS avg_s,
           ROUND(SUM(IF(score >= 4, 1, 0)) / COUNT(*) * 100, 2) AS good_rate,
           SUM(IF(images IS NOT NULL AND JSON_LENGTH(images) > 0, 1, 0)) AS with_img
    FROM review
    WHERE status = 1 AND is_follow_up = 0
    GROUP BY spu_id
) r ON s.id = r.spu_id
SET s.review_count = r.cnt,
    s.avg_score = r.avg_s,
    s.good_review_rate = r.good_rate,
    s.review_with_image = r.with_img;
```

**两个方案都要有**：增量保证实时性，全量保证正确性。

## 4. 评论审核

```java
public enum ReviewStatus {
    PENDING_AUDIT(0, "待审核"),
    PUBLISHED(1, "已发布"),
    BLOCKED(2, "已屏蔽"),
    REJECTED(3, "审核不通过");
}
```

**审核流程**：

```
① 用户提交 → status = 0（待审核），不对外展示
② 机器学习预审（1 秒内）
   ├─ 明显违规（广告、涉政、辱骂）→ status = 3，通知用户
   ├─ 疑似违规 → 进入人工队列
   └─ 正常 → 自动通过 → status = 1
③ 人工复审（队列消化）
④ 用户申诉 → 重新审核
```

**"先发后审" vs "先审后发"**：

| 模式 | 优点 | 缺点 | 适用 |
|---|---|---|---|
| 先审后发 | 内容安全 | 用户看到"审核中"，体验差 | 高风险类目（图书、医药） |
| **先发后审** | 体验好，实时可见 | 有违规内容短暂暴露 | **推荐：大多数类目** |
| 混合 | 机审通过的先发，疑似的人工审 | 平衡 | 最佳实践 |

**推荐混合模式**：

```java
if (machineAudit.isHighRisk(content)) {
    review.setStatus(PENDING_AUDIT);       // 高风险，先审后发
} else {
    review.setStatus(PUBLISHED);           // 低风险，先发后审
    review.setNeedSecondAudit(true);       // 但标记待二次审核
}
```

## 5. 追评

**规则**：首评后 30 天内可以追评一次（追评也算"已评论"状态，但不受 `uk_user_sku_item` 限制，因为它是一条新的记录且 `parent_id` 指向首评）。

```sql
-- 追评的幂等：一个首评只能有一条追评
UNIQUE KEY `uk_parent` (`parent_id`, `is_follow_up`)   -- 注意：NULL 不参与唯一约束
```

**MySQL 唯一索引与 NULL 的坑**：`(parent_id, is_follow_up)` 中，首评的 `parent_id = NULL`，MySQL 的唯一索引允许**多个 NULL**，所以首评不受影响（这是期望行为）。而追评 `parent_id` 有值、`is_follow_up = 1`，唯一约束生效。

**追评的业务价值**：用户可以补充"用了两周后"的真实体验，这是最可信的评价内容。展示上追评要**紧跟在首评下方**，并标注"追评"标签。

## 6. 商家回复

```sql
CREATE TABLE `review_reply` (
  `id`          BIGINT      NOT NULL AUTO_INCREMENT,
  `review_id`   BIGINT      NOT NULL,
  `reply_type`  TINYINT     NOT NULL COMMENT '1商家回复 2平台回复 3用户追加',
  `replier_id`  BIGINT      NOT NULL,
  `content`     VARCHAR(500) NOT NULL,
  `status`      TINYINT     NOT NULL DEFAULT 1 COMMENT '0待审 1已发布 2已删除',
  `created_at`  DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_review` (`review_id`, `status`)
) ENGINE=InnoDB;
```

**商家回复的约束**：一条评价最多 3 次商家回复（防刷屏），这是业务规则，在 Service 层校验。

## 7. 评价对商品排序的影响

评价是搜索排序的重要因素。**更新 ES 的评分字段**：

```java
@EventListener
public void onReviewStatsUpdated(ReviewStatsUpdatedEvent e) {
    // 异步更新 ES（不阻塞用户请求）
    esClient.update(u -> u.index("spu").id(String.valueOf(e.getSpuId()))
        .doc(Map.of(
            "avgScore", e.getAvgScore(),
            "reviewCount", e.getReviewCount(),
            "goodReviewRate", e.getGoodReviewRate()
        )));
}
```

**搜索排序公式**（示例）：

```
score = 0.5 * log(1 + totalSold)
      + 0.3 * goodReviewRate / 100
      + 0.2 * log(1 + reviewCount)
      + shopWeight
      + matchScore
```

## 8. 评价送积分

**激励用户评价**（但要有质量门槛，避免刷评）：

| 行为 | 奖励 | 约束 |
|---|---|---|
| 文字评价 | 10 积分 | 内容 ≥ 15 字 |
| 图文评价 | 30 积分 | 图片 ≥ 1 张，内容 ≥ 15 字 |
| 视频评价 | 50 积分 | 视频时长 ≥ 5s |
| 追评 | 10 积分 | 内容 ≥ 15 字 |

**领取时机**：审核通过 + 已发布后发放（防止"提交即拿积分后删除"）。

**幂等**：`points_flow.biz_key = "REVIEW:{reviewId}"`。

```java
@EventListener
public void onReviewPublished(ReviewPublishedEvent e) {
    long points = calcReward(e.getReview());
    if (points > 0) {
        pointsClient.award(e.getUserId(), points,
            "REVIEW:" + e.getReviewId(), "评价奖励");
    }
}
```

## 9. 关键边界场景

| 场景 | 处理 |
|---|---|
| 用户未购买就调评论接口 | `checkEligibility` 拦截（订单项不存在或不属于该用户） |
| 用户对已退款的订单项评论 | 拦截（`item_status = REFUNDED`） |
| 用户对同一订单项提交 2 次（间隔 1 秒） | 唯一索引拦截，返回"已评价" |
| 用户买同款 3 次（3 个订单项） | 可以评 3 次（各自对应一次购买） |
| 用户换规格购买（不同 SKU） | 不同 SKU 有不同订单项，可以分别评 |
| 用户订单已完成后 40 天评价 | 拒绝（超过 30 天窗口） |
| 已评价的商品发生退款 | 评论**保留**（评价是当时体验的真实记录），但展示时标注"该商品已退款" |
| 商家下架商品后评价 | 评论保留，商品详情页不可见（因为商品页不存在），但用户"我的评价"里可见 |
| 部分退款后剩余商品评价 | 允许（订单项未全部退款） |
| 评论含敏感词 | 机审拦截 → 待人工审核 |
| 用户删除自己的评论 | **不允许删除**，只能申请屏蔽（防止"拿积分后删评"） |
| 追评超过 1 次 | 拒绝（业务规则） |
| 商品无任何评价 | 展示"暂无评价"，好评率显示 100% 但**不展示**（避免误导），显示"暂无评分" |

**"评论不可删除"的设计理由**：如果允许删除，则：
1. 用户拿完积分后删除，等于白拿；
2. 商家可以诱导用户删除差评；
3. 评价的可信度下降。

**替代方案**：用户可"申请屏蔽"（需平台审核），屏蔽后评论不展示但不删除（保留审计）。

## 10. 性能设计

| 场景 | 方案 |
|---|---|
| 商品详情页评价列表 | 按 `(spu_id, status, created_at)` 索引，前 20 条；翻页用游标（`created_at < ?`）而非 offset |
| 评价统计 | SPU/SKU 表冗余字段，不实时聚合 |
| 图片展示 | CDN + 缩略图（`?x-oss-process=image/resize,w_200`） |
| 评价数量大（100 万+） | 按 `spu_id` 分表；或用 ES 存评价，DB 只存原件 |
| 好评率筛选 | 在 ES 里存 `score`，支持按评分区间筛选 |

**分页的坑**：`LIMIT 100000, 20` 会扫描 10 万行。用游标分页：

```sql
-- ❌ 深分页慢
SELECT * FROM review WHERE spu_id = ? AND status = 1 LIMIT 100000, 20;

-- ✅ 游标分页
SELECT * FROM review
WHERE spu_id = ? AND status = 1
  AND (created_at, id) < (?, ?)      -- 上一页最后一条的 (created_at, id)
ORDER BY created_at DESC, id DESC
LIMIT 20;
```

## 11. 反刷评

| 手段 | 说明 |
|---|---|
| 购买验证 | 必须有真实订单项（唯一索引的 `order_item_id` 保证） |
| 频次限制 | 单用户每天最多 20 条评价 |
| 内容重复检测 | 同一用户的多条评价相似度 > 90% → 标记 |
| IP/设备聚合 | 同 IP 大量评价 → 进入人工审核 |
| 评价时间分析 | 签收后 1 分钟内评价（没时间体验）→ 标记疑似 |
| 图片重复检测 | 图片 MD5 去重，同一图片在多条评价出现 → 标记 |
| 商家异常检测 | 某商家短期内好评率从 90% 突增到 100% → 预警刷单 |

**"签收后立刻评价"的判定**：正常用户至少需要几分钟体验。`receive_time` 到 `created_at` 间隔 < 60 秒的评价，标记为 `suspect`（不影响展示，但降低排序权重并送人工抽检）。
