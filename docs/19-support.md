# 19 客服系统（support）与站内信（notify）

## 1. 这一轮补的是哪一环

[18-account.md](18-account.md) §7 给**登录页与被冻结提示**补了可配置的静态联系方式
（`promotion.site_contact`）。那是「**登录之前**」的兜底 —— 被冻结的人登不进来，
也就打不开任何站内页面。

买家**登录之后**仍然没有任何沟通通道。在此之前，后端所有「客服」都是**运营侧**
概念：

| 已有的「客服」 | 属于谁 |
|---|---|
| 客服补发券（`POST /api/admin/coupons/issue`） | 运营 |
| 券码客服核销 | 运营 |
| `operator_type = 4 平台客服`（订单状态流水） | 运营 |
| `phone_cipher`「客服查看」 | 运营（且**从未被生产代码调用**） |

一个买家也够不着。这一轮补上「登录之后」，顺带把仓库里**已经写好但从未通电**的
outbox 接上。

**两条链路的分工**（都不取代对方）：

- **登录之后** → 客服会话 + 站内信（本文件）
- **登录之前 / 被冻结** → 静态联系方式（[18 §7](18-account.md)）

---

## 2. 四个关键决策

### 2.1 异步工单，不是实时 IM

| | 异步工单（采用） | 实时 IM |
|---|---|---|
| 新基础设施 | 无 | 连接注册表 + 心跳 + 离线消息 + 多实例广播（Redis pub/sub）+ nginx `Upgrade` |
| 测试 | 现有 HTTP + 真 PG 范式直接可用 | 要跑真连接，现有范式全部不适用 |
| 可审计 | 天然（消息落库） | 要额外做 |

电商客服的真实构成是**售后咨询为主**，异步完全够；实时那一层的成本几乎全在基础
设施而非业务。**升级条件**（写在这里以便日后判断）：日均会话 > 200 且「买家发出首条
消息后 60 秒内离开」的比例高 → 再上 WebSocket。届时 `ticket_message` 表**不动**，
只加一条推送路径。

### 2.2 会话归谁：`shop_id`，哨兵 `0` = 平台级

入口决定归属：商品页 / 订单页 / 售后页 → 该店；「联系平台客服」/ 申诉 → 平台级。

**商家回自己店的会话，平台客服能看全部并介入。** 不引入坐席概念（无分配、无排队、
无在线状态）——「谁先看到谁回」。

★ **`shop_id` 必须是 `NOT NULL DEFAULT 0`，不能用 NULL 表示平台级**：PG 的唯一索引
把 NULL 当作**互不相同**，可空的话下面那条部分唯一索引就形同虚设 —— 同一用户能
开出无数条平台级会话。这与 `inventory/routing.REGION_ALL = "0"`（"全国"）是同一套
哨兵思路。

### 2.3 状态只存两个；未读用两条游标

```
ticket.status ∈ {10 进行中, 30 已关闭}
```

**「欠谁一个回复」不另存一列**，由 `last_sender_type` 推出来
（`rules.staff_owes_reply` = 进行中 且 最后一条是买家）。
（`refund_order` 存了 `source_status` 是因为拒绝时要回到**原状态**，客服没有这个
需求，所以不学它。）

未读用 `user_read_at` / `staff_read_at` 两条游标，**不建按消息的已读表**：一期一条
会话只由一个客服处理，没有"多客服各自未读"的需求。游标算未读是**一次分组查询**
（`repository.unread_of`，join 之后逐行比 `m.created_at > t.user_read_at`），
**不存冗余计数** —— 那是同一事实的第二个来源。

★ 游标是**行上的值**，没法当成全局参数传进来，所以必须在 SQL 里逐行比。
没读过（游标为 NULL）时用 `-infinity` 兜底，否则 `> NULL` 求值为假，
**一条都没读过的会话反而会被算成"零条未读"**。

### 2.4 与订单 / 售后：弱关联

会话上存 `order_main_no` / `order_sub_no` / `refund_no` 的**快照**，详情页据此**跳转**
到既有页面，不在会话里重复渲染。

