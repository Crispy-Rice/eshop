# 09 支付：异步回调与对账补偿

> 第一期**只接入模拟支付渠道**（`MockChannel`），用来走通"下单 → 支付 → 回调 → 查单 → 对账 → 退款"的完整链路。真实渠道（微信支付、支付宝）二期接入时只需新增渠道适配器，链路其余部分不变。

## 1. 支付链路全景

```
用户点击"去支付"
   │
   ▼
① 创建支付单（payment）  ─── 幂等：同一母单只有一个有效支付单
   │
   ▼
② 调用渠道统一下单（第一期：MockChannel）
   │  返回：模拟收银台 URL
   ▼
③ 前端跳转收银台
   │
   ▼
④ 用户在收银台点"支付成功 / 支付失败"
   │
   ├──► ⑤ 渠道异步回调我方服务器（主路径，秒级）
   │
   └──► ⑥ 前端返回后主动查询（辅助，防回调丢失）
              │
              ▼
        ⑦ 定时对账任务（兜底，分钟级/日级）
```

**三条路径都必须能推动订单状态**，且都幂等。这是"支付掉单"问题的最小代价解法。模拟渠道刻意支持"延迟回调""重复回调""不回调"三种模式（§3.2），用来在开发和测试环境演练这三条路径。

## 2. 支付单模型

```sql
CREATE TABLE payment.payment (
  id                  BIGINT       PRIMARY KEY,
  pay_no              VARCHAR(32)  NOT NULL,          -- 支付单号（我们生成，传给渠道作为商户订单号）
  order_main_no       VARCHAR(32)  NOT NULL,          -- 母单号
  user_id             BIGINT       NOT NULL,
  channel             SMALLINT     NOT NULL,          -- 0模拟 1微信 2支付宝
  channel_app_id      VARCHAR(64),
  channel_merchant_id VARCHAR(64),
  amount              BIGINT       NOT NULL CHECK (amount > 0), -- 应付金额（分）
  paid_amount         BIGINT       NOT NULL DEFAULT 0,          -- 实付金额（分）
  refunded_amount     BIGINT       NOT NULL DEFAULT 0,          -- 已退金额（分）
  currency            VARCHAR(8)   NOT NULL DEFAULT 'CNY',
  status              SMALLINT     NOT NULL DEFAULT 0,  -- 0待支付 1支付中 2支付成功 3支付失败 4已关闭 5已全额退款
  -- 渠道返回
  out_trade_no        VARCHAR(64),                    -- 渠道交易号
  cashier_url         VARCHAR(512),                   -- 收银台地址（模拟渠道）/ prepay_id（真实渠道）
  channel_response    JSONB,                          -- 渠道返回原文（审计用）
  -- 回调
  notify_time         TIMESTAMPTZ(3),                 -- 首次收到成功回调的时间
  notify_count        INT          NOT NULL DEFAULT 0, -- 回调次数（含重复回调）
  -- 时间
  create_time         TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  expire_time         TIMESTAMPTZ(3) NOT NULL,        -- 支付单过期（= 订单支付截止）
  pay_time            TIMESTAMPTZ(3),
  close_time          TIMESTAMPTZ(3),
  updated_at          TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  version             INT          NOT NULL DEFAULT 0,
  CONSTRAINT uk_payment_pay_no UNIQUE (pay_no),
  CONSTRAINT uk_payment_channel_trade UNIQUE (channel, out_trade_no), -- ★ 防同一渠道交易号重复入账
  CONSTRAINT ck_payment_refund CHECK (refunded_amount <= paid_amount)
);
-- ★ 一个母单同时只能有一个"有效"支付单（PG 部分唯一索引）
CREATE UNIQUE INDEX uk_payment_main_active ON payment.payment (order_main_no)
  WHERE status IN (0, 1, 2, 5);
CREATE INDEX idx_payment_paying   ON payment.payment (create_time) WHERE status = 1;  -- 查单扫描
CREATE INDEX idx_payment_pay_time ON payment.payment (pay_time);                      -- 日对账
COMMENT ON TABLE payment.payment IS '支付单';
```

**两个唯一约束是关键防线**：

- `uk_payment_main_active`：防止同一订单同时存在多个有效支付单（用户连点"去支付"）。
- `uk_payment_channel_trade`：防止同一个渠道交易号入账两次（回调重复、或对账和回调同时处理）。`out_trade_no` 为 `NULL` 的行（尚未拿到渠道交易号）互不冲突，PG 的唯一约束天然允许多个 `NULL`。

