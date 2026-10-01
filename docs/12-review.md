# 12 评论系统

## 1. 需求：购买后才能评论，且只能评论一次

| 约束 | 实现手段 |
|---|---|
| **只能购买后评论** | 订单项状态校验：子单已完成（已签收），且该订单项属于当前用户 |
| **只能评论一次** | 部分唯一索引 `uk_review_order_item (order_item_id) WHERE NOT is_follow_up` |

## 2. 评论资格的判定

### 2.1 表结构

```sql
CREATE TABLE review.review (
  id                BIGINT        PRIMARY KEY,
  user_id           BIGINT        NOT NULL,
  spu_id            BIGINT        NOT NULL,
  sku_id            BIGINT        NOT NULL,
  -- ★ 来源订单（这几列是"购后评论"的证据）
  order_item_id     BIGINT        NOT NULL,          -- 订单项 ID
  order_sub_no      VARCHAR(32)   NOT NULL,
  order_main_no     VARCHAR(32)   NOT NULL,
  -- 规格快照（用户当时买的规格）
  sku_spec_snap     VARCHAR(255)  NOT NULL,          -- 如"暗夜黑;256G"
  -- 内容
  score             SMALLINT      NOT NULL CHECK (score BETWEEN 1 AND 5), -- 1-5 星
  content           VARCHAR(2000),
  images            JSONB         NOT NULL DEFAULT '[]'::jsonb  -- 图片相对路径数组，最多 9 张
                    CHECK (jsonb_typeof(images) = 'array' AND jsonb_array_length(images) <= 9),
  video_url         VARCHAR(255),                    -- 最多 1 个视频（二期）
  -- 维度评分（可选，按类目配置）
  dimension_scores  JSONB,                           -- {"描述相符":5,"物流服务":4,"服务态度":5}
  anonymous         BOOLEAN       NOT NULL DEFAULT false,
  -- 状态
  status            SMALLINT      NOT NULL DEFAULT 0, -- 0待审核 1已发布 2已屏蔽 3审核不通过
  need_second_audit BOOLEAN       NOT NULL DEFAULT false,
  suspect           BOOLEAN       NOT NULL DEFAULT false, -- 疑似刷评（见 §11）
  audit_remark      VARCHAR(255),
  -- 互动
  like_count        INT           NOT NULL DEFAULT 0,
  reply_count       INT           NOT NULL DEFAULT 0,
  -- 追评：追评是一条独立记录，parent_id 指向首评
  is_follow_up      BOOLEAN       NOT NULL DEFAULT false,
  parent_id         BIGINT        REFERENCES review.review (id),
  buy_count         INT           NOT NULL DEFAULT 1, -- 购买数量
  created_at        TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT ck_review_follow_up CHECK (is_follow_up = (parent_id IS NOT NULL))
);
-- ★★★ 只能评论一次的核心保证：每个订单项只有一条首评
CREATE UNIQUE INDEX uk_review_order_item ON review.review (order_item_id) WHERE NOT is_follow_up;
-- ★ 一个首评只能有一条追评
CREATE UNIQUE INDEX uk_review_follow_up  ON review.review (parent_id) WHERE is_follow_up;
-- 商品详情页评价列表（只索引已发布的首评）
CREATE INDEX idx_review_spu_list ON review.review (spu_id, created_at DESC, id DESC)
  WHERE status = 1 AND NOT is_follow_up;
CREATE INDEX idx_review_user  ON review.review (user_id, created_at DESC);  -- 我的评价
CREATE INDEX idx_review_order ON review.review (order_main_no);
COMMENT ON TABLE review.review IS '商品评价';
```

**唯一约束的选择**：

| 方案 | 问题 |
|---|---|
| `unique(user_id, spu_id)` | 用户买同款 2 次只能评 1 次，不合理（第二次购买是新的体验） |
| `unique(user_id, sku_id)` | 同上，且用户换了规格（不同 SKU）也评不了 |
| `unique(user_id, sku_id, order_item_id)` | 正确但冗余：一个订单项只属于一个用户、一个 SKU，前两列不增加区分度 |
| **`unique(order_item_id) WHERE NOT is_follow_up`** | ✅ 每次购买（每个订单项）可首评一次，追评不受此约束 |