**不合并进 `aftersale`**：售后有**资金动作**（退款、库存回补），客服没有；混在一张
表里会让"这条消息会不会动钱"这个判断失效 —— 而那正是 `aftersale` 全部复杂度的来源。

★ 上下文**只用于展示与跳转**，**不参与鉴权**：归属永远来自会话自己的 `user_id` /
`shop_id`，点进去的订单页 / 售后页各自还有自己的归属校验。

---

## 3. 数据模型（`support` schema，三张表）

| 表 | 说明 |
|---|---|
| `support.ticket` | 会话。一个买家对一个对象一条 |
| `support.ticket_message` | 消息，**只追加，不可改不可删**（客服也不能删自己的话） |
| `support.ticket_state_flow` | 状态流水（命名照 `trade.order_state_flow`）。**审计表**，`scripts/harden_grants.py` 会收走应用角色的 UPDATE/DELETE |

`support.ticket` 的关键列与索引：

- `ticket_no` —— `build_ticket_no`（`T` + yyyyMMdd + 雪花后 10 位 + Luhn），与订单号
  同构；前缀不同，所以拿会话号去查订单会**直接失败**而不会查错人。
- **`CREATE UNIQUE INDEX uk_ticket_user_shop_active ON support.ticket (user_id, shop_id)
  WHERE status <> 30`** —— 一个用户对同一个对象同时只有一条进行中会话。
  **唯一性由数据库兜底**，不只是应用层的"先查再插"。
- `last_message_at` 是列表排序键；`(last_message_at, id)` 是复合游标。
- `last_sender_type` 初值是 **4 系统**，不是买家 —— 会话刚开出来时**还没有人说过话**。
  默认成买家的话，一个"点了联系客服但没发消息就走了"的空会话会在后台**永远显示成
  「待回复」**。

## 4. 站内信怎么发（`notify` schema，一张表）

`notify` schema 与 `notify.site_message` 这张表**在基线迁移与 docs/13 §1 里都早就
预留了**，但一直没建出来。

### 4.1 两条路径，各归其位

| 来源 | 机制 | 为什么 |
|---|---|---|
| **客服回复** | `support` 在**自己的事务里直写** `notify_service.push(...)` | 站内信与业务**同库**，能随事务一起回滚 —— outbox 想解决的问题（Redis / 短信这类**回滚不了**的副作用）在这里根本不存在 |
| **trade / payment / aftersale 的 6 个既有 topic** | 继续走 `core.local_message`，由 worker 的投递循环分派 | 这三个模块**不能** import `notify`（那是给 docs/01 §2 的依赖图加边）；**outbox 正是它们的解耦缝** |

★ 这条分工很重要：站内信**不是**"为了用而用 outbox"。直接写的那条路径是**事务性**
的（回复与通知同生共死），走 outbox 的那条是**跨模块解耦**的。

### 4.2 投递循环：**有意偏离 docs/13 §4 与 docs/14 §6**

那两处写的是「投递到 Redis Streams（`XADD`）+ 消费组（`XREADGROUP`/`XACK`）+ 五次进
死信」。**实现不引入 Streams**，在 worker 进程内就地分派
（`worker/outbox_delivery.py` + `notify/handlers.py`）。

理由：那一层要等**第二个独立进程**也要消费同一批事件时才有意义（比如把单体拆成
服务）；眼下唯一的消费者就是这个 worker，多一层只会多一份要各自对账的状态。
`core/redis_keys.py` 的 `stream()` / `stream_dead()` 因此**继续闲置** —— 真要拆服务时
`local_message` 仍是权威存储，可以重放。

保留的部分（与文档一致）：`status 0/1/2/3`、`FOR UPDATE SKIP LOCKED` 取件、
指数退避 `min(2^retry × 10s, 3600)`、第 15 次弃置并写 `ops.alert`。
cron **每 5 秒**一轮。

### 4.3 topic → 处理