**换渠道重新支付**：PostgreSQL 支持部分唯一索引，所以原方案中"不允许换渠道重付"的限制可以去掉——旧支付单置为"已关闭"（4）或"支付失败"（3）后就不再占用唯一索引，可以为同一母单新建支付单。关闭旧支付单前必须先向渠道关单（§3），防止两笔都付成功。

## 3. 渠道对接抽象

```python
# app/modules/payment/channels/base.py
class PaymentChannel(Protocol):
    channel_type: ChannelType

    async def prepay(self, req: PrepayRequest) -> PrepayResult:
        """统一下单，返回前端拉起支付所需的信息（收银台 URL / prepay_id）。"""

    async def query(self, pay_no: str) -> ChannelPayResult:
        """主动查单（查单补偿 / 对账用）。"""

    async def close(self, pay_no: str) -> None:
        """关闭渠道订单。"""

    async def refund(self, req: ChannelRefundRequest) -> ChannelRefundResult:
        """申请退款（异步，结果以退款回调/查询为准）。"""

    async def query_refund(self, refund_no: str) -> ChannelRefundResult: ...

    async def download_bill(self, biz_date: date) -> list[ChannelBill]:
        """下载对账单。"""

    def parse_notify(self, headers: Mapping[str, str], body: bytes) -> NotifyResult:
        """解析并验签回调，验签失败抛 InvalidSignatureError。"""

    def notify_ack(self, success: bool) -> Response:
        """回调应答报文（必须按渠道要求的格式返回）。"""
```

渠道实现用 `httpx.AsyncClient` 调用外部接口（禁止使用同步的 `requests`），并设置明确超时（连接 3s、读取 5s）。

**统一金额单位**：所有渠道接口层统一用**分**（`int`）。微信支付用分；支付宝接口用元字符串（`"12.34"`），在支付宝适配器内部用 `Decimal` 转换，接口层不暴露。

### 3.1 模拟渠道 MockChannel

第一期的唯一渠道实现，**只在配置 `PAYMENT_MOCK_ENABLED=true` 时注册**，生产环境接入真实渠道后关闭。

```python
# app/modules/payment/channels/mock.py
class MockChannel:
    """模拟支付渠道：数据存在 payment.mock_channel_trade 表，回调用 HMAC-SHA256 签名。"""
    channel_type = ChannelType.MOCK

    async def prepay(self, req: PrepayRequest) -> PrepayResult:
        await self.repo.upsert_trade(req.pay_no, req.amount, status="NOTPAY")
        return PrepayResult(cashier_url=f"/mock-cashier?payNo={req.pay_no}")

    def parse_notify(self, headers: Mapping[str, str], body: bytes) -> NotifyResult:
        expected = hmac.new(settings.mock_pay_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get("X-Mock-Signature", "")):
            raise InvalidSignatureError()
        data = MockNotifyBody.model_validate_json(body)
        return NotifyResult(pay_no=data.pay_no, out_trade_no=data.trade_no,
                            paid_amount=data.amount, success=data.status == "SUCCESS",
                            pay_time=data.pay_time, raw_body=body.decode())
```

模拟收银台是商城前端的一个页面（`/mock-cashier`），点击按钮后调用 `POST /api/mock-channel/pay`（只在 mock 模式下注册的路由），由后端模拟渠道：更新模拟交易记录 → 计算签名 → 向我方回调地址发起 HTTP 回调。

### 3.2 模拟渠道的故障注入

收银台页面提供以下选项，用来验证补偿链路：

| 选项 | 行为 | 验证的路径 |
|---|---|---|
| 支付成功（正常） | 立即回调一次 | ⑤ 回调主路径 |
| 支付成功（重复回调） | 连续回调 3 次 | 回调幂等 |
| 支付成功（延迟 60s 回调） | ARQ 延迟任务 60s 后回调 | ⑥ 前端查单先于回调到达 |
| 支付成功（不回调） | 只更新模拟交易记录 | ⑦ 查单扫描 / 对账补单 |
| 支付成功（金额篡改） | 回调金额 = 应付 − 1 分 | 金额校验拦截 |
| 支付失败 | 回调失败状态 | 失败处理 |

## 4. 异步回调的处理（核心）

### 4.1 回调接口

```python
# app/modules/payment/router.py
@router.post("/api/pay/notify/{channel}", include_in_schema=False)
async def pay_notify(channel: ChannelType, request: Request) -> Response:
    ch = channel_registry.get(channel)
    body = await request.body()
    try:
        # ① 验签（必须！否则可被伪造回调刷单）
        result = ch.parse_notify(request.headers, body)
    except InvalidSignatureError:
        logger.warning("支付回调验签失败", channel=channel, ip=request.client.host)
        return ch.notify_ack(False)

    try:
        # ② 落库去重 + 投递异步处理，立即应答
        await pay_service.accept_notify(ch.channel_type, result)
        return ch.notify_ack(True)
    except Exception:
        logger.exception("支付回调处理异常", pay_no=result.pay_no)
        return ch.notify_ack(False)          # ★ 返回失败，让渠道重试
```

