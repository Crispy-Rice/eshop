# 15 接口清单与错误码

## 1. 通用约定

### 1.1 请求

| 项 | 规范 |
|---|---|
| 协议 | 第一期 HTTP（IP 直连，见 [16](16-deployment.md)）；绑定域名后强制 HTTPS |
| 格式 | `Content-Type: application/json` |
| 鉴权 | `Authorization: Bearer {access_token}`（JWT，FastAPI 依赖 `get_current_user` 校验） |
| 幂等 | 关键写接口必须带 `Idempotency-Key: {UUID}`（见 [10-idempotency](10-idempotency.md)） |
| 链路 | `X-Request-Id`：Nginx 生成（`$request_id`）并透传，应用写入日志与响应 |
| 路径前缀 | 买家 `/api/...`，商家 `/api/merchant/...`，平台运营 `/api/admin/...`；第一期不加版本号，不兼容变更时引入 `/api/v2/` |
| 接口文档 | FastAPI 自动生成的 OpenAPI：开发环境开放 `/docs`，生产环境关闭（`docs_url=None`） |

**鉴权方案**（第一期账号密码登录，不接短信）：

| 项 | 方案 |
|---|---|
| 密码存储 | `argon2-cffi` 哈希（Argon2id），不可逆 |
| access token | JWT（HS256，`PyJWT`），有效期 30 分钟，载荷只放 `sub`（用户 ID）、`role`、`shop_id`、`exp` |
| refresh token | 随机串，有效期 14 天，**哈希后**存 PG，可吊销；前端存 `localStorage` |
| 角色 | `buyer` / `merchant` / `admin` / `finance`。运营端用 `Depends(require_role("admin", "finance"))`；**商家端不校验角色，而是校验店铺归属**（`CurrentShopIdDep`）—— token 里的 `role` 是签发时的快照，开完店不会立即更新 |
| 登录防爆破 | 同账号 5 次失败锁定 15 分钟，同 IP 每分钟 20 次（Redis 计数） |

> refresh token 存 `localStorage` 会受 XSS 影响。第一期通过严格的输出转义与 CSP 降低风险（§4）；启用 HTTPS 后改为 `HttpOnly; Secure; SameSite=Strict` Cookie 存放 refresh token。

### 1.2 响应

```json
{
  "code": "OK",
  "message": "success",
  "data": { },
  "requestId": "a1b2c3d4e5f6"
}
```

```python
# app/core/response.py
class ApiResponse(CamelModel, Generic[T]):
    code: str = "OK"
    message: str = "success"
    data: T | None = None
    request_id: str = Field(default_factory=lambda: request_id_ctx.get())

    @classmethod
    def ok(cls, data: T) -> "ApiResponse[T]":
        return cls(data=data)
```

**`code` 用语义化字符串**（`PRICE_CHANGED`、`STOCK_INSUFFICIENT`）：前端按字符串分支处理，日志里一眼可读，也不会与 HTTP 状态码混淆。HTTP 状态码表达大类（4xx/5xx），`code` 表达具体原因。

### 1.3 金额、ID 与时间

| 字段 | JSON 类型 | 规范 |
|---|---|---|
| 金额 | `number`（整数，单位：分） | 前端展示时用 `formatYuan()` 格式化（[11 §7](11-price-consistency.md)） |
| ID（用户、SKU、订单 ID 等雪花 ID） | **`string`** | ★ 雪花 ID 超过 JS 的 2^53，用 number 会丢精度 |
| 业务单号（订单号、支付单号） | `string` | 本来就是字符串 |
| 时间 | `string`（ISO 8601，带时区，如 `2026-10-01T08:00:00+08:00`） | 全站统一 |

**ID 用字符串的必要性**：

```js
// ❌ 雪花 ID 用 number
JSON.parse('{"id": 1234567890123456789}').id
// → 1234567890123456800   （精度丢失！）

// ✅ 用 string
JSON.parse('{"id": "1234567890123456789"}').id
// → "1234567890123456789"
```

后端用 Pydantic 的注解类型统一处理，**只对 ID 字段转字符串**，金额保持整数：

