# 15 接口清单与错误码

## 1. 通用约定

### 1.1 请求

| 项 | 规范 |
|---|---|
| 协议 | HTTPS（强制） |
| 格式 | `Content-Type: application/json;charset=UTF-8` |
| 鉴权 | `Authorization: Bearer {JWT}`（网关校验，下游透传 `X-User-Id`） |
| 幂等 | 写接口必须带 `Idempotency-Key: {UUID}`（见 [10-idempotency](10-idempotency.md)） |
| 链路 | `X-Request-Id`（网关生成），全链路透传 |
| 版本 | URL 前缀 `/api/v1/` |
| 设备 | `X-Device-Id`、`X-App-Version`、`X-Platform`（ios/android/h5/mini） |

### 1.2 响应

```json
{
  "code": "0",
  "message": "success",
  "data": { ... },
  "requestId": "a1b2c3d4e5f6",
  "timestamp": 1759238200000
}
```

**`code` 用字符串**：避免客户端把 `"0"` 和 `0` 搞混，也支持 `"409001"` 这种带语义的格式。

### 1.3 金额与 ID

| 字段 | 类型 | 规范 |
|---|---|---|
| 金额 | `number`（整数，单位：分） | 前端展示时 `/100` 并格式化 |
| ID（订单号、SKU ID 等） | **`string`** | ★ 雪花 ID 超过 JS 的 2^53，用 number 会丢精度 |
| 时间 | `number`（毫秒时间戳） 或 `string`（ISO 8601） | 统一一种，推荐时间戳 |

**ID 用字符串的必要性**：

```javascript
// ❌ 雪花 ID 用 number
JSON.parse('{"id": 1234567890123456789}').id
// → 1234567890123456800   （精度丢失！）

// ✅ 用 string
JSON.parse('{"id": "1234567890123456789"}').id
// → "1234567890123456789"
```

后端配置（Jackson）：

```java
@Bean
public Jackson2ObjectMapperBuilderCustomizer longToString() {
    return b -> b.serializerByType(Long.class, ToStringSerializer.instance)
                 .serializerByType(Long.TYPE, ToStringSerializer.instance);
}
```

**但金额不要转字符串**（金额是分，最大 10^10 远小于 2^53，用 number 安全，且前端计算方便）。实现上需要**只对 ID 字段转字符串**，用自定义注解区分：

```java
public class OrderVO {
    @JsonSerialize(using = ToStringSerializer.class)
    private Long orderId;          // ID → string
    private Long payableAmount;    // 金额 → number
}
```

## 2. 接口清单

### 2.1 商品

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| GET | `/api/v1/spu/{spuId}` | SPU 详情（含 SKU 列表、规格） | - |
| GET | `/api/v1/sku/{skuId}` | SKU 详情 | - |
| POST | `/api/v1/sku/batch` | 批量查询 SKU（购物车/结算页用） | - |
| GET | `/api/v1/sku/{skuId}/stock` | 库存查询（展示用） | - |
| GET | `/api/v1/search` | 商品搜索 | - |

### 2.2 购物车

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| GET | `/api/v1/cart` | 购物车列表（按店铺分组） | - |
| POST | `/api/v1/cart/items` | 加购（`num` 累加） | ❌ 刻意不幂等（累加语义） |
| PUT | `/api/v1/cart/items/{skuId}` | 修改数量（`num` 设置为指定值） | ✅ 天然幂等（SET 语义） |
| DELETE | `/api/v1/cart/items` | 批量删除 | ✅ 天然幂等 |
| PUT | `/api/v1/cart/select` | 勾选/取消勾选 | ✅ 天然幂等 |

### 2.3 结算与下单

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| POST | `/api/v1/checkout/calc` | ★ 算价（返回明细 + priceToken） | - |
| POST | `/api/v1/checkout/coupons` | 可用券列表（含不可用原因） | - |
| POST | `/api/v1/order/create` | ★ 提交订单 | ✅ `Idempotency-Key` |
| GET | `/api/v1/order/{mainOrderNo}` | 订单详情（母单 + 子单） | - |
| GET | `/api/v1/order/list` | 订单列表 | - |
| POST | `/api/v1/order/{mainOrderNo}/cancel` | 取消订单 | ✅ 状态 CAS |
| POST | `/api/v1/order/sub/{subOrderNo}/receive` | 确认收货 | ✅ 状态 CAS |
| PUT | `/api/v1/order/sub/{subOrderNo}/address` | 修改地址（未发货） | ✅ `Idempotency-Key` |