| topic | 生产点 | 处理 |
|---|---|---|
| `notify.order_closed` | trade | 站内信「订单已关闭」 |
| `trade.order_paid` | payment | 站内信「支付成功」 |
| `aftersale.refund_succeeded` | aftersale | 站内信「退款已到账」 |
| `trade.sub_status_changed` | trade | 登记已处理，**不发**（不做逐步进度播报） |
| `aftersale.status_changed` | aftersale | 同上 |
| `inventory.redis_release` | trade | ★ **注册为 no-op，是有意留空** |

★ `inventory.redis_release` 为什么空着：那个 topic 承诺的是 **Redis** 侧预占回补
（DB 侧释放已经在关单事务里做完了，见 `trade/service.py` 的关单路径）。今天 Redis 的
漂移由 `reconcile_stock`（每 5 分钟）兜着，所以**不实现它 = 与这一轮之前的行为完全
一致**。要改成即时释放，得先想清楚交易路径上"Redis 预占"的释放语义（幂等键、
DB 已释放时 Redis 该不该再动），那是**单独一轮**的事。

站内信的 `biz_key` 带上 topic 前缀（`{topic}:{biz_key}`）：`site_message.biz_key` 是
全局唯一，而 outbox 的唯一键只是 `(topic, biz_key)`。

★ **至少一次**：handler 成功后才把 `status` 置 1，所以进程在中间崩溃会重放；幂等由
handler 自己保证（站内信是 `UNIQUE (biz_key)` + `ON CONFLICT DO NOTHING`）。因此
handler 内部的**部分写入**也能自愈，不必自己开保存点。

## 5. 接口

### 买家 `/api/support/*`（`CurrentUserDep`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/support/tickets` | 开会话。**已有进行中的会话就把那一条还给你**（不是报错）；不带 `shopId` = 平台级 |
| GET | `/api/support/tickets` | 我的会话（keyset 游标） |
| GET | `/api/support/tickets/{no}` | 详情 + 最近一页消息；`?before=<消息id>` 往前翻 |
| POST | `/api/support/tickets/{no}/messages` | 发消息 |
| POST | `/api/support/tickets/{no}/close` | 结束（重复调用**不报错**） |

### 商家 `/api/merchant/support/*`（`CurrentShopIdDep`，按**店铺归属**判权）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/merchant/support/tickets` | 本店队列，`pendingOnly=true` 只看待回复 |
| GET | `/api/merchant/support/pending-count` | 待回复条数（导航角标） |
| GET | `/api/merchant/support/tickets/{no}` | 详情 |
| POST | `/api/merchant/support/tickets/{no}/messages` | 回复（**同事务**给买家写站内信） |
| POST | `/api/merchant/support/tickets/{no}/close` | 结束 |

### 平台 `/api/admin/support/*`（`require_role("admin")`）

同上，外加 `shopId` 与 `status` 过滤；**不校验归属**，可介入任何会话。

### 站内信 `/api/notifications`（`CurrentUserDep`）

`GET /`（`unreadOnly`）、`GET /unread-count`（角标）、`POST /{id}/read`、
`POST /read-all`。

### 几条约定

- **归属不对一律 404，不是 403**（照 `aftersale/service.py`）：不区分"不存在"与
  "不是你的"，否则就是一个可以遍历探测会话号的口子。
- **没有 `/read` 端点**：读游标由「打开详情」和「发消息」推进 —— 少一个端点就少一处
  前端要记得调的地方。
- **发消息不要求 `Idempotency-Key`**：聊天消息的语义就是"又发了一条"，没有资金动作
  要护住；真重复了也只是多一条同样的话（与 `cart` 的加购同类）。该做的是前端在请求
  在飞时禁用发送按钮。
- **关闭后再发消息 = 重开同一条**（不是报错、也不新建）。这与"如果分页"无关：买家已经
  点进这条会话并打了字，重开是最诚实的解读；商家那边它自然重新出现在「待回复」里。
- 新增错误码只有 **1 个**：`SUPPORT_RATE_LIMITED`(429)（Redis 固定窗口计数）。
  长度/空值由 pydantic 兜成 `VALIDATION_ERROR`。