```python
# app/core/schemas.py
from pydantic import BaseModel, ConfigDict, PlainSerializer, BeforeValidator
from pydantic.alias_generators import to_camel

# 入参接受字符串或整数，出参序列化为字符串
SnowflakeId = Annotated[
    int,
    BeforeValidator(lambda v: int(v) if isinstance(v, str) and v.isdigit() else v),
    PlainSerializer(str, return_type=str, when_used="json"),
]
# 金额：严格整数、非负
Fen = Annotated[int, Field(ge=0, strict=True)]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class OrderOut(CamelModel):
    order_id: SnowflakeId          # → "1234567890123456789"
    payable_amount: Fen            # → 2374800
```

> 路由函数返回 Pydantic 模型时，FastAPI 默认按别名（camelCase）序列化（`response_model_by_alias=True`）。

## 2. 接口清单

### 2.0 账号

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| POST | `/api/auth/register` | 注册（手机号 + 密码，第一期不验证手机号真实性） | 公开 |
| POST | `/api/auth/login` | 登录，返回 access/refresh token | 公开 |
| POST | `/api/auth/refresh` | 刷新 access token | refresh token |
| POST | `/api/auth/logout` | 吊销 refresh token | 登录 |
| GET | `/api/me` | 当前用户信息、积分余额 | 登录 |
| GET/POST/PUT/DELETE | `/api/me/addresses[/{id}]` | 收货地址 | 登录 |

### 2.1 商品

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| GET | `/api/spus/{spuId}` | SPU 详情（含 SKU 列表、规格） | - |
| POST | `/api/skus/batch` | 批量查询 SKU（购物车/结算页用） | - |
| GET | `/api/skus/{skuId}/stock` | 库存查询（展示用，档位化） | - |
| GET | `/api/search?kw=&categoryId=&priceFrom=&priceTo=&sort=&cursor=` | 商品搜索（[02 §7](02-domain-model.md)） | - |
| GET | `/api/categories` | 类目树 | - |
| POST | `/api/files/images?biz=reviews\|aftersale\|products` | 上传图片，返回相对路径与可直用的 url | 登录，单张 ≤ 5MB |

### 2.2 购物车

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| GET | `/api/cart` | 购物车列表（按店铺分组） | - |
| POST | `/api/cart/items` | 加购（`num` 累加） | ❌ 刻意不幂等（累加语义） |
| PUT | `/api/cart/items/{skuId}` | 修改数量（`num` 设置为指定值） | ✅ 天然幂等（SET 语义） |
| DELETE | `/api/cart/items` | 批量删除（body：`skuIds`） | ✅ 天然幂等 |
| PUT | `/api/cart/select` | 勾选/取消勾选 | ✅ 天然幂等 |

> **实现补充**（在 docs/02 §4 定了"失效项不自动删、由用户主动清"之后新增的两个接口）：
>
> | 方法 | 路径 | 说明 |
> |---|---|---|
> | GET | `/api/cart/count` | 顶栏角标。单独一个轻接口，不必为了显示数字拉整个购物车 |
> | DELETE | `/api/cart/invalid` | 清除失效商品。判定用**实时状态**而不只看"商品查不到"——已下架的商品同样是买了也没用的 |
>
> 另外 `GET /api/cart` 的分组规则：**失效与已下架**的商品放进 `invalidItems`（不参与合计），
> **无货**的商品留在原分组（它是正常商品，补货后还能买，用户也需要看到它在哪个店铺）；
> 金额一律只算**有效且已勾选**的项。

### 2.3 结算与下单

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| POST | `/api/checkout/calc` | ★ 算价（返回明细 + priceToken） | - |
| POST | `/api/checkout/coupons` | 可用券列表（含不可用原因） | - |
| POST | `/api/orders` | ★ 提交订单 | ✅ `Idempotency-Key` + `uk_order_main_request` |
| GET | `/api/orders/{orderMainNo}` | 订单详情（母单 + 子单） | - |
| GET | `/api/orders?status=&cursor=` | 订单列表（游标分页） | - |
| POST | `/api/orders/{orderMainNo}/cancel` | 取消订单 | ✅ 状态 CAS |
| POST | `/api/order-subs/{orderSubNo}/receive` | 确认收货 | ✅ 状态 CAS |
| PUT | `/api/order-subs/{orderSubNo}/address` | 修改地址（未发货） | ✅ `Idempotency-Key` |

