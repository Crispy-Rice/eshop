# 电商平台设计方案（v2.0）

一个**多商家（平台 + 店铺）**的电商平台设计方案，围绕交易链路的**一致性、并发安全、资金准确**三条主线展开。

v2.0 技术栈：**Vue 3 前端 + Python/FastAPI 模块化单体 + PostgreSQL + Redis，Docker Compose 部署到腾讯云 CVM**。

## 快速开始（本地开发）

### 先说终端

下面的命令**在 Git Bash 和 PowerShell 里都能直接粘贴执行**，为此用了两处写法：

| 写法 | 原因 |
|---|---|
| 用 `;` 串联命令，不用 `&&` | Windows PowerShell 5.1 不支持 `&&`（会报"不是有效的语句分隔符"） |
| npm 写成 `npm.cmd` | PowerShell 默认执行策略会拦截 `npm.ps1`，报 `UnauthorizedAccess`。在 Git Bash 里写 `npm` 即可 |

用什么终端都行：

- **Git Bash**（本项目脚本都是 bash，推荐）：开始菜单搜 "Git Bash"，或在项目目录右键 → "Git Bash Here"
- **PowerShell / cmd**：直接粘贴下面的命令
- **PyCharm 的 Terminal**：注意改过环境变量后要**重启 PyCharm** 才生效

> 前置：**Docker Desktop 已启动**（它依赖 WSL2）。工具链安装见 [16-deployment §2](docs/16-deployment.md)。