#### POST /api/v1/checkout/calc

**请求**：

```json
{
  "items": [
    { "skuId": "1001", "num": 2 },
    { "skuId": "2001", "num": 1 }
  ],
  "couponCodeIds": ["880011", "880022"],
  "usePoints": true,
  "addressId": "12345"
}
```

**响应**：

```json
{
  "code": "0",
  "data": {
    "shops": [
      {
        "shopId": "100",
        "shopName": "Apple 官方旗舰店",
        "items": [
          {
            "skuId": "1001",
            "spuTitle": "iPhone 16 Pro",
            "specText": "暗夜黑;256G",
            "coverImage": "//cdn/1001.png",
            "num": 2,
            "unitPrice": 799900,
            "promoPrice": 749900,
            "itemDiscount": 100000,
            "shopDiscount": 20000,
            "platformDiscount": 3100,
            "pointDeduction": 1000,
            "payAmount": 1375800,
            "promoTags": ["限时直降"]
          }
        ],
        "shopDiscount": 20000,
        "shopDiscountName": "店铺满 1000 减 200",
        "freight": 0,
        "subtotal": 1375800
      }
    ],
    "summary": {
      "totalAmount": 2499700,
      "itemDiscount": 100000,
      "shopDiscount": 20000,
      "platformDiscount": 5000,
      "pointDeduction": 1500,
      "pointUsed": 1500,
      "freight": 1600,
      "payableAmount": 2374800
    },
    "coupons": {
      "selected": [
        { "codeId": "880011", "name": "平台满 200 减 50", "deduction": 5000, "level": "PLATFORM" }
      ],
      "unavailable": [
        { "codeId": "880022", "name": "店铺券 9 折", "reason": "STACK_CONFLICT", "reasonText": "与店铺满减活动互斥" }
      ]
    },
    "points": {
      "balance": 5000,
      "maxUsable": 1500,
      "used": 1500,
      "deduction": 1500,
      "rule": "100 积分抵 1 元，最多抵扣订单金额的 50%"
    },
    "freightDetail": [
      { "warehouseId": "1", "warehouseName": "上海仓", "weight": 1400,
        "rule": "首重 1kg ¥10，续重 0.5kg ¥3", "fee": 1600 }
    ],
    "priceToken": "eyJ1aWQiOiI4ODAwMSIsI...",
    "tokenExpireAt": 1759240000000
  }
}
```

#### POST /api/v1/order/create

**请求**：

```http
POST /api/v1/order/create HTTP/1.1
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
Idempotency-Key: 7f3a9b2c-4d1e-4a8b-9c3d-2e1f0a9b8c7d
Content-Type: application/json

{
  "items": [
    { "skuId": "1001", "num": 2 },
    { "skuId": "2001", "num": 1 }
  ],
  "couponCodeIds": ["880011"],
  "usePoints": true,
  "addressId": "12345",
  "priceToken": "eyJ1aWQiOiI4ODAwMSIsI...",
  "buyerRemark": "请尽快发货",
  "source": "CART"
}
```

**成功响应**：

```json
{
  "code": "0",
  "data": {
    "orderMainNo": "M2026093003847192637",
    "payableAmount": 2374800,
    "payDeadline": 1759240000000,
    "subOrders": [
      { "orderSubNo": "M2026093003847192637-1", "shopId": "100", "payableAmount": 1375800 },
      { "orderSubNo": "M2026093003847192637-2", "shopId": "200", "payableAmount": 999000 }
    ],
    "needPay": true
  }
}
```

**`needPay = false`** 表示 0 元订单（全额抵扣），前端直接跳成功页。

**价格变化响应**（409）：见 [11-price-consistency §4.4](11-price-consistency.md)。

### 2.4 优惠券

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| GET | `/api/v1/coupon/templates` | 可领取的券列表 | - |
| POST | `/api/v1/coupon/receive` | ★ 领券 | ✅ `Idempotency-Key` + Lua |
| GET | `/api/v1/coupon/mine` | 我的券（按状态筛选） | - |
| POST | `/api/v1/coupon/exchange` | 兑换码兑换 | ✅ `Idempotency-Key` + 码唯一 |