#### POST /api/checkout/calc

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
  "code": "OK",
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
            "coverImage": "/media/products/1001.webp",
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
      { "warehouseId": "1", "warehouseName": "上海仓", "weightG": 1400,
        "rule": "首重 1kg ¥10，续重 0.5kg ¥3", "fee": 1600 }
    ],
    "priceToken": "eyJ1aWQiOiI4ODAwMSIsI...",
    "tokenExpireAt": "2026-10-01T08:30:00+08:00"
  }
}
```

#### POST /api/orders

**请求**：

```http
POST /api/orders HTTP/1.1
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
  "code": "OK",
  "data": {
    "orderMainNo": "M2026100138471926377",
    "payableAmount": 2374800,
    "payDeadline": "2026-10-01T08:30:00+08:00",
    "subOrders": [
      { "orderSubNo": "M2026100138471926377-1", "shopId": "100", "payableAmount": 1375800 },
      { "orderSubNo": "M2026100138471926377-2", "shopId": "200", "payableAmount": 999000 }
    ],
    "needPay": true
  }
}
```

**`needPay = false`** 表示 0 元订单（全额抵扣），前端直接跳成功页。

**价格变化响应**（409 `PRICE_CHANGED`）：见 [11-price-consistency §4.4](11-price-consistency.md)。

### 2.4 优惠券

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| GET | `/api/coupon-templates` | 可领取的券列表 | - |
| POST | `/api/coupons/receive` | ★ 领券 | ✅ `Idempotency-Key` + Lua |
| GET | `/api/coupons/mine?status=` | 我的券（按状态筛选） | - |
| POST | `/api/coupons/exchange` | 兑换码兑换 | ✅ `Idempotency-Key` + 码唯一 |

#### POST /api/coupons/receive

```http
POST /api/coupons/receive
Idempotency-Key: 9a8b7c6d-...

{ "templateId": "5001", "channel": "HOME_BANNER" }
```

**响应**：

```json
// 成功
{ "code": "OK", "data": { "codeId": "880033", "name": "满200减30", "validEnd": "2026-10-31T23:59:59+08:00" } }

// 已抢光（HTTP 410）
{ "code": "COUPON_SOLD_OUT", "message": "优惠券已被抢光" }

// 超出限领（HTTP 422）
{ "code": "COUPON_LIMIT_EXCEEDED", "message": "每人限领 1 张，您已领取" }

// 幂等命中（返回首次结果）
{ "code": "OK", "data": { "codeId": "880033", "name": "满200减30", "validEnd": "..." } }
```

### 2.5 支付

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| POST | `/api/payments` | ★ 发起支付（同一母单重复调用返回同一张支付单） | ✅ `uk_payment_order_main_no` |
| GET | `/api/payments/{payNo}` | 查询支付状态 | - |
| POST | `/api/payments/{payNo}/mock-callback` | ★ **模拟渠道回调**（仅 `PAYMENT_MOCK_ENABLED=true` 可用，不需要登录） | ✅ 支付单 CAS `status = 待支付` |
| POST | `/api/pay/notify/{channel}` | 真实渠道回调（不走 JWT，验签） | 二期，随真实渠道适配器一起做 |

**关于模拟回调的形态**：[09 §3.1](09-payment.md) 描述的是"前端 → `POST /api/mock-channel/pay`
→ 后端**再发起一次 HTTP 回调**到自己"的两跳模型，为的是把"渠道服务器回调我方"这一跳也演出来。
第一期简化成**一跳**：前端点「确认支付」直接打 `mock-callback`，它承担"渠道通知我方"的语义，
所做的三件事（写渠道交易 → CAS 推进支付单 → 调 `trade.mark_paid`）与真实回调完全一致，
**回调幂等性也一并被验证到了**。二期接真实渠道时补上验签与两跳即可，业务侧调用不变。

#### POST /api/payments

```json
// 请求
{ "orderMainNo": "M2026100138471926377", "channel": "MOCK" }