**关键理解**：评论的粒度是**订单项（order_item）**，不是商品。用户买同一 SKU 三次（三个订单项），可以评三次。每次评论对应一次真实的购买。

**为什么用部分唯一索引**：原方案依赖"MySQL 唯一索引允许多个 NULL"来让首评绕过追评的唯一约束，这种写法语义隐晦。PostgreSQL 支持 `WHERE` 条件的部分唯一索引，首评和追评各自的约束可以直接写清楚。

> `ck_review_follow_up` 保证"是追评 ⇔ 有 parent_id"，避免出现 `is_follow_up = false` 但带了 `parent_id` 的脏数据绕过两个唯一索引。

### 2.2 评论资格的校验

```python
# app/modules/review/service.py
REVIEW_WINDOW = timedelta(days=30)

async def check_eligibility(session: AsyncSession, user_id: int, order_item_id: int) -> Eligibility:
    row = await trade_service.get_item_with_sub(session, order_item_id)   # 同进程调用 trade 模块
    if row is None:
        return Eligibility.no(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")
    item, sub = row

    # ① 归属校验：必须是自己的订单（不存在与不属于返回同样的提示，避免探测他人订单）
    if sub.user_id != user_id:
        return Eligibility.no(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")

    # ② 订单项未被全部退款
    if item.item_status == ItemStatus.REFUNDED:
        return Eligibility.no(ErrorCode.ITEM_REFUNDED, "该商品已退款，无法评价")

    # ③ 订单状态：已完成（已签收）。★ 待收货状态不能评价（还没收到货）
    if sub.status != SubOrderStatus.FINISHED:
        if sub.status == SubOrderStatus.WAIT_RECEIVE:
            return Eligibility.no(ErrorCode.NOT_RECEIVED, "确认收货后才能评价")
        if sub.status in (SubOrderStatus.REFUNDING, SubOrderStatus.REFUNDED):
            return Eligibility.no(ErrorCode.IN_AFTERSALE, "售后处理中，暂不能评价")
        return Eligibility.no(ErrorCode.ORDER_NOT_FINISHED, "订单完成后才能评价")

    # ④ 评价窗口：签收后 30 天内（可配置）
    if datetime.now(UTC) > sub.receive_time + REVIEW_WINDOW:
        return Eligibility.no(ErrorCode.REVIEW_EXPIRED, "评价期限已过（签收后 30 天内）")

    # ⑤ ★ 是否已评价（唯一索引的预检查，只为给出友好提示）
    if await review_repo.exists_first_review(session, order_item_id):
        return Eligibility.no(ErrorCode.ALREADY_REVIEWED, "该商品已评价")

    return Eligibility.yes(item, sub)
```

**第 ⑤ 步是"预检查"，不是"保证"**。真正的保证是唯一索引。并发两次提交时，两个请求的预检查都通过，但只有一个能 INSERT 成功。

```python
async def submit_review(session: AsyncSession, user_id: int, req: ReviewSubmitRequest) -> int:
    """调用方（路由）的会话内执行，事务边界由 DbSession 依赖统一管理。"""
    # ① 资格校验（快速失败，好体验）
    e = await check_eligibility(session, user_id, req.order_item_id)
    if not e.eligible:
        raise BizError(e.code, e.message)

    # ② 内容校验：长度、图片数量、图片路径必须是本用户上传的、敏感词
    validate_content(req, user_id)
    status, need_second_audit = machine_audit(req.content)          # 见 §4

    # ③ 写入：唯一索引冲突时 RETURNING 为空，不抛异常（事务不会进入 aborted 状态）
    review_id = await session.scalar(
        pg_insert(Review)
        .values(id=snowflake.next_id(), user_id=user_id, **e.to_review_fields(),
                score=req.score, content=req.content, images=req.images,
                anonymous=req.anonymous, status=status, need_second_audit=need_second_audit,
                suspect=is_suspect(e.sub.receive_time))
        .on_conflict_do_nothing(index_elements=["order_item_id"],
                                index_where=text("NOT is_follow_up"))
        .returning(Review.id)
    )
    if review_id is None:
        # ★★★ 唯一索引拦截：并发提交 / 重复提交 —— 这不是系统错误，而是"已经评价过了"
        raise BizError(ErrorCode.ALREADY_REVIEWED, "该商品已评价，不能重复评价")

    # ④ 写 outbox（异步：更新商品评分、发积分、二次审核）
    await outbox.add(session, topic="review.created", biz_key=f"REVIEW_CREATED:{review_id}",
                     payload={"reviewId": review_id, "status": status})
    await trade_service.mark_item_reviewed(session, req.order_item_id)
    return review_id
```