### 1. 启动数据库与 Redis

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
```

首次启动会自动建好 3 个角色、3 个扩展、14 个 schema（`deploy/postgres/init/01-init.sh`）。端口只绑定 `127.0.0.1`，不对局域网暴露。

确认健康状态（两个都应是 `healthy`）：

```bash
docker compose -f deploy/docker-compose.dev.yml ps
```

### 2. 初始化后端（只需一次）

```bash
cd backend; cp ../deploy/.env.example .env
```

```bash
cd backend; uv sync; uv run alembic upgrade head
```

`uv sync` 会自动使用 Python 3.12（见 `backend/.python-version`），不影响系统里已有的其它版本。

**每次 `git pull` 之后都要重跑迁移**：

```bash
cd backend; uv run alembic upgrade head
```

### 3. 起三个服务（各开一个终端窗口）

```bash
cd backend; uv run uvicorn app.main:app --reload --port 8000
```

```bash
cd web-mall; npm.cmd run dev
```

```bash
cd web-admin; npm.cmd run dev
```

| 服务 | 地址 | 说明 |
|---|---|---|
| 后端 API | http://127.0.0.1:8000 | 接口文档 http://127.0.0.1:8000/docs |
| 买家商城 | http://localhost:5173 | 搜索商品、规格选择 |
| 商家后台 | http://localhost:5174 | 发布商品、上下架 |

两个前端都用 Vite 把 `/api` 代理到 8000 端口，**前后端同源，不需要 CORS**。

### 4. 造演示数据（可选，但强烈建议）

```bash
cd backend; uv run python scripts/seed_demo.py
```

会建好类目树、一个平台管理员、一个商家和三个已上架商品，**并在结尾打印两个账号的手机号和密码**。
用商家账号登录 http://localhost:5174 就能看到商品；在 http://localhost:5173 用买家身份浏览。

### 5. 跑测试

首次需要建一次测试库（用 `eshop_owner` 连接，避免和应用角色纠缠权限）：

```bash
docker exec eshop-postgres psql -U eshop_owner -d eshop -c "CREATE DATABASE eshop_test OWNER eshop_owner;"
```

```bash
docker exec eshop-postgres psql -U eshop_owner -d eshop_test -c "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS btree_gin; CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
```

之后直接跑即可（夹具会自动把测试库迁移到最新并清表）：

```bash
cd backend; uv run pytest -q
```

测试连的是**真实的 PostgreSQL 和 Redis**，不 mock 数据库。

### 常用命令

```bash
docker compose -f deploy/docker-compose.dev.yml logs -f postgres
```

```bash
docker exec -it eshop-postgres psql -U eshop_owner -d eshop
```

```bash
docker exec -it eshop-redis redis-cli
```

```bash
cd backend; uv run alembic revision --autogenerate -m "描述"
```

> 生成迁移后**务必先看一眼**：确认里面没有 `drop_table('alembic_version')`，也没有把别的模块的表当成"多余的表"删掉。

### 代码检查

```bash
cd backend; uv run ruff check .; uv run ruff format --check .
```

```bash
cd web-mall; npm.cmd run type-check; npm.cmd run lint
```

```bash
cd web-admin; npm.cmd run type-check; npm.cmd run lint
```

### 停止

三个服务窗口按 `Ctrl+C`。数据库与 Redis：

```bash
docker compose -f deploy/docker-compose.dev.yml down
```

加 `-v` 会**连数据一起删掉**（下次要重新迁移建表），平时别加。

## 文档索引

| # | 文档 | 解决的问题（对应需求编号） |
|---|---|---|
| 01 | [总体架构与模块划分](docs/01-overview.md) | 全局：模块拆分、技术选型、数据一致性策略 |
| 02 | [商品域模型：SPU + SKU](docs/02-domain-model.md) | ⑧ 商品规格 |
| 03 | [库存设计：排队削峰防超发](docs/03-inventory.md) | ① 库存超发 → 排队 |
| 04 | [优惠券：Redis 原子发券](docs/04-coupon.md) | ② 券超发 → Redis 原子操作 |
| 05 | [促销计算引擎与优惠分摊](docs/05-promotion-engine.md) | ③ 复杂优惠券的设计与计算 |
| 06 | [运费计算](docs/06-freight.md) | ⑨ 首重/续重、多仓、冲突取首重最高 |
| 07 | [订单、拆单与状态机](docs/07-order-and-split.md) | ⑦ 母子单、⑪ 订单流转、⑫ 退货状态机 |
| 08 | [退货/售后：积分、优惠券、库存恢复](docs/08-aftersale.md) | ④ 退货链路，库存恢复节点=入库后 |
| 09 | [支付：异步回调与对账补偿](docs/09-payment.md) | ⑫ 支付回调 + 对账补偿 |
| 10 | [幂等与防连点](docs/10-idempotency.md) | ⑤ 连点幂等 |
| 11 | [价格一致性：前端算价 / 后端裁决](docs/11-price-consistency.md) | ⑥ 价格一致性、金额精度 |
| 12 | [评论系统](docs/12-review.md) | ⑩ 购后评价、仅一次 |
| - | [数据库 DDL 与数据字典](docs/13-schema.md) | 全部表结构、索引（PostgreSQL） |
| - | [Redis Key 与 Lua 脚本汇总](docs/14-redis-keys.md) | ②③① 的原子操作实现 |
| - | [接口清单与错误码](docs/15-api-and-errors.md) | 核心接口契约 |
| - | [开发环境与部署](docs/16-deployment.md) | 本地环境、Docker Compose、腾讯云部署、凭证清单 |
| - | [前端设计系统与开发约定](docs/17-frontend-design-system.md) | 视觉规范、三层 token、大促换肤机制、写页面的硬性约定 |

## 一页纸总览

```
   浏览器（买家 PC 商城 / 商家·运营后台，Vue 3）
                        │ HTTP
            ┌───────────▼────────────┐
            │  Nginx（静态资源 + 反代）│
            └───────────┬────────────┘
                        │
   ┌────────────────────▼─────────────────────────────────────┐
   │            FastAPI 应用（模块化单体，多 worker）            │
   │  中间件：JWT 鉴权 · 限流 · Idempotency-Key 幂等            │
   │ ┌────────┬────────┬────────┬────────┬────────┬────────┐  │
   │ │product │inventory│ cart  │promotion│freight │ trade  │  │
   │ ├────────┼────────┼────────┼────────┼────────┼────────┤  │
   │ │payment │aftersale│ user  │ review │settlement│notify│  │
   │ └────────┴────────┴────────┴────────┴────────┴────────┘  │
   └──────┬──────────────────────────────────┬────────────────┘
          │                                  │
   ┌──────▼──────┐                    ┌──────▼──────────────────────┐
   │ PostgreSQL  │  ← 最终账本         │ Redis                        │
   │ 单库，按模块 │                    │ 库存/券闸门 · 缓存 · 幂等键   │
   │ 分 schema   │                    │ Streams 事件 · ARQ 任务队列   │
   └─────────────┘                    └──────────────┬──────────────┘
                                                     │
                                       ┌─────────────▼──────────────┐
                                       │ Worker 进程（ARQ + 消费者）  │
                                       │ 订单超时 · 消息投递 · 对账   │
                                       └────────────────────────────┘