// 响应（模拟渠道）
{
  "code": "OK",
  "data": {
    "payNo": "P2026100112345678",
    "channel": "MOCK",
    "cashierUrl": "/mock-cashier?payNo=P2026100112345678",
    "expireAt": "2026-10-01T08:25:00+08:00"
  }
}
```

接入微信/支付宝后，`data` 中的 `cashierUrl` 替换为对应渠道参数（PC 端为扫码支付的二维码链接 `codeUrl` 或支付宝 PC 收银台表单），前端按 `channel` 分支处理。

**支付回调接口的安全要求**：

- 不走 JWT 鉴权（渠道不会带我们的 token），不走 `Idempotency-Key` 依赖
- 必须验签（见 [09-payment §4.2](09-payment.md)）
- 不返回业务错误详情（防止信息泄露），只返回渠道要求的应答报文
- 接入真实渠道后，在 Nginx 为该路径配置渠道回调 IP 白名单

### 2.6 售后

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| POST | `/api/aftersales/check` | 售后资格预检（可退数量、金额） | - |
| POST | `/api/aftersales` | ★ 申请售后 | ✅ `Idempotency-Key` + 订单项行锁 |
| GET | `/api/aftersales` | 我的售后列表（游标分页，可按状态筛） | - |
| GET | `/api/aftersales/{refundNo}` | 售后详情 | - |
| POST | `/api/aftersales/{refundNo}/return` | 填写退货物流 | ✅ 状态 CAS |
| POST | `/api/aftersales/{refundNo}/revoke` | 撤销申请 | ✅ 状态 CAS |
| POST | `/api/aftersales/{refundNo}/intervene` | 申请平台介入 | **本期未实现**（平台仲裁不在第一期范围） |

**本期范围**：只做**仅退款**与**退货退款**两种类型，商家 48 小时未审核**自动同意**
（而不是转平台介入）。状态 90（平台介入中）保留在枚举里但没有任何入边，
所以 `/intervene` 这个接口一期不提供。换货、补寄、积分返还同样不在本期。

#### POST /api/aftersales/check

**用于前端展示"可退多少钱"**，申请前预检：

```json
// 请求
{ "orderSubNo": "M2026100138471926377-1", "items": [{ "orderItemId": "70001", "num": 1 }] }