回调接口**不走 JWT 鉴权**（渠道不会带我们的 token），安全性完全依赖验签 + 金额校验（§10）。它也不走 `Idempotency-Key` 依赖，幂等由 §4.2 的唯一约束保证。

### 4.2 回调处理的三个铁律

**铁律 1：验签**

无验签 = 任何人 `curl` 一下就能把订单标记为已支付。真实渠道**必须用渠道官方 SDK 或文档给出的算法验签**，不要自己发明；模拟渠道用 HMAC-SHA256 + `hmac.compare_digest`（防时序攻击）。

**铁律 2：立刻应答，业务异步**

渠道回调有超时（微信、支付宝约 5s）。业务处理（改订单、扣库存、扣积分、发通知）可能变慢。做法：

```sql
CREATE TABLE payment.pay_notify_log (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  channel       SMALLINT     NOT NULL,
  notify_type   SMALLINT     NOT NULL,          -- 1支付 2退款
  out_trade_no  VARCHAR(64)  NOT NULL,          -- 支付回调为渠道交易号，退款回调为渠道退款单号
  pay_no        VARCHAR(32)  NOT NULL,
  paid_amount   BIGINT       NOT NULL,
  success       BOOLEAN      NOT NULL,
  source        SMALLINT     NOT NULL DEFAULT 1, -- 1回调 2查单 3对账
  raw_body      TEXT         NOT NULL,
  status        SMALLINT     NOT NULL DEFAULT 0, -- 0待处理 1处理中 2已处理 3处理失败
  error_msg     VARCHAR(512),
  retry_count   INT          NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT uk_notify_trade UNIQUE (channel, notify_type, out_trade_no)  -- ★ 回调去重
);
CREATE INDEX idx_notify_pending ON payment.pay_notify_log (created_at) WHERE status IN (0, 3);
```

> 这张表**不做分区**：分区表的唯一约束必须包含分区键，会破坏 `(channel, notify_type, out_trade_no)` 的全局去重。保留期 3 个月，由 ARQ cron 分批删除已处理记录。

```python
async def accept_notify(self, channel: ChannelType, result: NotifyResult) -> None:
    async with self.session_factory() as session, session.begin():
        # ① 快速落库（回调原文），一次 INSERT；重复回调被唯一约束吸收
        log_id = await session.scalar(
            pg_insert(PayNotifyLog)
            .values(channel=channel, notify_type=1, out_trade_no=result.out_trade_no,
                    pay_no=result.pay_no, paid_amount=result.paid_amount,
                    success=result.success, raw_body=result.raw_body)
            .on_conflict_do_nothing(constraint="uk_notify_trade")
            .returning(PayNotifyLog.id)
        )
        await session.execute(
            update(Payment).where(Payment.pay_no == result.pay_no)
            .values(notify_count=Payment.notify_count + 1)
        )
    if log_id is None:
        logger.info("重复回调，忽略", out_trade_no=result.out_trade_no)
        return                                   # ★ 返回成功，渠道不再重试

    # ② 事务提交后投递异步处理（job_id 去重）
    await self.arq.enqueue_job("handle_pay_notify", log_id, _job_id=f"notify:{log_id}")
```

ARQ 任务可能因 Redis 故障而丢失，所以另有一个 cron 每分钟扫描 `status IN (0, 3)` 且超过 1 分钟未处理的记录重新处理（命中部分索引 `idx_notify_pending`）。

**铁律 3：处理必须幂等**

任务可能被重复执行（ARQ 重试 + cron 补偿）。核心去重：

```python
# app/worker/tasks.py
async def handle_pay_notify(ctx: dict, log_id: int) -> None:
    async with ctx["session_factory"]() as session, session.begin():
        # ① CAS 抢占处理权：只有待处理/失败的记录才能进入处理中
        claimed = await session.execute(
            update(PayNotifyLog)
            .where(PayNotifyLog.id == log_id, PayNotifyLog.status.in_((0, 3)))
            .values(status=1, updated_at=func.now())
        )
    if claimed.rowcount == 0:
        return                                   # 已处理或正在被处理

    try:
        async with ctx["session_factory"]() as session, session.begin():
            notify = await session.get(PayNotifyLog, log_id)
            await pay_service.apply_pay_result(session, notify)
            notify.status = 2
    except Exception as e:
        async with ctx["session_factory"]() as session, session.begin():
            await session.execute(
                update(PayNotifyLog).where(PayNotifyLog.id == log_id)
                .values(status=3, error_msg=str(e)[:512], retry_count=PayNotifyLog.retry_count + 1)
            )
        raise                                    # 让 ARQ 按退避策略重试
```