```

## 三条主线的设计结论

**1. 一致性：以数据库为最终账本，Redis 只是"闸门"**

Redis 承担高并发下的写入压力，PostgreSQL 承担正确性。所有 Redis 的库存/券库存都是**前置拦截器**，DB 侧用条件更新（`WHERE stock >= n`）、`CHECK` 约束和唯一索引兜底，两者定时对账修正。**绝不出现"只有 Redis 有库存"的情况**——Redis 挂了，下单降级为拒绝或限流后直扣 DB，而不是本地兜底（本地兜底会造出新的超发）。

模块化单体共用一个 PG 库，**下单时的订单写入、DB 库存预占、券锁定可以放在同一个本地事务里**，不需要分布式事务；只有 Redis 侧操作和异步副作用（通知、积分、统计）走补偿与事件。

**2. 并发安全：排队 + 原子操作 + 幂等**

- 热点 SKU：库存分片 + FIFO 令牌排队，把随机抢购转成有序消费。
- 领券：单条 Lua 脚本完成"查库存 → 查限领 → 扣减 → 记领取"，Redis 单线程保证原子。
- 接口：幂等键 + 状态机前置守卫 + 唯一索引三层去重。

**3. 资金准确：一切金额用整型分，后端是唯一裁决者**

前端算价只做展示，提交时后端重算并逐项对比，不一致返回 `409 PRICE_CHANGED` 让用户确认。优惠分摊用**最大余数法**保证分摊之和恰好等于总优惠，退款时按分摊明细原路扣回。所有金额字段是 `BIGINT` 存分，Python 代码里金额一律 `int`，禁止 `float`；比例类计算用 `Decimal` 并显式取整。

## 关键设计取舍

| 取舍点 | 选择 | 理由 |
|---|---|---|
| 架构形态 | 模块化单体（按领域分包，单库多 schema） | 第一期运维成本低，模块边界保留，日后可按模块拆服务 |
| 库存扣减时机 | 下单预占（Redis + DB 同事务）+ 支付成功后实扣 | 防超卖同时不长期锁库存 |
| 订单超时 | ARQ 延迟任务 + 定时扫描双保险 | 延迟任务可能丢（Redis 故障），扫描兜底 |
| 异步事件 | 本地消息表（outbox）→ Redis Streams | 不引入独立 MQ，消息不丢靠 outbox 保证 |
| 搜索 | PostgreSQL `pg_trgm`（二期可加中文分词） | 不引入 ES，单库无分片，商家查订单直接走索引 |
| 优惠券叠加 | 类型互斥 + 同类型只取最优 1 张 | 枚举组合可控，避免规则爆炸 |
| 拆单 | 下单时按店铺拆母子单 | 支付用母单，履约/退款用子单 |
| 退货库存恢复 | 商家确认入库质检后触发 | 防止"申请即回补"造成二次超卖 |
| 优惠券退款 | 整单退→券退回，部分退→不退 | 兼顾体验与防刷 |
| 运费冲突 | 冲突时以首重最高的 SKU 为准 | 需求指定；同仓合并计费不收多次首重 |

## 技术选型

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Vue Router + Element Plus；两个应用：买家 PC 商城、商家/运营后台 |
| 后端 | Python 3.12 + FastAPI + Pydantic v2，Uvicorn 多 worker |
| 数据访问 | SQLAlchemy 2.0（async）+ asyncpg，Alembic 管理迁移 |
| 数据库 | PostgreSQL 17（单库，按模块分 schema） |
| 缓存/闸门 | Redis 7.4（单实例 + AOF 持久化） |
| 异步任务 | ARQ（延迟任务、定时任务）+ Redis Streams（领域事件，消费组） |
| 一致性 | 本地事务 + 本地消息表（outbox）+ 定时对账 |
| ID 生成 | 雪花算法（Python 实现，worker id 来自环境变量），JSON 中以字符串返回 |
| 部署 | Docker Compose：nginx / api / worker / postgres / redis，运行在腾讯云 CVM |
| 可观测 | 结构化日志（JSON，stdout → docker 日志）、`/healthz`、Prometheus 指标端点（二期接 Grafana） |
| 第一期不做 | 真实支付渠道（用模拟支付）、COS 对象存储（图片存本地卷）、短信（账号密码登录）、域名与 HTTPS（IP 访问） |

---

> 所有文档中的金额单位统一为**分（整数，`BIGINT`）**，时间统一为 `TIMESTAMPTZ(3)`（存 UTC，展示时转东八区），ID 统一为 `BIGINT`（前端按字符串处理）。