// 响应
{
  "code": "OK",
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
    "deadline": "2026-10-08T23:59:59+08:00"
  }
}
```

**`breakdown` 字段让用户清楚知道"为什么退的比原价少"**，这能大幅减少客服咨询。

### 2.7 评价

| 方法 | 路径 | 说明 | 幂等 |
|---|---|---|---|
| GET | `/api/reviews/pending` | 待评价的订单项 | - |
| POST | `/api/reviews/eligibility` | 评价资格检查 | - |
| POST | `/api/reviews` | ★ 提交评价 | ✅ `uk_review_order_item`（同一订单项只能一条首评） |
| POST | `/api/reviews/{reviewId}/follow-up` | 追评 | ✅ `uk_review_follow_up`（一条首评只能追一次） |
| GET | `/api/reviews/mine` | 我的评价（首评与追评都列出，游标分页） | - |
| GET | `/api/spus/{spuId}/reviews` | 商品评价列表。**公开接口**，`sort=latest\|recommend`、`filter=all\|good\|with_image`、游标分页 | - |
| GET | `/api/spus/{spuId}/review-stats` | 评价统计。**公开接口** | - |

**两条前端必须知道的约定**：

1. **`GET /api/spus/{spuId}/review-stats` 在商品没有任何已发布评价时，
   `avgScore` 与 `goodRate` 都是 `null`** —— 不要渲染成 5.0 分或 0 分，
   直接显示"暂无评价"（docs/12 §9）。
2. 评价列表返回的 `images` 是对象数组 `{path, url, thumbUrl}`。**列表页用
   `thumbUrl`**（200px 缩略图），详情查看用 `url`（长边 1280 的归一化图）。
   入库时提交的是上传接口返回的 `path`（相对路径，不带 `/media/` 前缀）。

### 2.8 商家端（节选，`role = merchant`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST/PUT | `/api/merchant/spus[/{spuId}]` | 商品发布与编辑（[02 §9](02-domain-model.md)） |
| POST | `/api/merchant/spus/{spuId}/submit` | 提交审核 |
| PUT | `/api/merchant/skus/{skuId}/stock` | 设置库存（需审批记录，[03 §11](03-inventory.md)） |
| GET/POST/PUT | `/api/merchant/freight/templates[/{id}]` | 运费模板（实际路径带 `freight/`，早期文档写成 `freight-templates` 是错的） |
| POST | `/api/merchant/freight/bind` | 把 SKU 绑到运费模板 |
| GET | `/api/merchant/freight/templates/{id}/binds` | ★ 该模板绑了哪些 SKU。带商品标题/规格/仓库名（后端拼好）。**只有读，没有解绑** |
| PUT | `/api/merchant/freight/templates/{id}/default` | 设为店铺默认模板（一店一条，设新的顶掉旧的）。未绑定模板的规格算运费时回落到它。**开店时系统已自动建了一条**，商家可改可换 |
| GET/POST | `/api/merchant/coupon-templates` | 店铺券（未实现） |
| GET | `/api/merchant/orders?status=&cursor=` | 商家订单列表（`idx_order_sub_shop` 索引） |
| POST | `/api/merchant/order-subs/{subNo}/ship` | 发货（填写快递公司与单号） |
| GET | `/api/merchant/aftersales?status=&pendingOnly=&cursor=` | 商家售后列表。**列表项是精简对象**：不含凭证图、退货物流、质检结果、时间线 |
| GET | `/api/merchant/aftersales/{refundNo}` | ★ 售后详情（完整对象：明细、凭证图、退货物流、质检、全部时间戳、`can*` 标志） |
| POST | `/api/merchant/aftersales/{refundNo}/approve` | 同意售后 |
| POST | `/api/merchant/aftersales/{refundNo}/reject` | 拒绝售后 |
| POST | `/api/merchant/aftersales/{refundNo}/receive` | 确认收到退货 |
| POST | `/api/merchant/aftersales/{refundNo}/quality` | ★ 提交质检结果（触发库存回补）。`images` 支持 ≤9 张留证图 |
| POST | `/api/merchant/reviews/{reviewId}/reply` | 回复评价（每条评价最多 3 次商家回复） |
| GET | `/api/merchant/reviews?status=&cursor=` | 本店评价列表（走 `idx_review_shop_status`） |

### 2.9 平台运营端（节选，`role = admin / finance`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/shops` | 券 / 活动的「指定店铺」目标。`keyword` 按店名搜；`ids`（逗号分隔）按 id 回看 —— 存下来的范围里只有 id。两个方向同一个入口，刻意不分页 |
| POST | `/api/admin/shops` | 店铺入驻与管理（未实现） |
| POST | `/api/admin/spus/{spuId}/audit` | 商品审核 |
| GET | `/api/admin/coupons/templates` | 券模板列表（`status` 过滤 + 游标分页）。★ 出参比券中心那份**多带发行量/已发量/适用范围/getType** |
| POST | `/api/admin/coupons/templates` | 新建券模板 |
| POST | `/api/admin/coupons/templates/{tplId}/void` | **作废券模板**（`status = 4`）。止住新的领取（`claim_template_quota` 要求 `status = 2`），**已发出去的券不受影响** |
| POST | `/api/admin/coupons/issue` | 客服补发。**只支持单个 `userId`**，单次 ≤100 张，不占活动额度 |
| GET | `/api/admin/promotions` | 促销活动列表（`status` 过滤 + 游标分页），带 `levelText`/`typeText`/`calcTypeText`/`statusText` |
| POST | `/api/admin/promotions` | 新建促销活动。响应只回 `{id, name}` |
| POST | `/api/admin/promotions/{id}/void` | **作废促销活动**（`status = 4`）。算价查询要求 `status = 2`，所以**当场失效**；历史订单读自己的快照，不受影响。重复作废 → 400 |
| GET | `/api/admin/aftersales?status=90` | 平台介入工单（未实现） |
| POST | `/api/admin/aftersales/{refundNo}/judge` | 平台裁决（未实现） |
| GET | `/api/admin/reviews/audit-queue` | 评价审核队列（**两个互不相交的视图**）：默认 = 待审核（机审命中高风险词，先审后发）；`secondAuditOnly=true` = 待抽检（机审放行、先发后审，即 `status=已发布 ∧ need_second_audit`）。**这个参数必须写 alias**，否则前端传 camelCase 会被静默忽略 |
| POST | `/api/admin/reviews/{reviewId}/audit` | 处置评价：APPROVE / REJECT / BLOCK / UNBLOCK。**会同步更新商品评分** |
| GET | `/api/admin/reconcile/diffs` | 对账差异（finance，未实现） |
| GET | `/api/admin/alerts` | 告警列表（对账异常、死信等，未实现） |
| PUT | `/api/admin/switches/{name}` | 降级开关（未实现） |