> 处理中（`status=1`）的记录如果因 worker 崩溃卡住，cron 补偿任务会把 `status=1 AND updated_at < now() - 5min` 的记录重置为 3 后重新处理。

### 4.3 回调的业务处理

模块化单体的优势在这里最明显：**支付单、订单、子单、库存、积分、优惠券的状态推进全部在一个本地事务里完成**，要么全部成功，要么全部回滚重试，不存在"支付单成功了但库存没实扣"的中间态。

```python
async def apply_pay_result(self, session: AsyncSession, notify: PayNotifyLog) -> None:
    """支付结果的唯一处理入口：回调、查单、对账三条路径都调用它。"""
    pay = await session.scalar(
        select(Payment).where(Payment.pay_no == notify.pay_no).with_for_update()
    )
    if pay is None:
        raise BizError(ErrorCode.PAY_NOT_FOUND)

    if not notify.success:
        await self.mark_failed(session, pay, notify)
        return

    # ① 金额校验（★ 最重要的校验！防止渠道金额被篡改）
    if notify.paid_amount != pay.amount:
        logger.error("★ 支付金额不一致", pay_no=pay.pay_no, expected=pay.amount, actual=notify.paid_amount)
        await alert(session, "PAY_AMOUNT_MISMATCH", pay.pay_no)
        raise AmountMismatchError(pay.pay_no)    # 不推进任何状态，挂起人工处理

    # ② 状态机守卫：已成功则幂等返回；只有待支付/支付中/已关闭才往下走
    if pay.status == PayStatus.SUCCESS:
        return                                   # ★ 幂等核心
    if pay.status not in (PayStatus.WAIT_PAY, PayStatus.PAYING, PayStatus.CLOSED):
        logger.warning("支付单状态异常，忽略回调", pay_no=pay.pay_no, status=pay.status)
        return

    # ③ 更新支付单（钱确实收到了，无论订单状态如何都要记成功）
    pay.status = PayStatus.SUCCESS
    pay.out_trade_no = notify.out_trade_no
    pay.paid_amount = notify.paid_amount
    pay.pay_time = pay.notify_time = func.now()

    # ④ 更新母单（CAS：只有待付款才能转已支付）
    paid = await order_service.mark_main_paid(session, pay.order_main_no, notify.paid_amount)
    if not paid:
        # 订单状态不是待付款 → 超时关单后又收到支付成功
        await self.handle_late_payment(session, pay)   # ★ 见 §7
        return

    main = await order_service.get_main(session, pay.order_main_no)
    subs = await order_service.list_subs(session, pay.order_main_no)

    # ⑤ 子单状态推进（统一状态机入口）
    for sub in subs:
        await trade_service.transit(session, sub.order_sub_no, OrderEvent.PAY_SUCCESS, TransitContext.system())

    # ⑥ 库存：预占转实扣   ⑦ 积分：冻结转实扣   ⑧ 优惠券：锁定转已使用
    await inventory_service.confirm(session, build_items(subs), biz_key=f"PAY:{pay.order_main_no}")
    await points_service.confirm_frozen(session, main.user_id, pay.order_main_no, main.point_used)
    await coupon_service.use_by_order(session, pay.order_main_no)

    # ⑨ 异步副作用写 outbox：站内信、结算入账、销量统计
    await outbox.add(session, topic="trade.order_paid", biz_key=f"PAID:{pay.order_main_no}",
                     payload={"orderMainNo": pay.order_main_no, "paidAmount": notify.paid_amount})
```

**第 ④ 步返回 `False` 是最容易漏掉的边界**（见 §7）。

## 5. 主动查单补偿

**为什么需要**：回调可能永远收不到（网络问题、渠道故障、我们服务重启、回调地址配错）。

**方式 1：前端返回时触发**（用户视角最快的补偿）