#### POST /api/v1/coupon/receive

```http
POST /api/v1/coupon/receive
Idempotency-Key: 9a8b7c6d-...

{ "templateId": "5001", "channel": "HOME_BANNER" }
```

**响应**：

```json
// 成功
{ "code": "0", "data": { "codeId": "880033", "name": "满200减30", "validEnd": 1761830400000 } }

// 已抢光
{ "code": "410001", "message": "优惠券已被抢光" }

// 超出限领
{ "code": "410002", "message": "每人限领 1 张，您已领取" }

// 幂等命中（返回首次结果，code = 0）
{ "code": "0", "data": { "codeId": "880033", ... } }
```

### 2.5 支付

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| POST | `/api/v1/pay/prepay` | ★ 发起支付（返回渠道参数） | ✅ `uk_main_order` |
| GET | `/api/v1/pay/{payNo}/status` | 查询支付状态（前端轮询） | - |
| POST | `/pay/notify/{channel}` | ★ 渠道回调（仅渠道 IP 可访问） | ✅ `uk_channel_trade` |

#### POST /api/v1/pay/prepay

```json
// 请求
{ "orderMainNo": "M2026093003847192637", "channel": "WECHAT", "tradeType": "APP" }

// 响应
{
  "code": "0",
  "data": {
    "payNo": "P2026093012345678",
    "channel": "WECHAT",
    "payParams": {
      "appid": "wx...",
      "partnerid": "...",
      "prepayid": "wx...",
      "package": "Sign=WXPay",
      "noncestr": "...",
      "timestamp": "...",
      "sign": "..."
    },
    "expireAt": 1759240000000
  }
}
```

**支付回调接口的安全要求**：

- 不走网关 JWT 鉴权（渠道不会带 JWT）
- 在网关层配置 **IP 白名单**（仅渠道的回调 IP 段）
- 必须验签（见 [09-payment §4.2](09-payment.md)）
- 不返回业务错误详情（防止信息泄露），只返回渠道要求的应答报文

### 2.6 售后

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| POST | `/api/v1/aftersale/apply/check` | 售后资格预检（可退数量、金额） | - |
| POST | `/api/v1/aftersale/apply` | ★ 申请售后 | ✅ `Idempotency-Key` + CAS |
| GET | `/api/v1/aftersale/{refundNo}` | 售后详情 | - |
| POST | `/api/v1/aftersale/{refundNo}/return` | 填写退货物流 | ✅ 状态 CAS |
| POST | `/api/v1/aftersale/{refundNo}/revoke` | 撤销申请 | ✅ 状态 CAS |
| POST | `/api/v1/aftersale/{refundNo}/intervene` | 申请平台介入 | ✅ 状态 CAS |

#### POST /api/v1/aftersale/apply/check

**用于前端展示"可退多少钱"**，申请前预检：

```json
// 请求
{ "orderSubNo": "M2026093003847192637-1", "items": [{ "orderItemId": "70001", "num": 1 }] }

// 响应
{
  "code": "0",
  "data": {
    "refundable": true,
    "maxRefundAmount": 687900,
    "breakdown": {
      "itemAmount": 749900,
      "itemDiscountShare": -50000,
      "couponShare": -10000,
      "pointShare": -2000,
      "freightRefund": 0,
      "freightNote": "部分退货不退运费"
    },
    "refundPoints": 200,
    "couponReturn": false,
    "couponNote": "部分退款，优惠券不退回",
    "types": ["REFUND_ONLY", "RETURN_REFUND"],
    "deadline": 1759843000000
  }
}
```

**`breakdown` 字段让用户清楚知道"为什么退的比原价少"**，这能大幅减少客服咨询。

### 2.7 评价

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| GET | `/api/v1/review/pending` | 待评价的订单项 | - |
| POST | `/api/v1/review/eligibility` | 评价资格检查 | - |
| POST | `/api/v1/review` | ★ 提交评价 | ✅ `uk_user_sku_item` |
| POST | `/api/v1/review/{reviewId}/follow-up` | 追评 | ✅ `uk_parent` |
| GET | `/api/v1/spu/{spuId}/reviews` | 商品评价列表（游标分页） | - |
| GET | `/api/v1/spu/{spuId}/review-stats` | 评价统计 | - |