**唯一冲突不是异常，是幂等保护生效**。必须转成友好的业务提示，不能让用户看到 500。用 `ON CONFLICT DO NOTHING` 而不是捕获 `IntegrityError`，原因见 [10 §7](10-idempotency.md)。

## 3. 评论的展示

### 3.1 商品详情页的评价区

```sql
-- 默认排序：时间倒序，游标分页（命中部分索引 idx_review_spu_list）
SELECT id, user_id, score, content, images, sku_spec_snap, anonymous, like_count, created_at
FROM review.review
WHERE spu_id = :spu_id AND status = 1 AND NOT is_follow_up
  AND (created_at, id) < (:cursor_time, :cursor_id)       -- 首页不带这个条件
ORDER BY created_at DESC, id DESC
LIMIT 20;
```

**"有内容的优先、点赞多的优先"排序**不适合实时计算（无法走索引，越翻越慢）。做法：在表上增加一个冗余的 `rank_score INT`，评价发布、点赞变化时更新（`有内容 +1000、有图 +500、点赞数`），"推荐排序"按 `(spu_id, rank_score DESC, id DESC)` 建部分索引做游标分页。默认评价（无内容）`rank_score` 最低，自然沉底。

### 3.2 评价统计（必须在商品表冗余）

商品详情页需要展示"好评率 98%""共 1234 条评价"，**不能每次实时聚合**。统计口径用"计数"而不是"平均值"冗余，避免增量更新时反复做除法引入舍入漂移：

```sql
-- 在 spu 表冗余统计字段（02 中的 review_count / avg_score 改为由这些计数推导）
ALTER TABLE product.spu
  ADD COLUMN review_score_sum  BIGINT NOT NULL DEFAULT 0,  -- 评分之和
  ADD COLUMN good_review_count INT    NOT NULL DEFAULT 0,  -- 4 星及以上
  ADD COLUMN review_with_image INT    NOT NULL DEFAULT 0;
-- 展示时计算：avg = score_sum / review_count，好评率 = good / review_count

-- 在 sku 表也冗余（规格维度的评价）
ALTER TABLE product.sku
  ADD COLUMN review_count     INT    NOT NULL DEFAULT 0,
  ADD COLUMN review_score_sum BIGINT NOT NULL DEFAULT 0;
```

**统计的更新方式**：

**方案 A：增量更新**（outbox 事件 `review.published` 的消费者，在 worker 中执行）

```sql
UPDATE product.spu SET
    review_count      = review_count + 1,
    review_score_sum  = review_score_sum + :score,
    good_review_count = good_review_count + CASE WHEN :score >= 4 THEN 1 ELSE 0 END,
    review_with_image = review_with_image + CASE WHEN :has_image THEN 1 ELSE 0 END,
    avg_score         = ROUND((review_score_sum + :score)::numeric / (review_count + 1), 2)
WHERE id = :spu_id;
```

消费者按 `biz_key = REVIEW_STAT:{reviewId}` 幂等（先插 `review.stat_biz_key` 表，`ON CONFLICT DO NOTHING`，rowcount = 1 才执行更新），防止事件重复投递导致重复计数。

**方案 B：定时全量重算**（兜底，ARQ cron 每日 03:00）