```python
@router.get("/api/payments/{pay_no}")
async def query_payment(pay_no: str, user: CurrentUser, session: DbSession) -> PaymentOut:
    pay = await pay_repo.get_owned(session, pay_no, user.id)   # ★ 校验归属，防止查别人的支付单
    if pay.status in (PayStatus.WAIT_PAY, PayStatus.PAYING) \
            and (utcnow() - pay.create_time) >= timedelta(seconds=5):
        # 距创建超过 5 秒才真正查渠道（避免用户刚点就查，渠道还没结果）
        await pay_service.sync_from_channel(pay_no)            # 结果走 apply_pay_result，幂等
        pay = await pay_repo.get_owned(session, pay_no, user.id)
    return PaymentOut.model_validate(pay)
```

前端在收银台返回后每 2 秒轮询一次，最多 15 次；仍未成功则提示"支付结果确认中，可稍后在订单列表查看"。

**方式 2：定时扫描**（兜底）

```python
# ARQ cron：每 30 秒（second={0, 30}）
async def scan_paying_payments(ctx: dict) -> None:
    async with ctx["session_factory"]() as session:
        stale = (await session.scalars(
            select(Payment.pay_no)
            .where(Payment.status == PayStatus.PAYING,
                   Payment.create_time < func.now() - text("interval '30 seconds'"))
            .limit(200)
        )).all()
    for pay_no in stale:
        try:
            await pay_service.sync_from_channel(pay_no)
        except Exception:
            logger.exception("查单失败", pay_no=pay_no)
```

**关键设计：查单结果和回调结果走同一个处理函数**。`sync_from_channel` 把查单结果构造成一条 `source=2` 的 `PayNotifyLog`（插入时同样受 `uk_notify_trade` 去重），然后调用 `apply_pay_result`。无论哪条路径先到，结果一致，而且天然幂等。

```python
async def sync_from_channel(self, pay_no: str) -> None:
    pay = await self.repo.get(pay_no)
    r = await channel_registry.get(pay.channel).query(pay_no)
    if r.state is ChannelPayState.PAYING:
        return                                           # 未出结果，等下次
    # 构造"伪回调"，复用全部校验与幂等（source = 2 查单）
    await self.accept_notify(pay.channel, r.to_notify_result(source=NotifySource.QUERY))
```

## 6. 对账补偿（最终的兜底）

### 6.1 三层对账

| 层级 | 频率 | 内容 | 动作 |
|---|---|---|---|
| **实时查单** | 30 秒 | 本地"支付中"的支付单，主动查渠道 | 修正状态（§5） |
| **准实时对账** | 5 分钟 | 近 1 小时创建的待支付/支付中支付单，逐笔查单 | 补单 |
| **日对账** | 每日 02:10 | 拉取前一日渠道账单，与本地逐笔比对 | 生成差异单，自动/人工处理 |

模拟渠道的 `download_bill` 直接读 `payment.mock_channel_trade` 表生成账单，日对账逻辑与真实渠道完全一致。

### 6.2 日对账的实现

```python
# ARQ cron：每日 02:10
async def daily_reconcile(ctx: dict, biz_date: date | None = None) -> None:
    biz_date = biz_date or (datetime.now(SHANGHAI) - timedelta(days=1)).date()   # 账单日按东八区

    for channel in channel_registry.enabled():
        # ① 拉取渠道账单
        bills = {b.out_trade_no: b for b in await channel.download_bill(biz_date)}
        # ② 拉取本地该日期成功支付的支付单
        locals_ = {p.out_trade_no: p for p in await pay_repo.list_paid_on(channel.channel_type, biz_date)}

        diffs: list[ReconcileDiff] = []
        # 3.1 本地有、渠道无 → "本地多单"
        diffs += [ReconcileDiff.local_only(p) for no, p in locals_.items() if no not in bills]
        # 3.2 渠道有、本地无 → "本地少单"（掉单，最严重）
        diffs += [ReconcileDiff.channel_only(b) for no, b in bills.items() if no not in locals_]
        # 3.3 两边都有但金额不一致 → "金额不符"
        diffs += [ReconcileDiff.amount_mismatch(p, bills[no])
                  for no, p in locals_.items() if no in bills and p.paid_amount != bills[no].amount]

        # ④ 处理差异（落库幂等 + 自动修复）
        await reconcile_service.process(channel.channel_type, biz_date, diffs)
        logger.info("日对账完成", channel=channel.channel_type, biz_date=biz_date, diffs=len(diffs))
```

> 时区：数据库统一存 UTC（`TIMESTAMPTZ`），"账单日"是业务概念，按 `Asia/Shanghai` 切分。`list_paid_on` 中的条件写成 `pay_time >= :day_start_utc AND pay_time < :day_end_utc`，边界由东八区零点换算而来，不要用 `DATE(pay_time)`（既会按 UTC 切错日，又用不上索引）。

### 6.3 差异类型的处理策略