### 2.8 商家端（节选）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/merchant/orders` | 商家订单列表（走 ES） |
| POST | `/api/v1/merchant/orders/{subNo}/ship` | 发货 |
| POST | `/api/v1/merchant/aftersale/{refundNo}/approve` | 同意售后 |
| POST | `/api/v1/merchant/aftersale/{refundNo}/reject` | 拒绝售后 |
| POST | `/api/v1/merchant/aftersale/{refundNo}/receive` | 确认收到退货 |
| POST | `/api/v1/merchant/aftersale/{refundNo}/quality` | ★ 提交质检结果（触发库存回补） |
| POST | `/api/v1/merchant/reviews/{reviewId}/reply` | 回复评价 |

**商家端接口的权限校验**：每个接口必须校验**资源归属**（这个子单/售后单属于当前商家的店铺），不能只校验"是商家"。

```java
// ★ 越权检查（最常见的安全漏洞）
OrderSub sub = subMapper.selectByNo(subNo);
if (!sub.getShopId().equals(MerchantContext.getShopId())) {
    throw new ForbiddenException("无权操作该订单");
}
```

## 3. 错误码

### 3.1 编码规则

```
6 位数字：{HTTP 类别 3 位}{业务序号 3 位}

400xxx  参数错误
401xxx  未认证
403xxx  无权限
404xxx  资源不存在
409xxx  冲突（状态、价格、幂等）
410xxx  资源已耗尽（库存、券）
422xxx  业务规则不满足
429xxx  限流
500xxx  系统错误
503xxx  服务不可用（降级）
```

### 3.2 错误码表

| code | HTTP | 含义 | 前端处理 |
|---|---|---|---|
| `0` | 200 | 成功 | - |
| **通用** | | | |
| `400001` | 400 | 参数错误 | 提示具体字段 |
| `400002` | 400 | 缺少 Idempotency-Key | 前端 Bug，上报 |
| `401001` | 401 | 未登录 / Token 过期 | 跳登录 |
| `403001` | 403 | 无权操作该资源 | 提示 |
| `404001` | 404 | 资源不存在 | 提示 |
| `429001` | 429 | 请求过于频繁 | 提示稍后再试 |
| `500001` | 500 | 系统错误 | 提示"系统繁忙"，**不展示技术细节** |
| `503001` | 503 | 服务降级中 | 提示"活动火爆，请稍后" |
| **幂等** | | | |
| `409101` | 409 | 请求处理中 | 等待 2s 后查询结果 |
| `409102` | 409 | 重复提交 | 引导查看已有订单 |
| **价格一致性** | | | |
| `409001` | 409 | 价格已变化 | ★ 弹窗展示差异，用户确认后用新 token 重提 |
| `409002` | 409 | priceToken 已过期 | 刷新结算页 |
| `400010` | 400 | priceToken 无效（验签失败） | 刷新结算页（可能被篡改，上报） |
| **库存** | | | |
| `410101` | 410 | 库存不足 | 展示缺货商品，可调整数量 |
| `410102` | 410 | 商品已售罄 | 提示 |
| `410103` | 410 | 秒杀已结束 | 提示 |
| `422101` | 422 | 超出限购数量 | 提示限购数 |
| **排队** | | | |
| `202101` | 202 | 排队中 | 展示排位，轮询结果 |
| `422102` | 422 | 已参与过该活动 | 提示 |
| `429102` | 429 | 排队人数已满 | 提示稍后再试 |
| **优惠券** | | | |
| `410001` | 410 | 券已被抢光 | 提示 |
| `422001` | 422 | 超出每人限领 | 提示 |
| `422002` | 422 | 券不可用（已过期） | 刷新券列表 |
| `422003` | 422 | 券不满足使用门槛 | 提示"还差 ¥xx" |
| `422004` | 422 | 券已被其他订单占用 | 刷新 |
| `422005` | 422 | 券与其他优惠互斥 | 提示 |
| `422006` | 422 | 活动未开始 | 展示倒计时 |
| `422007` | 422 | 活动已结束 | 提示 |
| **订单** | | | |
| `422201` | 422 | 订单状态不允许此操作 | 刷新订单 |
| `422202` | 422 | 订单已超时关闭 | 引导重新下单 |
| `422203` | 422 | 订单已支付 | 跳订单详情 |
| `422204` | 422 | 已发货不能取消 | 引导申请售后 |
| `422205` | 422 | 该地区不支持配送 | 更换地址 |
| `422206` | 422 | 商品已下架 | 移除商品 |
| **支付** | | | |
| `422301` | 422 | 支付单已关闭 | 重新下单 |
| `422302` | 422 | 支付金额异常 | ★ 上报（可能是攻击） |
| `503301` | 503 | 支付渠道维护中 | 换渠道 |
| **售后** | | | |
| `422401` | 422 | 已超过售后期限 | 提示 |
| `422402` | 422 | 退货数量超过可退数量 | 提示可退数 |
| `422403` | 422 | 退款金额超过实付 | 前端 Bug，上报 |
| `422404` | 422 | 已有进行中的售后 | 跳转售后详情 |
| `422405` | 422 | 该商品不支持七天无理由 | 提示 |
| **评价** | | | |
| `422501` | 422 | 未购买不能评价 | 提示 |
| `422502` | 422 | 确认收货后才能评价 | 提示 |
| `422503` | 422 | 该商品已评价 | ★ 提示（唯一索引拦截） |
| `422504` | 422 | 评价期限已过 | 提示 |
| `422505` | 422 | 已追评 | 提示 |
| `422506` | 422 | 已退款商品不能评价 | 提示 |