```sql
-- 修正增量更新的漂移（PG 用 UPDATE ... FROM，聚合用 FILTER）
UPDATE product.spu s
SET review_count      = r.cnt,
    review_score_sum  = r.score_sum,
    good_review_count = r.good_cnt,
    review_with_image = r.with_img,
    avg_score         = ROUND(r.score_sum::numeric / r.cnt, 2)
FROM (
    SELECT spu_id,
           COUNT(*)                                          AS cnt,
           SUM(score)                                        AS score_sum,
           COUNT(*) FILTER (WHERE score >= 4)                AS good_cnt,
           COUNT(*) FILTER (WHERE jsonb_array_length(images) > 0) AS with_img
    FROM review.review
    WHERE status = 1 AND NOT is_follow_up
    GROUP BY spu_id
) r
WHERE s.id = r.spu_id
  AND (s.review_count, s.review_score_sum, s.good_review_count, s.review_with_image)
      IS DISTINCT FROM (r.cnt, r.score_sum, r.good_cnt, r.with_img);   -- 只更新有偏差的行
```

> 这条 SQL 是跨模块访问（review 模块读、写 product 表），属于 [01 §2](01-overview.md) "禁止跨模块读写对方的表" 的例外：它是运维性质的对账任务，放在 `app/worker/reconcile/` 下统一管理，不出现在业务代码里。

**两个方案都要有**：增量保证实时性，全量保证正确性。

## 4. 评论审核

```python
class ReviewStatus(IntEnum):
    PENDING_AUDIT = 0   # 待审核
    PUBLISHED = 1       # 已发布
    BLOCKED = 2         # 已屏蔽
    REJECTED = 3        # 审核不通过
```

**审核流程**（第一期不接第三方内容安全服务，用本地敏感词库 + 人工审核）：

```
① 用户提交 → 机审（同步，毫秒级）
   ├─ 命中高风险词（广告联系方式、辱骂、涉政）→ status = 0 待审核，进入人工队列
   └─ 未命中 → status = 1 直接发布，need_second_audit = true（抽检）
② 运营后台人工审核队列：待审核 + 被举报 + 抽检
③ 用户申诉 → 重新进入人工队列
```

敏感词匹配用 Aho-Corasick 自动机（`pyahocorasick`，词库启动时加载、运营后台修改词库后通过事件通知各进程重载）。图片第一期不做机审，全部走人工抽检；二期接腾讯云内容安全（需要额外的 API 密钥，见 [16](16-deployment.md) 凭证清单中的二期项）。

**"先发后审" vs "先审后发"**：

| 模式 | 优点 | 缺点 | 适用 |
|---|---|---|---|
| 先审后发 | 内容安全 | 用户看到"审核中"，体验差 | 高风险类目（图书、医药） |
| 先发后审 | 体验好，实时可见 | 有违规内容短暂暴露 | 低风险类目 |
| **混合** | 机审通过的先发，疑似的人工审 | 平衡 | **采用** |

```python
def machine_audit(content: str | None) -> tuple[ReviewStatus, bool]:
    """返回 (初始状态, 是否需要二次审核)。"""
    if content and sensitive_words.hit_high_risk(content):
        return ReviewStatus.PENDING_AUDIT, False     # 高风险，先审后发
    return ReviewStatus.PUBLISHED, True              # 低风险，先发后审（标记待抽检）
```

## 5. 追评

**规则**：首评发布后 30 天内可以追评一次。追评是一条新记录，`is_follow_up = true`、`parent_id` 指向首评，`order_item_id` 等字段从首评复制。

约束由数据库直接表达（见 §2.1）：

```sql
-- 首评：每个订单项一条
CREATE UNIQUE INDEX uk_review_order_item ON review.review (order_item_id) WHERE NOT is_follow_up;
-- 追评：每个首评一条
CREATE UNIQUE INDEX uk_review_follow_up  ON review.review (parent_id) WHERE is_follow_up;
```

追评的资格校验：首评存在且属于当前用户、首评状态为已发布、在追评窗口内；写入同样用 `ON CONFLICT DO NOTHING`，冲突返回"已追评过"。

**追评的业务价值**：用户可以补充"用了两周后"的真实体验，这是最可信的评价内容。展示上追评要**紧跟在首评下方**，并标注"追评"标签（查询首评列表后，用 `WHERE parent_id = ANY(:ids) AND is_follow_up` 一次批量取回追评）。

## 6. 商家回复