| 差异类型 | 含义 | 处理 |
|---|---|---|
| **本地有，渠道无** | 我方记了成功，渠道说没这笔。可能是伪造回调（已验签，概率极低）或渠道账单不全 | 不自动冲正，**挂起人工核查**，同时标记用户风险 |
| **渠道有，本地无**（掉单） | 用户付了钱，我方不知道 | ★ **自动补单**：构造 `source=3` 的伪回调走 `accept_notify` |
| **金额不符** | 用户实付 ≠ 订单应付 | 挂起，人工处理；同时通知用户 |
| **本地失败，渠道成功** | 我方标了失败但渠道成功了 | 自动补单（`apply_pay_result` 允许从失败/关闭进入 §7 的晚付处理） |
| **本地成功，渠道退款** | 渠道侧有退款我们不知道 | 生成差异单，人工核实后补录退款 |

### 6.4 对账的幂等

对账任务**必须可重跑**（同一天跑两次结果一致）。差异记录用唯一约束 upsert：

```sql
CREATE TABLE payment.reconcile_diff (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  biz_date       DATE        NOT NULL,
  channel        SMALLINT    NOT NULL,
  out_trade_no   VARCHAR(64) NOT NULL,
  pay_no         VARCHAR(32),
  diff_type      SMALLINT    NOT NULL,   -- 1本地多单 2本地少单 3金额不符 4本地失败渠道成功 5渠道退款
  local_amount   BIGINT,
  channel_amount BIGINT,
  status         SMALLINT    NOT NULL DEFAULT 0,  -- 0待处理 1已自动修复 2人工已处理 3已忽略
  handle_remark  VARCHAR(255),
  created_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  handled_at     TIMESTAMPTZ(3),
  CONSTRAINT uk_reconcile_diff UNIQUE (biz_date, channel, diff_type, out_trade_no)  -- ★ 重跑幂等
);
CREATE INDEX idx_reconcile_diff_pending ON payment.reconcile_diff (biz_date) WHERE status = 0;
COMMENT ON TABLE payment.reconcile_diff IS '对账差异记录';
```

```sql
INSERT INTO payment.reconcile_diff (biz_date, channel, out_trade_no, pay_no, diff_type, local_amount, channel_amount)
VALUES (:biz_date, :channel, :out_trade_no, :pay_no, :diff_type, :local_amount, :channel_amount)
ON CONFLICT (biz_date, channel, diff_type, out_trade_no) DO NOTHING;
```

重跑时已存在的差异不会重复插入；已处理（`status <> 0`）的记录保留原处理结果。上一轮存在、本轮已消失的待处理差异（例如补单后已对平），由 `process` 将其标记为 1 已自动修复。

## 7. 最危险的场景：超时关闭后收到支付成功

**时序**：

```
T+0       用户下单，支付截止 T+30min
T+29m     用户打开收银台，开始支付
T+30m     我方超时关单（ARQ 延迟任务触发）
T+30m30s  用户实际支付成功
T+31m     渠道回调到达
```

**此时订单已关闭，库存已释放，券已退回。**

处理方案（按优先级）：

| 方案 | 做法 | 评价 |
|---|---|---|
| **A. 自动退款** | 识别到"已关单但支付成功"，立即发起原路退款 | ✅ **采用**。钱退回来了，损失最小 |
| B. 自动重开订单 | 重新预占库存，若成功则恢复订单 | ❌ 库存可能已卖给别人，且用户可能不要了 |
| C. 挂起人工 | 生成差异单等待客服 | ❌ 用户体验差，客服成本高 |

**方案 A 的实现**（在 `apply_pay_result` 的同一事务内）：

```python
async def handle_late_payment(self, session: AsyncSession, pay: Payment) -> None:
    main = await order_service.get_main(session, pay.order_main_no, for_update=True)
    if main.status != SubOrderStatus.CLOSED:
        logger.info("订单已被其他路径处理，忽略", pay_no=pay.pay_no)
        return

    logger.warning("★ 关单后收到支付成功，发起自动退款", pay_no=pay.pay_no)
    # ① 支付单已在调用方标记成功（钱确实收了）
    # ② 标记订单"已关单已付款"，禁止任何发货动作（pay_status = 1 且 status = 50 的组合）
    await order_service.mark_closed_but_paid(session, pay.order_main_no, pay.paid_amount)
    # ③ 创建全额资金退款单（幂等键 LATE:{pay_no}），由退款任务异步调用渠道
    await refund_service.create_payment_refund(
        session, pay, amount=pay.paid_amount, biz_no=f"LATE:{pay.pay_no}",
        reason="订单超时关闭后支付成功，自动退款")
    # ④ 通知用户 + ⑤ 告警（这是异常，要监控频率）
    await outbox.add(session, topic="notify.late_pay_refund", biz_key=f"LATE:{pay.pay_no}",
                     payload={"userId": main.user_id, "orderMainNo": main.order_main_no})
    await alert(session, "LATE_PAYMENT", pay.pay_no)
```