> 券与活动这两组 GET 是**后补的**：原先运营端三个接口全是 POST，
> 运营建完券模板/活动后**界面上再也看不到**，会以为提交失败。
> 路径刻意与同资源的 POST 一致（`/api/admin/coupons/templates`、
> `/api/admin/promotions`），没有照早期文档写成 `/api/admin/coupon-templates`。
>
> ★ **叠加规则（`promo_stack_rule`）没有任何写接口**，那 10 条默认规则是迁移里插的，
> 界面上配不了。

**商家端与运营端接口的权限校验**：除了角色，每个接口必须校验**资源归属**（这个子单/售后单属于当前商家的店铺），不能只校验"是商家"：

```python
# ★ 越权检查（最常见的安全漏洞）——封装成依赖，路由无法漏掉
async def owned_sub_order(order_sub_no: str, merchant: CurrentMerchant, session: DbSession) -> OrderSub:
    sub = await trade_repo.get_sub(session, order_sub_no)
    if sub is None or sub.shop_id != merchant.shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "订单不存在")       # 不区分"不存在"与"无权"
    return sub

@router.post("/api/merchant/order-subs/{order_sub_no}/ship")
async def ship(body: ShipRequest, sub: Annotated[OrderSub, Depends(owned_sub_order)], ...): ...
```

## 3. 错误码

### 3.1 编码规则

`code` 为大写下划线的语义字符串，定义在后端 `app/core/errors.py` 的 `ErrorCode` 枚举中，每个错误码绑定一个 HTTP 状态码与默认文案；前端 `src/api/errors.ts` 中维护同名常量（由 OpenAPI 生成或手工同步，CI 检查两边一致）。

```python
class ErrorCode(StrEnum):
    def __new__(cls, value: str, http_status: int, message: str):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.http_status = http_status
        obj.default_message = message
        return obj

    VALIDATION_ERROR = ("VALIDATION_ERROR", 400, "参数错误")
    PRICE_CHANGED = ("PRICE_CHANGED", 409, "商品信息发生变化，请确认后重新提交")
    # ...
```

| HTTP | 类别 |
|---|---|
| 400 | 参数错误 |
| 401 | 未认证 |
| 403 | 无权限（角色不符） |
| 404 | 资源不存在（含"无权访问他人资源"，见 §3.3） |
| 409 | 冲突（状态、价格、幂等） |
| 410 | 资源已耗尽（库存、券） |
| 422 | 业务规则不满足 |
| 429 | 限流 |
| 500 | 系统错误 |
| 503 | 服务不可用（降级） |

> FastAPI 默认对请求体校验失败返回 422。为与本表一致，全局注册 `RequestValidationError` 处理器，统一转换为 `400 VALIDATION_ERROR`，`data.errors` 中列出字段与原因；422 专用于业务规则不满足。

### 3.2 错误码表