### 3.3 错误响应的安全原则

| 原则 | 说明 |
|---|---|
| **不暴露堆栈** | 500 错误只返回 `"系统繁忙"` + `requestId`，堆栈只写日志 |
| **不暴露内部结构** | 不返回 SQL 错误、表名、字段名 |
| **不暴露资源存在性** | 越权访问他人订单时，返回 `404`（不存在）而非 `403`（无权限），防止遍历探测 |
| **限制错误信息的精确度** | 登录失败统一返回"账号或密码错误"，不区分"账号不存在"和"密码错误" |
| **`requestId` 必返** | 用户反馈问题时，客服用 `requestId` 快速定位日志 |

**第 3 条的实现**：

```java
// 查询订单详情
OrderMain main = mainMapper.selectByNo(mainOrderNo);
if (main == null || !main.getUserId().equals(currentUserId)) {
    // ★ 两种情况返回同一个错误，不让攻击者区分"订单不存在"和"不是你的订单"
    throw new NotFoundException("订单不存在");
}
```

## 4. 接口安全基线

| 项 | 要求 |
|---|---|
| 鉴权 | 除公开接口（商品详情、搜索）外，全部需要 JWT |
| 资源归属 | 所有带 ID 的接口必须校验资源属于当前用户/商家 |
| 限流 | 网关层：用户 10 QPS，IP 50 QPS；敏感接口单独配置 |
| 输入校验 | 所有参数 `@Valid` 校验：数量 `1 ≤ num ≤ 200`，ID 格式，字符串长度 |
| SQL 注入 | 全部使用参数化查询（MyBatis `#{}`，禁用 `${}`） |
| XSS | 评价内容、备注等用户输入，**存储时原样存，输出时转义**（或前端用 `textContent` 渲染） |
| 敏感数据 | 手机号脱敏（`138****8888`）、地址部分脱敏；日志中不打印完整手机号/身份证 |
| CSRF | API 使用 JWT（非 Cookie），天然免疫 CSRF |
| 支付回调 | IP 白名单 + 验签 + 金额校验 |
| 文件上传 | 评价图片：限制类型（jpg/png/webp）、大小（≤5MB）、数量（≤9）；上传到 OSS，服务端不落盘 |
| 重放攻击 | 下单、支付等接口：`Idempotency-Key` + 时间戳校验（±5 分钟） |

**`num` 的输入校验不能省**：

```java
public class CartItemRequest {
    @NotNull
    private String skuId;

    @NotNull
    @Min(1)          // ★ 防止 num = 0 或负数（负数加购 = 减少库存？）
    @Max(200)        // ★ 防止 num = 999999999（整数溢出）
    private Integer num;
}
```

`num = -1` 如果没有校验，在某些逻辑里可能导致"下单后库存增加"（`available -= -1`）。这类漏洞在真实系统中多次出现过。