**降低发生率的措施**：

1. **渠道过期时间更短**：渠道侧订单过期时间设为 25 分钟，我方订单 30 分钟。渠道先关，就不会出现我们关了、渠道还能付的情况。模拟收银台同样校验这个过期时间。
2. **关单前二次确认**：超时关单前，若存在"支付中"的支付单，先主动查一次渠道。

```python
async def close_order_if_unpaid(ctx: dict, order_main_no: str) -> None:
    pay = await pay_repo.get_active_by_main(order_main_no)
    if pay is not None and pay.status == PayStatus.PAYING:
        r = await channel_registry.get(pay.channel).query(pay.pay_no)
        if r.state is ChannelPayState.SUCCESS:
            await pay_service.accept_notify(pay.channel, r.to_notify_result(source=NotifySource.QUERY))
            return                                     # 支付成功，不关单
        if r.state is ChannelPayState.PAYING:
            # 渠道还在处理，2 分钟后再试（job_id 带时间戳，避免与原任务冲突）
            await ctx["redis"].enqueue_job("close_order_if_unpaid", order_main_no,
                                           _job_id=f"close:{order_main_no}:{int(time.time())}",
                                           _defer_by=timedelta(minutes=2))
            return
        await channel_registry.get(pay.channel).close(pay.pay_no)   # 先关渠道单，再关本地
    async with ctx["session_factory"]() as session, session.begin():
        await order_service.close_order(session, order_main_no, CloseReason.TIMEOUT)
```

## 8. 退款（资金侧）

**资金退款单与售后单分离**：

- `aftersale.refund_order`（售后单）：业务视角，用户/商家/平台的处理流程
- `payment.payment_refund`（资金退款单）：技术视角，一次渠道退款调用

一个售后单对应一次资金退款；"晚付自动退款"没有售后单，`refund_biz_no` 用 `LATE:{pay_no}`。

```sql
CREATE TABLE payment.payment_refund (
  id                BIGINT       PRIMARY KEY,
  refund_no         VARCHAR(32)  NOT NULL,      -- 资金退款单号（传给渠道）
  refund_biz_no     VARCHAR(40)  NOT NULL,      -- 售后单号 或 LATE:{pay_no}
  pay_no            VARCHAR(32)  NOT NULL,      -- 原支付单号
  order_main_no     VARCHAR(32)  NOT NULL,
  user_id           BIGINT       NOT NULL,
  amount            BIGINT       NOT NULL CHECK (amount > 0), -- 退款金额（分）
  channel           SMALLINT     NOT NULL,
  status            SMALLINT     NOT NULL DEFAULT 0,  -- 0待退款 1退款中 2退款成功 3退款失败 4已关闭
  channel_refund_no VARCHAR(64),                -- 渠道退款单号
  fail_reason       VARCHAR(255),
  retry_count       INT          NOT NULL DEFAULT 0,
  next_retry_at     TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  create_time       TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  success_time      TIMESTAMPTZ(3),
  updated_at        TIMESTAMPTZ(3) NOT NULL DEFAULT now(),
  CONSTRAINT uk_payment_refund_no UNIQUE (refund_no),
  CONSTRAINT uk_payment_refund_biz UNIQUE (refund_biz_no)   -- ★ 一个售后单只能有一次资金退款
);
CREATE INDEX idx_payment_refund_retry ON payment.payment_refund (next_retry_at) WHERE status IN (0, 3);
CREATE INDEX idx_payment_refund_pay   ON payment.payment_refund (pay_no);
COMMENT ON TABLE payment.payment_refund IS '资金退款单';
```

创建资金退款单时，同一事务内对支付单做条件更新，**从数据库层面保证不会超退**：

```sql
UPDATE payment.payment
SET refunded_amount = refunded_amount + :amount, updated_at = now()
WHERE pay_no = :pay_no AND status = 2
  AND refunded_amount + :amount <= paid_amount;
-- rowcount = 0 → 超退，拒绝；另有 CHECK (refunded_amount <= paid_amount) 兜底
```

**退款的执行与重试**：