| code | HTTP | 含义 | 前端处理 |
|---|---|---|---|
| `OK` | 200 | 成功 | - |
| **通用** | | | |
| `VALIDATION_ERROR` | 400 | 参数错误 | 提示具体字段 |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | 缺少 Idempotency-Key | 前端 Bug，上报 |
| `UNAUTHORIZED` | 401 | 未登录 / Token 过期 | 尝试 refresh，失败跳登录 |
| `LOGIN_FAILED` | 401 | 账号或密码错误 | 提示（不区分账号不存在/密码错误） |
| `ACCOUNT_LOCKED` | 423 | 登录失败次数过多，已锁定 | 提示剩余锁定时间 |
| `FORBIDDEN` | 403 | 角色无权访问 | 提示 |
| `NOT_FOUND` | 404 | 资源不存在 | 提示 |
| `RATE_LIMITED` | 429 | 请求过于频繁 | 提示稍后再试 |
| `INTERNAL_ERROR` | 500 | 系统错误 | 提示"系统繁忙"，展示 `requestId`，**不展示技术细节** |
| `SYSTEM_BUSY` | 503 | 服务降级中 | 提示"活动火爆，请稍后" |
| **幂等** | | | |
| `REQUEST_PROCESSING` | 409 | 请求处理中 | 等待 2s 后用同一个 key 重试 |
| **价格一致性** | | | |
| `PRICE_CHANGED` | 409 | 价格已变化 | ★ 弹窗展示差异，用户确认后用新 token 重提 |
| `PRICE_TOKEN_EXPIRED` | 409 | priceToken 已过期 | 刷新结算页 |
| `INVALID_PRICE_TOKEN` | 400 | priceToken 无效（验签失败） | 刷新结算页（可能被篡改，上报） |
| **库存** | | | |
| `STOCK_INSUFFICIENT` | 410 | 库存不足 | 展示缺货商品，可调整数量 |
| `STOCK_SOLD_OUT` | 410 | 商品已售罄 | 提示 |
| `SECKILL_ENDED` | 410 | 秒杀已结束 | 提示 |
| `PURCHASE_LIMIT_EXCEEDED` | 422 | 超出限购数量 | 提示限购数 |
| **排队** | | | |
| `SECKILL_QUEUED` | 202 | 排队中 | 展示排位，轮询结果 |
| `ALREADY_PURCHASED` | 422 | 已参与过该活动 | 提示 |
| `QUEUE_FULL` | 429 | 排队人数已满 | 提示稍后再试 |
| **优惠券** | | | |
| `COUPON_SOLD_OUT` | 410 | 券已被抢光 | 提示 |
| `COUPON_LIMIT_EXCEEDED` | 422 | 超出每人限领 | 提示 |
| `COUPON_EXPIRED` | 422 | 券已过期 | 刷新券列表 |
| `COUPON_THRESHOLD_NOT_MET` | 422 | 券不满足使用门槛 | 提示"还差 ¥xx" |
| `COUPON_LOCKED` | 422 | 券已被其他订单占用 | 刷新 |
| `COUPON_STACK_CONFLICT` | 422 | 券与其他优惠互斥 | 提示 |
| `ACTIVITY_NOT_STARTED` | 422 | 活动未开始 | 展示倒计时 |
| `ACTIVITY_ENDED` | 422 | 活动已结束 | 提示 |
| **订单** | | | |
| `ORDER_STATUS_INVALID` | 422 | 订单状态不允许此操作 | 刷新订单 |
| `ORDER_CLOSED` | 422 | 订单已超时关闭 | 引导重新下单 |
| `ORDER_ALREADY_PAID` | 422 | 订单已支付 | 跳订单详情 |
| `ORDER_ALREADY_SHIPPED` | 422 | 已发货不能取消 | 引导申请售后 |
| `NOT_DELIVERABLE` | 422 | 该地区不支持配送 | 更换地址 |
| `SKU_OFF_SHELF` | 422 | 商品已下架 | 移除商品 |
| **支付** | | | |
| `PAYMENT_CLOSED` | 422 | 支付单已关闭 | 重新发起支付 |
| `PAY_AMOUNT_MISMATCH` | 422 | 支付金额异常 | ★ 上报（可能是攻击） |
| `PAY_CHANNEL_UNAVAILABLE` | 503 | 支付渠道维护中 | 提示稍后再试 |
| **售后** | | | |
| `AFTERSALE_EXPIRED` | 422 | 已超过售后期限 | 提示 |
| `AFTERSALE_STATUS_INVALID` | 422 | 当前售后状态不允许该操作 | 刷新售后详情 |
| `REFUND_NUM_EXCEED` | 422 | 退货数量超过可退数量 | 提示可退数 |
| `REFUND_AMOUNT_EXCEED` | 422 | 退款金额超过实付 | 前端 Bug，上报 |
| `AFTERSALE_IN_PROGRESS` | 422 | 已有进行中的售后 | 跳转售后详情 |
| `NO_REASON_RETURN_UNSUPPORTED` | 422 | 该商品不支持七天无理由 | 提示 |
| **评价** | | | |
| `ORDER_ITEM_NOT_FOUND` | 404 | 订单不存在（含"不是你的订单"） | 提示 |
| `NOT_RECEIVED` | 422 | 确认收货后才能评价 | 提示 |
| `ORDER_NOT_FINISHED` | 422 | 订单完成后才能评价（待付款 / 未发货） | 提示 |
| `ALREADY_REVIEWED` | 422 | 该商品已评价 | ★ 提示（唯一索引拦截） |
| `REVIEW_EXPIRED` | 422 | 评价期限已过 | 提示 |
| `ALREADY_FOLLOWED_UP` | 422 | 已追评 | 提示 |
| `ITEM_REFUNDED` | 422 | 已退款商品不能评价 | 提示 |
| `IN_AFTERSALE` | 422 | 售后处理中，暂不能评价 | 提示 |
| `REPLY_LIMIT_EXCEEDED` | 422 | 该评价的回复次数已达上限 | 提示（每条最多 3 次） |
| `REVIEW_STATUS_INVALID` | 422 | 当前评价状态不允许该操作 | 刷新审核队列 |
| **文件上传** | | | |
| `INVALID_IMAGE` | 422 | 图片格式不支持或已损坏 | 提示重新选择 |
| `IMAGE_TOO_LARGE` | 413 | 图片体积超过限制 | 提示压缩后再传 |