```sql
CREATE TABLE review.review_reply (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  review_id   BIGINT       NOT NULL REFERENCES review.review (id),
  reply_type  SMALLINT     NOT NULL,          -- 1商家回复 2平台回复
  replier_id  BIGINT       NOT NULL,
  content     VARCHAR(500) NOT NULL,
  status      SMALLINT     NOT NULL DEFAULT 1, -- 0待审 1已发布 2已删除
  created_at  TIMESTAMPTZ(3) NOT NULL DEFAULT now()
);
CREATE INDEX idx_review_reply_review ON review.review_reply (review_id, status);
```

**商家回复的约束**：一条评价最多 3 次商家回复（防刷屏）。在 service 层对评价行 `SELECT ... FOR UPDATE` 后计数校验，避免并发回复绕过上限。商家只能回复**自己店铺商品**的评价（通过 `spu.shop_id` 与当前登录商家的 `shop_id` 比对）。

## 7. 评价对商品排序的影响

评价是搜索排序的重要因素。第一期搜索走 PostgreSQL（[02 §7](02-domain-model.md)），排序直接用 `spu` 表上的冗余字段，不需要额外同步：

```sql
-- "综合排序"：预先计算好的排序分，评价统计/销量变化时由 worker 更新
ALTER TABLE product.spu ADD COLUMN search_rank NUMERIC(10,4) NOT NULL DEFAULT 0;
CREATE INDEX idx_spu_search_rank ON product.spu (search_rank DESC, id DESC) WHERE status = 2 AND NOT deleted;
```

**排序分公式**（示例，在 worker 中计算后写入 `search_rank`）：

```
search_rank = 0.5 * ln(1 + total_sold)
            + 0.3 * good_review_count / max(review_count, 1)
            + 0.2 * ln(1 + review_count)
            + shop_weight
```

关键词相关度（`pg_trgm` 的 `similarity()`）在查询时计算，与 `search_rank` 组合排序：`ORDER BY similarity(search_text, :kw) * 0.5 + search_rank DESC`。数据量大后这种组合排序会变慢，届时再考虑专门的搜索引擎。

## 8. 评价送积分

**激励用户评价**（但要有质量门槛，避免刷评）：

| 行为 | 奖励 | 约束 |
|---|---|---|
| 文字评价 | 10 积分 | 内容 ≥ 15 字 |
| 图文评价 | 30 积分 | 图片 ≥ 1 张，内容 ≥ 15 字 |
| 视频评价 | 50 积分 | 视频时长 ≥ 5s（二期） |
| 追评 | 10 积分 | 内容 ≥ 15 字 |

**领取时机**：状态变为"已发布"后发放（防止"提交即拿积分后删除"）。被二次审核屏蔽的评价**不追回积分**（第一期简化），但计入用户信用分。

**幂等**：`account.points_biz_key.biz_key = "REVIEW:{reviewId}"`。

```python
# worker 中 review.published 事件的消费者
async def on_review_published(session: AsyncSession, event: ReviewPublished) -> None:
    points = calc_reward(event.review)
    if points > 0:
        await points_service.award(session, event.user_id, points,
                                   biz_key=f"REVIEW:{event.review_id}", remark="评价奖励")
```

## 9. 关键边界场景

| 场景 | 处理 |
|---|---|
| 用户未购买就调评论接口 | `check_eligibility` 拦截（订单项不存在或不属于该用户） |
| 用户对已退款的订单项评论 | 拦截（`item_status = REFUNDED`） |
| 用户对同一订单项提交 2 次（间隔 1 秒） | 部分唯一索引拦截，返回"已评价" |
| 用户买同款 3 次（3 个订单项） | 可以评 3 次（各自对应一次购买） |
| 用户换规格购买（不同 SKU） | 不同 SKU 有不同订单项，可以分别评 |
| 用户订单已完成后 40 天评价 | 拒绝（超过 30 天窗口） |
| 已评价的商品发生退款 | 评论**保留**（评价是当时体验的真实记录），但展示时标注"该商品已退款" |
| 商家下架商品后评价 | 评论保留，商品详情页不可见，用户"我的评价"里可见 |
| 部分退款后剩余商品评价 | 允许（订单项未全部退款） |
| 评论含敏感词 | 机审命中 → 待人工审核 |
| 用户删除自己的评论 | **不允许删除**，只能申请屏蔽（防止"拿积分后删评"） |
| 追评超过 1 次 | 部分唯一索引 `uk_review_follow_up` 拦截 |
| 评价图片路径是别人上传的 | `validate_content` 校验图片路径前缀为 `reviews/{user_id}/`，否则拒绝 |
| 商品无任何评价 | 展示"暂无评价"，不展示好评率（避免显示"100%"误导） |