```python
# ARQ cron：每分钟；也会在创建退款单后立即投递一次 execute_refund
async def retry_refunds(ctx: dict) -> None:
    async with ctx["session_factory"]() as session:
        due = (await session.scalars(
            select(PaymentRefund.refund_no)
            .where(PaymentRefund.status.in_((0, 3)),
                   PaymentRefund.next_retry_at <= func.now(),
                   PaymentRefund.retry_count < 10)
            .limit(200)
        )).all()
    for refund_no in due:
        try:
            await refund_service.execute(refund_no)          # 调渠道退款，状态 → 1 退款中
        except Exception as e:
            # 退避：第 n 次重试间隔 = min(2^n 分钟, 6 小时)
            await refund_service.mark_retry(refund_no, error=str(e))
```

`retry_count >= 10` 的退款单进入人工处理列表并告警。

**退款也可能掉单**：渠道退款是异步的，结果以退款回调或退款查询为准。同样用"回调 + 查单 + 对账"三件套，退款回调写入 `pay_notify_log`（`notify_type=2`），处理函数在同一事务内推进资金退款单和售后单（见 [08 §6](08-aftersale.md) 事务 B）。

## 9. 幂等的完整清单（支付链路）

| 环节 | 幂等键 | 实现 |
|---|---|---|
| 创建支付单 | `order_main_no` | 部分唯一索引 `uk_payment_main_active` + `Idempotency-Key` |
| 渠道统一下单 | `pay_no` | 渠道侧用商户订单号幂等；本地有有效支付单则直接复用 |
| 收到回调 | `(channel, notify_type, out_trade_no)` | `uk_notify_trade` + `ON CONFLICT DO NOTHING` |
| 处理回调 | `pay_notify_log.id` | 状态 CAS（0/3 → 1） |
| 更新支付单 | `pay_no` + `status` | `SELECT ... FOR UPDATE` + 状态守卫 |
| 更新订单 | `order_main_no` + `status` | CAS |
| 库存实扣 | `PAY:{mainOrderNo}` | `inventory.stock_biz_key` 主键 |
| 积分实扣 | `ORDER_USE:{mainOrderNo}` | `account.points_biz_key` 主键 |
| 用券 | `USE:{mainOrderNo}:{codeId}` | 券状态 CAS（2 → 3） |
| 发起退款 | `refund_biz_no` | `uk_payment_refund_biz` |
| 渠道退款 | `refund_no` | 渠道侧幂等 + 本地状态 CAS |

**资金链路上每一环都有幂等键**，这不是过度设计——支付回调天然会重复（渠道的重试机制就是重发），没有幂等必然出事故。模拟渠道的"重复回调"选项就是为了持续验证这一点。

## 10. 安全清单

| 项 | 要求 |
|---|---|
| 回调验签 | **必须**。真实渠道用官方算法/SDK；模拟渠道用 HMAC-SHA256 + `compare_digest` |
| 金额校验 | 回调金额必须与支付单金额一致，不一致拒绝并告警 |
| 订单归属校验 | 回调的商户订单号必须存在；前端查询支付单必须校验 `user_id` |
| 幂等重复回调 | 返回成功（让渠道停止重试），但不再处理业务 |
| 回调频率限制 | Nginx 对 `/api/pay/notify/` 单独 `limit_req`（每 IP 20 r/s） |
| 模拟渠道隔离 | `/api/mock-channel/*` 路由与 `MockChannel` 只在 `PAYMENT_MOCK_ENABLED=true` 时注册；接入真实渠道上线前必须关闭 |
| 密钥管理 | 渠道密钥、`MOCK_PAY_SECRET` 放服务器 `.env` 文件（权限 600），不入代码库，见 [16](16-deployment.md) |
| 日志脱敏 | 日志不打印完整密钥、回调原文中的敏感字段 |
| 来源 IP 限制 | 接入真实渠道后，在 Nginx 上为回调路径配置渠道 IP 白名单 |
| 敏感操作二次校验 | 大额退款（> 5000 元）需运营在后台人工审核 |

## 11. 监控指标

第一期指标通过 `/metrics`（`prometheus-client`）暴露，告警先以"对账任务写 `ops.alert` + 后台首页提示"实现，二期接 Prometheus + Grafana + 企业微信告警。

| 指标 | 告警阈值 |
|---|---|
| 支付成功率 | < 95% 告警 |
| 回调处理失败率 | > 1% 告警 |
| 对账差异数（掉单） | > 0 立即告警（每条都查） |
| 对账差异数（金额不符） | > 0 立即告警 |
| 晚付退款笔数 | > 5 笔/小时告警 |
| 退款失败重试次数 | 单笔 > 5 次告警 |
| 回调到订单更新延迟 | P99 > 5s 告警 |