### 3.3 错误响应的安全原则

| 原则 | 说明 |
|---|---|
| **不暴露堆栈** | 未捕获异常由全局处理器返回 `INTERNAL_ERROR` + `requestId`，堆栈只写日志；生产环境 `debug=False` |
| **不暴露内部结构** | 不返回 SQL 错误、表名、字段名（`IntegrityError` 等数据库异常一律视为 500） |
| **不暴露资源存在性** | 越权访问他人订单时，返回 `404 NOT_FOUND` 而非 `403`，防止遍历探测 |
| **限制错误信息的精确度** | 登录失败统一返回"账号或密码错误"，不区分"账号不存在"和"密码错误"；注册接口对"手机号已注册"也只返回模糊提示并限流 |
| **`requestId` 必返** | 用户反馈问题时，用 `requestId` 快速定位日志 |

**第 3 条的实现**：

```python
async def get_order_detail(session: AsyncSession, user_id: int, order_main_no: str) -> OrderDetail:
    main = await order_repo.get_main(session, order_main_no)
    if main is None or main.user_id != user_id:
        # ★ 两种情况返回同一个错误，不让攻击者区分"订单不存在"和"不是你的订单"
        raise BizError(ErrorCode.NOT_FOUND, "订单不存在")
    ...
```

## 4. 接口安全基线

| 项 | 要求 |
|---|---|
| 鉴权 | 除公开接口（注册、登录、商品详情、搜索、评价列表、支付回调）外，全部需要 JWT |
| 资源归属 | 所有带 ID 的接口必须校验资源属于当前用户/商家，封装为依赖（§2.9） |
| 限流 | Nginx：每 IP `limit_req` 30 r/s（突发 60）；应用：用户 10 QPS，登录/注册/领券单独配置 |
| 输入校验 | 所有入参用 Pydantic 模型声明约束：数量 `1 ≤ num ≤ 200`，ID 为正整数，字符串 `max_length`；金额字段 `strict=True` |
| SQL 注入 | 全部使用 SQLAlchemy 表达式或 `text()` 绑定参数；**禁止用 f-string / `%` 拼接 SQL**（CI 用 `ruff` 的 `S608` 规则检查） |
| XSS | 评价内容、备注等用户输入**原样存储，输出时转义**；Vue 模板默认转义，**禁止对用户内容使用 `v-html`**；Nginx 下发 `Content-Security-Policy: default-src 'self'; img-src 'self' data:; object-src 'none'` |
| 敏感数据 | 手机号脱敏（`138****8888`）、地址部分脱敏；日志中不打印完整手机号、密码、token |
| CSRF | access token 放在 `Authorization` 头（非 Cookie），不受 CSRF 影响；改用 Cookie 存 refresh token 时需加 `SameSite=Strict` |
| CORS | 前端与 API 同源（都经 Nginx），生产环境**不开启 CORS**；开发环境由 Vite 代理转发，同样不需要 |
| 支付回调 | 验签 + 金额校验；接入真实渠道后加 IP 白名单 |
| 文件上传 | 类型按内容判断（Pillow 重新编码）、大小 ≤ 5MB、数量 ≤ 9、服务端生成文件名、存储目录禁止执行（[12 §10](12-review.md)） |
| 未加密传输 | 第一期 HTTP 明文传输密码与 token，**仅适用于内测**；对公网正式开放前必须绑定域名并启用 HTTPS（[16 §6](16-deployment.md)） |

**`num` 的输入校验不能省**：

```python
class CartItemRequest(CamelModel):
    sku_id: SnowflakeId
    num: int = Field(ge=1, le=200, strict=True)   # ★ 防止 num = 0、负数、超大值
```

`num = -1` 如果没有校验，在某些逻辑里可能导致"下单后库存增加"（`available -= -1`）。这类漏洞在真实系统中多次出现过。表上的 `CHECK (num > 0)`（[02 §5](02-domain-model.md)）是数据库层的第二道防线。