**"评论不可删除"的设计理由**：如果允许删除，则：
1. 用户拿完积分后删除，等于白拿；
2. 商家可以诱导用户删除差评；
3. 评价的可信度下降。

**替代方案**：用户可"申请屏蔽"（需平台审核），屏蔽后评论不展示但不删除（保留审计）。

## 10. 性能设计

| 场景 | 方案 |
|---|---|
| 商品详情页评价列表 | 部分索引 `idx_review_spu_list`，游标分页（`(created_at, id) < (...)`），不用 `OFFSET` |
| 评价统计 | SPU/SKU 表冗余计数字段，不实时聚合 |
| 图片存储与展示 | 第一期存服务器本地卷，Nginx 直接提供静态文件；上传时用 `Pillow` 生成 200px 缩略图，列表页只加载缩略图 |
| 评价数量很大（千万级） | 按 `spu_id` 哈希分区（PG 声明式分区）；分区键需加入主键与唯一索引，届时唯一约束改为 `(spu_id, order_item_id)` |
| 按评分筛选 | 加部分索引 `(spu_id, score, created_at DESC) WHERE status = 1 AND NOT is_follow_up` |

**图片上传的安全要点**（第一期本地存储）：

- 只接受 `image/jpeg`、`image/png`、`image/webp`，**按文件内容**（`Pillow` 打开并重新编码）判断类型，不信任扩展名和 `Content-Type`；
- 重新编码时去掉 EXIF（可能含拍摄地 GPS）；
- 单张 ≤ 5MB，Nginx `client_max_body_size` 同步限制；
- 文件名由服务端生成（`reviews/{user_id}/{uuid}.webp`），不使用用户提供的文件名，防止路径穿越；
- 存储目录在 Nginx 配置中禁止执行脚本，只作为静态文件提供。

**分页的坑**：`LIMIT 20 OFFSET 100000` 会扫描 10 万行。用游标分页：

```sql
-- ❌ 深分页慢
SELECT * FROM review.review WHERE spu_id = :spu_id AND status = 1 LIMIT 20 OFFSET 100000;

-- ✅ 游标分页（PG 支持行值比较，且能用上 (spu_id, created_at DESC, id DESC) 索引）
SELECT * FROM review.review
WHERE spu_id = :spu_id AND status = 1 AND NOT is_follow_up
  AND (created_at, id) < (:last_created_at, :last_id)   -- 上一页最后一条
ORDER BY created_at DESC, id DESC
LIMIT 20;
```

## 11. 反刷评

| 手段 | 说明 |
|---|---|
| 购买验证 | 必须有真实订单项（`order_item_id` + 归属校验 + 唯一索引保证） |
| 频次限制 | 单用户每天最多 20 条评价（Redis 计数 `review:daily:{userId}:{yyyymmdd}`） |
| 内容重复检测 | 同一用户的多条评价相似度高 → 标记（PG `pg_trgm` 的 `similarity()` > 0.9） |
| IP/设备聚合 | 同 IP 大量评价 → 进入人工审核 |
| 评价时间分析 | 签收后 1 分钟内评价（没时间体验）→ 标记疑似 |
| 图片重复检测 | 上传时计算图片 SHA-256，同一图片在多条评价出现 → 标记 |
| 商家异常检测 | 某商家短期内好评率突增 → 运营后台预警 |

**"签收后立刻评价"的判定**：正常用户至少需要几分钟体验。`receive_time` 到 `created_at` 间隔 < 60 秒的评价，写入时置 `suspect = true`（不影响展示，但降低 `rank_score` 并进入人工抽检队列）。