### 5.1 买家侧的四个入口

**开会话的逻辑只有一处** —— 客服页认这几个查询参数，进来自动开会话并跳到那一条。
各个入口只负责"把上下文放进 URL"，不各写一套开会话的代码。

| 入口 | 带什么 |
|---|---|
| 商品详情页 · 店铺块里的「联系客服」 | `shopId` + `subject=关于「<商品名>」` —— **带上商品名**，否则会话标题只会是「商品咨询」，商家看不出买家在问哪件 |
| 订单详情页 · 每个子单一行 | `shopId` + `orderMainNo` + `orderSubNo`，**按子单**——跨店母单的每个子单属于不同的店 |
| 售后详情页 · 操作条 | `shopId` + `refundNo`（+ `orderMainNo`） |
| 我的 ·「咨询与客服」 | 不带上下文 → 客服列表页；那里可以「联系平台客服」 |

★ 订单与售后**不传 `subject`**：后端已能按 `source` + 单号生成「关于订单 M…」，
再传一遍是冗余。商品页之所以要传，是因为它默认生成的那句（「商品咨询」）不含商品名。

★ 商品页的「联系客服」必须是**店铺块的兄弟节点**，不能放进那个 `<RouterLink>` 里：
链接里嵌按钮是无效 HTML，而且点一下会**同时触发进店跳转**。

★ 平台级会话（`shop_id = 0`）商家看不到，所以「联系平台客服」与上面三处是**两条不同的
链路**，不是同一个入口的两种叫法。

## 6. 手机号：客服台**只有打码号**

`GET /api/admin/users/{id}` 的注释写明了「后台刻意不提供解密查看」，
`phone_decrypt` 至今**没有任何生产调用点**。这一轮**不动这条边界**：
客服台显示的是 `account_service.list_user_labels` 给的**打码手机号** + 买家昵称。

真要放开（比如需要电话外呼），那是一个**要单独写文档的明文出口**，属于后续工作。

## 7. 角标：只有「待回复」，没有「站内信」

- 商城端：「消息」角标 = 站内信未读数。**客服回复必然产生一条站内信**，所以这一个
  角标就够了，「客服」入口上不再挂第二个 —— 两个角标说同一件事只会让用户困惑。
- 后台：「客服」角标 = **待回复会话数**（单独 count，不是数列表第一页）。站内信是
  发给**买家**的，商家/运营一条都不会有，挂站内信角标会永远显示 0。

## 8. 明确不做（并写明理由）

| 不做 | 理由 |
|---|---|
| 实时 IM / 在线状态 / 坐席分配 / 排队 | 与"两条游标"绑定：一旦多客服，未读就得变 N×M。见 §2.1 的升级条件 |
| 游客（未登录）开单 | 要手机号验证码，会把短信一起拖进来；一期由静态联系方式兜 |
| `phone_decrypt` 启用 | §6，不动这条隐私边界 |
| `inventory.redis_release` 的即时释放 | §4.3，动交易路径的 Redis 语义，单独一轮 |
| Redis Streams 投递层 | §4.2，有意偏离 docs/13 §4 / docs/14 §6 |
| 短信渠道 | docs/01 §2 原文即「第一期不接短信」 |
| 客服常用语 / 会话搜索 / 导出 | 一期的目标是"用户的问题能被看见、能被回答、能追溯"，这三件已由本文件覆盖 |

## 9. 与既有设计的一致性

- **模块边界**：`support` 只调 `account`（店铺名、买家标签）与 `notify`（站内信）；
  `notify` 只被 worker 与 `support` 调用；`trade` / `payment` / `aftersale` 与 `notify`
  之间**没有边**（走 outbox）。
- **幂等**：`support` 的"开会话"由**部分唯一索引**保证；`notify` 的写入由 `biz_key`
  保证；二者都不依赖请求头。
- **测试**：`tests/test_support_units.py`（纯函数 + 分派表覆盖检查）、
  `tests/test_support.py`（22 项集成）、`tests/test_notify.py`（投递循环、退避、
  弃置告警、重放幂等）。
