# 16 开发环境与部署

> 本文档覆盖三件事：本地 Windows 开发环境怎么搭、生产环境在腾讯云上怎么部署、部署需要哪些凭证以及如何安全地提供。

## 1. 总体拓扑

```
本地 Windows 11（开发）                         腾讯云 CVM（生产，Ubuntu 24.04 LTS）
┌─────────────────────────────────┐            ┌─────────────────────────────────────────┐
│ PyCharm / VS Code                │            │ docker compose                           │
│  ├─ backend：uv run uvicorn      │            │  ┌───────┐  :80                          │
│  ├─ web-mall：npm run dev (5173) │   scp /    │  │ nginx │◄──── 公网（安全组仅放行 80/22） │
│  └─ web-admin：npm run dev (5174)│  git pull  │  └───┬───┘                               │
│ Docker Desktop（WSL2）            │ ─────────► │      ├── 静态资源：web-mall、web-admin     │
│  ├─ postgres:17  (127.0.0.1:5432) │            │      └── /api ──► api ×N (uvicorn)        │
│  └─ redis:7.4    (127.0.0.1:6379) │            │                     worker (ARQ + 消费者) │
└─────────────────────────────────┘            │                     postgres  redis       │
                                               │ 数据盘 /data：pgdata、redis、media、backup │
                                               └─────────────────────────────────────────┘
```

本地只用 Docker 跑数据库和 Redis，后端与前端直接在 Windows 上运行（热重载快、断点调试方便）；生产环境所有组件都在容器里。

## 2. 本地 Windows 需要安装的工具

已于 2026-10-01 检查本机，状态如下：

| 工具 | 用途 | 本机状态 | 是否必须 |
|---|---|---|---|
| Git | 版本管理 | ✅ 2.55 | 必须 |
| uv | Python 版本与依赖管理 | ✅ 0.12 | 必须 |
| Python 3.12 | 后端运行时（由 uv 安装，与系统里的 3.14 互不影响） | 需安装 | 必须 |
| Node.js | 前端构建与开发服务器 | ✅ v24 | 必须 |
| npm | 前端依赖管理 | ✅ 11 | 必须（不额外引入 pnpm） |
| WSL2 | Docker Desktop 的运行基础 | ❌ 未安装 | 必须 |
| Docker Desktop | 本地运行 PostgreSQL、Redis；构建生产镜像 | ❌ 未安装 | 必须 |
| OpenSSH 客户端 | 连接服务器、上传代码 | Windows 11 自带 | 必须 |
| 数据库 GUI | 查看表数据 | — | 可选：PyCharm Professional 自带 Database 工具，或 DBeaver |
| Redis GUI | 查看 key | — | 可选：Redis Insight |

**为什么 Redis 不在 Windows 上原生安装**：Redis 官方不提供 Windows 版本，网上的 Windows 移植版（如 tporadowski/redis、Memurai）版本落后或是商业软件，行为与生产环境的 Linux Redis 不完全一致。PostgreSQL 虽然有 Windows 安装包，但同样建议用 Docker，保证本地与生产是同一个版本、同一份配置。所以本地**只需要装 Docker Desktop，PostgreSQL 和 Redis 都以容器运行**，不需要单独安装，`psql`、`redis-cli` 也直接用容器里的。

### 2.1 安装 WSL2

以**管理员身份**打开 PowerShell（开始菜单右键 → 终端（管理员））：

```powershell
wsl --install
```

完成后**重启电脑**。重启后会自动弹出 Ubuntu 窗口要求设置 Linux 用户名和密码（这个用户只在 WSL 内使用，随便设置即可）。

验证：

```powershell
wsl --status
```

输出中应包含"默认版本: 2"。如果提示需要开启虚拟化，进入 BIOS 打开 Intel VT-x / AMD-V（SVM），任务管理器 → 性能 → CPU 中"虚拟化：已启用"即为成功。

### 2.2 安装 Docker Desktop

```powershell
winget install --id Docker.DockerDesktop -e
```

或从 Docker 官网下载安装包。安装时勾选 "Use WSL 2 instead of Hyper-V"。安装完成后启动 Docker Desktop，等待左下角显示 "Engine running"。

验证（普通 PowerShell 或 PyCharm 终端均可）：

```bash
docker version
```

```bash
docker run --rm hello-world
```

**国内网络拉取镜像慢或失败**：Docker Desktop → Settings → Docker Engine，在 JSON 中加入镜像加速地址后点 "Apply & restart"。可用的加速地址会变化，以腾讯云、阿里云容器镜像服务控制台当时提供的地址为准；也可以开启系统代理后在 Settings → Resources → Proxies 中配置。

> Docker Desktop 对个人、教育和小型企业（员工 < 250 人且年收入 < 1000 万美元）免费，超出需购买订阅。

### 2.3 安装 Python 3.12

uv 已安装，直接用它安装项目使用的 Python 版本：

```bash
uv python install 3.12
```

项目根目录的 `backend/.python-version` 会写明 `3.12`，`uv sync` 时自动使用它，不影响系统里已有的 Python 3.14。

### 2.4 检查 SSH 客户端

```bash
ssh -V
```

能输出 `OpenSSH_for_Windows_...` 即可。若提示找不到命令：设置 → 系统 → 可选功能 → 添加功能 → "OpenSSH 客户端"。

## 3. 本地开发环境

### 3.1 仓库结构

```
eshop/
├── backend/                       # FastAPI 应用（模块划分见 01 §2.1）
│   ├── pyproject.toml             # 依赖（uv 管理，uv.lock 锁定精确版本）
│   ├── .python-version            # 3.12
│   ├── alembic.ini                # ★ 必须保持纯 ASCII，见下方说明
│   ├── .env                       # 从 deploy/.env.example 复制（已 gitignore）
│   ├── app/
│   │   ├── main.py                # FastAPI 入口：lifespan、中间件、异常处理、健康检查
│   │   ├── router.py              # 各模块路由的聚合处
│   │   ├── models.py              # ORM 模型登记处（autogenerate 只认导入过的模型）
│   │   ├── core/                  # config / db / redis / redis_keys / errors / schemas /
│   │   │                          # response / enums / logging / snowflake / security /
│   │   │                          # deps / base / context
│   │   ├── modules/               # 业务模块（待实现）
│   │   └── worker/main.py         # ARQ worker 配置与入口
│   ├── migrations/                # Alembic（env.py + versions/）
│   ├── scripts/seed_demo.py       # 本地演示数据（类目、管理员、商家、商品）
│   ├── lua/                       # Redis Lua 脚本（待添加）
│   ├── tests/                     # 集成测试（连真实 PG/Redis）
│   └── Dockerfile                 # 待添加
├── web-mall/                      # 买家 PC 商城（Vue 3.5 + Vite 8 + TS 6 + Element Plus 2.14）
│   └── src/{api,stores,router,views,utils}
│       └── views/                 # 登录、商品列表、商品详情（含规格选择器）、我的、系统状态
├── web-admin/                     # 商家/运营后台（同上，跑在 5174 端口）
│   └── src/views/                 # 登录、我的商品、发布商品（规格编辑器）、编辑商品、系统状态
├── .claude/launch.json            # 三个本地服务的启动配置（api / web-mall / web-admin）
├── shared/                        # 待添加：price-testcases.json（11 §8）
├── deploy/
│   ├── docker-compose.dev.yml     # 本地：只起 PostgreSQL 17 + Redis 7.4
│   ├── .env.example               # 环境变量模板（不含真实值，可提交）
│   ├── postgres/init/01-init.sh   # 首次初始化：角色、扩展、14 个 schema、权限
│   ├── docker-compose.yml         # 待添加：生产全部服务
│   ├── nginx/                     # 待添加
│   └── redis/                     # 生产 redis.conf 待添加（本地直接用命令行参数）
├── docs/
└── .gitignore
```

**`alembic.ini` 必须保持纯 ASCII**：Alembic 用**系统 locale 编码**读这个文件
（`alembic/util/compat.py` 里是 `encoding="locale"`，中文 Windows 下即 GBK），
写入任何非 ASCII 字节都会让所有 alembic 命令直接 `UnicodeDecodeError` 崩溃。
连接串不在这个文件里配，由 `migrations/env.py` 从应用配置注入。

**前后端的 API 层目前是复制的**：`web-mall/src/api/` 与 `web-admin/src/api/` 内容相同。
其中 `errors.ts` 是 `backend/app/core/errors.py` 的镜像，**改后端错误码时必须同步两边**，
CI 会加一条比对检查（15 §3.1）。将来这套代码变大后，再考虑抽成共享包。

**依赖版本锁定**：后端用 `uv.lock`、前端用 `package-lock.json` 锁定精确版本并提交到仓库；Docker 基础镜像使用明确的版本标签（如 `postgres:17.6`、`redis:7.4.5`、`python:3.12-slim`、`nginx:1.28-alpine`，具体小版本在创建 compose 文件时取当时的最新补丁版本），不使用 `latest`。

### 3.2 本地启动步骤

**准备**：Docker Desktop 已启动（需要 WSL2）。本机若还没有 `backend/.env`：

```bash
cd backend && cp ../deploy/.env.example .env
```

**① PostgreSQL 与 Redis**（端口只绑定 127.0.0.1，不对局域网暴露）

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
```

首次启动会自动执行 `deploy/postgres/init/01-init.sh`：建 3 个角色、3 个扩展、14 个 schema 并配好默认权限。

**② 后端依赖与迁移**

```bash
cd backend && uv sync && uv run alembic upgrade head
```

**③ 起三个服务**（各开一个终端窗口）

```bash
cd backend && uv run uvicorn app.main:app --reload --port 8000
```

```bash
cd web-mall && npm run dev
```

```bash
cd web-admin && npm run dev
```

| 服务 | 地址 | 说明 |
|---|---|---|
| 后端 API | http://127.0.0.1:8000 | 接口文档 `/docs`（生产环境自动关闭） |
| 买家商城 | http://localhost:5173 | 搜索、规格选择 |
| 商家/运营后台 | http://localhost:5174 | 发布商品、上下架 |

**④ 异步任务 worker**（用到延迟任务与定时任务时才需要）

```bash
cd backend && uv run arq app.worker.main.WorkerSettings
```

**⑤ 演示数据**（可选）

```bash
cd backend && uv run python scripts/seed_demo.py
```

会建好类目树、一个平台管理员、一个商家和三个已上架商品，并在结尾打印两个账号的密码。

> 本地 `backend/.env` 用的是 `deploy/.env.example` 里的弱密码，只监听回环地址，**不要把生产密钥放到本地文件里**。

**开发调试命令**

```bash
docker compose -f deploy/docker-compose.dev.yml logs -f postgres
```

```bash
docker exec -it eshop-postgres psql -U eshop_owner -d eshop
```

```bash
docker exec -it eshop-redis redis-cli
```

### 3.3 测试

| 范围 | 工具 | 说明 |
|---|---|---|
| 后端单元测试 | pytest + hypothesis | 算价、分摊、状态机等纯函数 |
| 后端集成测试 | pytest + pytest-asyncio + httpx | 连接 docker 中独立的测试库 `eshop_test`，**不 mock 数据库**（唯一约束、行锁、CHECK 约束只有真库才能测出来） |
| 前端单元测试 | vitest | 价格工具函数、共享用例对齐（11 §8）；**尚未搭建** |
| 代码检查 | ruff、eslint + oxlint、vue-tsc | 已接入；mypy 与 CI 待补 |

集成测试跑之前需要**建一次测试库**（用 `eshop_owner` 连接，避免和应用角色纠缠权限）：

```bash
docker exec eshop-postgres psql -U eshop_owner -d eshop -c "CREATE DATABASE eshop_test OWNER eshop_owner;"
```

```bash
docker exec eshop-postgres psql -U eshop_owner -d eshop_test -c "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS btree_gin; CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
```

之后直接跑 pytest 即可，夹具会自动把测试库迁移到最新版本，并在每个用例前清空业务表：

```bash
cd backend && uv run pytest -q
```

## 4. 生产环境：Docker Compose

### 4.1 服务清单

| 服务 | 镜像 | 说明 | 端口 |
|---|---|---|---|
| `nginx` | 自建（多阶段：Node 构建两个前端 → `nginx:alpine`） | 静态资源、反向代理、限流、安全响应头 | **对外 80** |
| `api` | 自建（`python:3.12-slim` + uv） | `uvicorn app.main:app --workers 4`，可 `--scale api=N` | 仅内部 8000 |
| `worker` | 同 api 镜像 | `arq app.worker.main.WorkerSettings`：延迟任务、cron、outbox 投递、Streams 消费 | 无 |
| `migrate` | 同 api 镜像 | `alembic upgrade head`，一次性运行后退出 | 无 |
| `postgres` | `postgres:17.x` | 数据挂载 `/data/pgdata` | 仅内部 5432 |
| `redis` | `redis:7.4.x` | AOF 挂载 `/data/redis` | 仅内部 6379 |

**PostgreSQL、Redis 不映射到宿主机公网端口**。需要从本地连接排查时，用 SSH 隧道：

```bash
ssh -i ~/.ssh/eshop_deploy -L 15432:127.0.0.1:15432 ubuntu@<服务器IP>
```

（生产 compose 中 postgres 映射为 `127.0.0.1:15432:5432`，只在服务器本机可访问，通过隧道转发到本地 15432 端口。）

### 4.2 关键配置要点

```yaml
# deploy/docker-compose.yml（节选，完整文件在代码阶段提供）
services:
  api:
    image: eshop-api:${APP_VERSION}
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
      interval: 10s
    deploy:
      resources: { limits: { memory: 1g } }
    logging:
      driver: json-file
      options: { max-size: "50m", max-file: "5" }     # 防止日志写满磁盘

  postgres:
    image: postgres:17.6
    environment:
      POSTGRES_USER: eshop_owner
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_owner_password
      POSTGRES_DB: eshop
    volumes:
      - /data/pgdata:/var/lib/postgresql/data
      - ./postgres/postgresql.conf:/etc/postgresql/postgresql.conf:ro
      - ./postgres/init:/docker-entrypoint-initdb.d:ro
    command: ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
    ports: ["127.0.0.1:15432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U eshop_owner -d eshop"]
    deploy:
      resources: { limits: { memory: 3g } }
    shm_size: 256mb
```

**数据库角色**（`deploy/postgres/init/01-roles.sql`，首次初始化时执行）：

| 角色 | 权限 | 使用者 |
|---|---|---|
| `eshop_owner` | 拥有所有 schema 与表，可执行 DDL | `migrate` 容器、运维 |
| `eshop_app` | 各 schema 的 `USAGE`；表的 `SELECT/INSERT/UPDATE/DELETE`；`trade.order_state_flow` 只有 `SELECT/INSERT` | `api`、`worker` |
| `eshop_readonly` | 只读 | 报表、排查 |

应用账号不能执行 DDL，即使出现 SQL 注入也无法删表；审计流水表从权限上不可篡改（[07 §4.5](07-order-and-split.md)）。

**Nginx 要点**：

- `/` → 商城静态文件，`/admin/` → 后台静态文件，`/api/` → `api:8000`，`/media/` → 上传图片目录（`/data/media`，只读挂载，禁止执行）；
- `limit_req_zone` 按 IP 限速；`/api/pay/notify/` 单独限速；
- `client_max_body_size 10m`；
- 响应头：`Content-Security-Policy`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: same-origin`；
- 前端带 hash 的静态文件 `Cache-Control: max-age=31536000, immutable`，`index.html` 不缓存；
- 生成 `$request_id` 并以 `X-Request-Id` 头传给后端。

## 5. 腾讯云部署

### 5.1 云资源（由你在腾讯云控制台创建）

| 资源 | 建议规格 | 说明 |
|---|---|---|
| 云服务器 CVM（或轻量应用服务器 Lighthouse） | 4 核 8GB，Ubuntu Server 24.04 LTS | 轻量应用服务器价格更低、自带流量包，第一期足够；CVM 后续扩展更灵活 |
| 系统盘 | 50GB SSD | |
| 数据盘 | 100GB 增强型 SSD 云硬盘，挂载到 `/data` | 数据与系统分离，便于快照与扩容（轻量应用服务器可选购"云硬盘"） |
| 公网 IP / 带宽 | 按流量计费或 5Mbps 固定带宽 | |
| 安全组 / 防火墙 | 入站只放行：`22`（**仅限你的办公/家庭出口 IP**）、`80`（全部） | **不要放行 5432、6379** |
| 快照策略 | 数据盘每日自动快照，保留 7 天 | 控制台"快照策略"配置 |

**如果只做演示**：一台 2 核 2G 的轻量应用服务器也能把系统完整跑起来，但必须使用 [§5.5](#55-演示环境2-核-2g-的裁剪配置) 的裁剪配置并加 2G swap，且只能用于演示与验收，不能给真实用户使用。

**地域选择**：

- 选**中国大陆地域**（如广州、上海）：访问速度快，但**以后绑定域名必须完成 ICP 备案**（通常需要 1~3 周，服务器需在腾讯云且满足购买时长要求）；第一期用 IP 访问不需要备案。
- 选**中国香港等非大陆地域**：绑定域名无需备案，但大陆用户访问延迟较高、带宽贵。

### 5.2 服务器初始化（拿到 SSH 访问后由我执行，每一步执行前会先告知）

```bash
# 以下在服务器上执行，仅作说明
sudo apt-get update && sudo apt-get -y upgrade
# 安装 Docker Engine + Compose 插件（Docker 官方 apt 源，国内可用腾讯云镜像源 mirrors.cloud.tencent.com/docker-ce）
# 配置 /etc/docker/daemon.json：
#   "registry-mirrors": ["https://mirror.ccs.tencentyun.com"]   ← 腾讯云内网镜像加速，仅腾讯云服务器可用
#   "log-driver": "json-file", "log-opts": {"max-size": "50m", "max-file": "5"}
# 格式化并挂载数据盘到 /data，写入 /etc/fstab（使用 UUID）
# 创建目录：/data/{pgdata,redis,media,backup}，/opt/eshop
# 开启 unattended-upgrades 自动安全更新
# 禁用 SSH 密码登录（PasswordAuthentication no），只允许密钥登录
```

### 5.3 发布流程

第一期不引入镜像仓库，**在服务器上直接构建镜像**：

```
① 本地：git tag v0.1.0
② 本地 → 服务器：上传代码（任选一种）
     a. git archive 打包 + scp 上传（无需服务器访问代码仓库，推荐）
     b. 服务器 git pull（需要给服务器配置只读 Deploy Key）
③ 服务器：docker compose build
④ 服务器：docker compose run --rm migrate          ← 迁移失败则停止发布
⑤ 服务器：docker compose up -d                     ← 滚动替换 api / worker / nginx
⑥ 服务器：curl http://127.0.0.1/api/healthz        ← 冒烟检查
```

**回滚**：镜像按版本打标签（`eshop-api:v0.1.0`），回滚时把 `.env` 中的 `APP_VERSION` 改回上一版本并 `docker compose up -d`。数据库迁移要求**向前兼容**（新增列带默认值、不在同一版本里删除旧列），保证旧版本代码在新表结构上仍能运行。

**发布窗口**：`up -d` 替换 api 容器时会有数秒的请求失败。第一期接受这一点并选在低峰期发布；二期再做多副本滚动或蓝绿切换。

### 5.4 备份与恢复

| 内容 | 方式 | 频率 | 保留 |
|---|---|---|---|
| PostgreSQL | 宿主机 cron：`docker compose exec -T postgres pg_dump -Fc -U eshop_owner eshop > /data/backup/eshop-$(date +%F).dump` | 每日 03:30 | 本机 7 天 |
| PostgreSQL（异地） | 定期下载到本地，或二期用 COS 存储 | 每周 | 4 周 |
| Redis | AOF 持续写入 + RDB 快照拷贝到 `/data/backup` | 每日 | 3 天 |
| 上传图片 `/data/media` | 随数据盘快照 | 每日 | 7 天 |
| 数据盘整体 | 腾讯云快照策略 | 每日 | 7 天 |
| `.env` | **你自己保存在密码管理器中** | 每次变更后 | 长期 |

**备份必须演练**：上线前在本地用最新的 `.dump` 执行一次 `pg_restore` 到空库，确认能恢复并能启动应用。

### 5.5 演示环境：2 核 2G 的裁剪配置

**适用场景**：已经有一台 2 核 2G / 50GB 系统盘的服务器（或只有演示、验收需求，不想为此单独买机器）。**结论：可以完整跑通全部功能，但只能演示，不能给真实用户使用**，也不能在这上面做压测。

#### 5.5.1 内存预算

2G 内存连裁剪版都很紧，先把账算清楚（单位：MB）：

| 组件 | 内存上限 | 相对 §4.2 的调整 |
|---|---|---|
| 系统 + dockerd | 不设限（约 350–450） | 演示前**先停掉这台机器上其他应用** |
| nginx | 64 | 不变 |
| api（uvicorn） | 400 | worker 数 4 → **1** |
| worker（ARQ） | 300 | 单进程，不开 `--scale` |
| postgres | 640 | `shared_buffers` 1GB → 256MB，`max_connections` 100 → 50 |
| redis | 256 | `maxmemory` 1536MB → **192MB** |
| **合计上限** | **1660** | 余约 380MB 给系统 |

仍然要加 **2G swap** 作为缓冲——不是为了性能，是为了在内存尖峰时不要立刻触发 OOM Killer。

#### 5.5.2 加 swap

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 降低交换倾向（默认 60 会让内核过于积极地换出数据库页，反而拖慢）
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-eshop.conf
sudo sysctl -p /etc/sysctl.d/99-eshop.conf
```

验证：`free -h` 应显示 swap 总量 2.0Gi。

#### 5.5.3 覆盖文件

新增 `deploy/docker-compose.demo.yml`，**不改动** `docker-compose.yml`（4C8G 版本保持原样）：

```yaml
# deploy/docker-compose.demo.yml —— 2 核 2G 演示环境覆盖
# 用法：docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d
services:
  api:
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000",
              "--workers", "1", "--limit-concurrency", "50"]
    deploy:
      resources: { limits: { memory: 400m } }

  worker:
    deploy:
      resources: { limits: { memory: 300m } }

  postgres:
    # 命令行 -c 的优先级高于配置文件，无需再维护第二份 postgresql.conf
    command:
      - postgres
      - -c
      - config_file=/etc/postgresql/postgresql.conf
      - -c
      - shared_buffers=256MB
      - -c
      - effective_cache_size=768MB
      - -c
      - max_connections=50
      - -c
      - work_mem=4MB
      - -c
      - maintenance_work_mem=64MB
      - -c
      - wal_buffers=8MB
      - -c
      - max_wal_size=1GB
      - -c
      - min_wal_size=256MB
    deploy:
      resources: { limits: { memory: 640m } }

  redis:
    # 同样用命令行参数覆盖 redis.conf 里的 maxmemory
    command: ["redis-server", "/usr/local/etc/redis/redis.conf", "--maxmemory", "192mb"]
    deploy:
      resources: { limits: { memory: 256m } }

  nginx:
    deploy:
      resources: { limits: { memory: 64m } }
```

**注意 `redis` 的 `maxmemory-policy` 保持 `noeviction` 不变**：内存满时宁可写入报错让应用走降级，也不能淘汰库存/券 key（[14 §5.1](14-redis-keys.md)）。192MB 对演示数据量足够——按 [14 §4.1](14-redis-keys.md) 的估算，10 万 SKU 的库存 key 只要约 40MB。

发布命令相应改为：

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d
```

#### 5.5.4 磁盘路径

这台机器只有 50GB 系统盘、没有数据盘，所以把 [§5.2](#52-服务器初始化拿到-ssh-访问后由我执行每一步执行前会先告知) 里的 `/data/*` 改成：

```
/opt/eshop/data/{pgdata,redis,media,backup}
```

并把 [§5.4](#54-备份与恢复) 的 PostgreSQL 备份保留期从 7 天降到 **3 天**（`find /opt/eshop/data/backup -name '*.dump' -mtime +3 -delete`）。50GB 的分配大致是：系统 + Docker 约 10GB、镜像约 3GB、数据库与图片数 GB、备份 3 份数 GB，够用但没有余量，**每月看一眼 `df -h`**。

#### 5.5.5 能做与不能做

| | 说明 |
|---|---|
| ✅ 完整功能演示 | 下单 → 模拟支付 → 发货 → 收货 → 评价 → 退货 → 退款 → 对账，全链路都能走通 |
| ✅ 验收测试 | 模拟支付渠道的 7 种故障注入（[09 §3.2](09-payment.md)）都能跑 |
| ✅ 个位数并发 | 同时几个人点，没问题 |
| ⚠️ 秒杀/排队场景 | 能演示但别真压；`--limit-concurrency 50` 会把超出部分挡在门外 |
| ❌ 压测 | 会 OOM，数据不会丢但服务会中断 |
| ❌ 真实用户 | 见 [§6](#6-第一期-http-访问的限制) |

#### 5.5.6 OOM 的风险与处置

- 上面给每个容器都设了内存上限，**超限时 Docker 杀掉的是那个容器，不是整台机器**。
- PostgreSQL 被 OOM 杀掉**不会丢数据**：配置里 `synchronous_commit=on`，重启后会自动做 WAL 崩溃恢复，代价是几十秒不可用。`restart: unless-stopped` 会把它拉起来。
- Redis 有 AOF（`appendfsync everysec`），最多丢 1 秒，且库存最终以 DB 为准并对账修正。
- **演示前先看一眼**：`docker stats --no-stream` 和 `free -h`。如果 postgres 常驻内存已经逼近 640M 上限，说明演示数据量超预期，该考虑升配了。
- 演示期间如果这台机器上还跑着别的应用，**先停掉**：`docker compose down`（其他项目目录下）或 `sudo systemctl stop <服务名>`。

## 6. 第一期 HTTP 访问的限制

第一期按你的选择**不绑定域名、用 IP + HTTP 访问**，带来以下限制，正式对外开放前需要解决：

| 限制 | 影响 | 解决 |
|---|---|---|
| 密码、token 明文传输 | 同一网络中的人可以截获登录凭证 | 绑定域名 + HTTPS（腾讯云免费 DV 证书或 Let's Encrypt） |
| `crypto.randomUUID()` 不可用 | 前端改用 `uuid` 包生成幂等键（[10 §3.1](10-idempotency.md)） | HTTPS 后可恢复使用原生 API |
| refresh token 只能放 `localStorage` | 受 XSS 影响 | HTTPS 后改为 `HttpOnly; Secure` Cookie |
| 真实支付渠道无法接入 | 微信/支付宝要求 HTTPS 回调地址和已备案域名 | 二期 |

**结论**：第一期适合内部测试与演示，不适合让真实用户注册使用。

## 7. 需要你提供的凭证

### 7.1 第一期必须提供

| 凭证 | 用途 | 如何提供 |
|---|---|---|
| 服务器公网 IP | 部署目标 | 直接在对话里告诉我（IP 不是秘密） |
| SSH 登录用户名 | 一般为 `ubuntu`（腾讯云 Ubuntu 镜像默认用户，自带 sudo） | 直接告诉我 |
| **SSH 私钥** | 我通过本机的 `ssh` / `scp` 登录服务器执行部署 | **不要把私钥内容或服务器密码发到对话里**。按 §7.3 在本机生成密钥对，把**公钥**绑定到服务器，只告诉我**私钥文件的路径** |
| 服务器所在地域 | 决定镜像源、备案要求 | 直接告诉我 |
| 你的出口 IP（可选） | 安全组 22 端口白名单 | 在控制台自行配置即可，不需要告诉我 |

### 7.2 不需要你提供、由部署过程生成的密钥

以下密钥由我在部署时用安全随机数生成，**只写入服务器上的 `/opt/eshop/deploy/.env`（权限 600）**，不提交到代码仓库、不出现在对话里。部署完成后请你登录服务器把 `.env` 复制保存到密码管理器（丢失其中的 `PHONE_ENC_KEY` 将无法解密已存储的手机号）。

| 变量 | 用途 |
|---|---|
| `PG_OWNER_PASSWORD` / `PG_APP_PASSWORD` | 数据库角色密码 |
| `REDIS_PASSWORD` | Redis `requirepass` |
| `JWT_SECRET` | access token 签名 |
| `PRICE_TOKEN_SECRET` | priceToken 签名（[11 §4](11-price-consistency.md)） |
| `MOCK_PAY_SECRET` | 模拟支付回调签名（[09 §3.1](09-payment.md)） |
| `PHONE_HASH_KEY` / `PHONE_ENC_KEY` | 手机号哈希与加密（[13 §2](13-schema.md)） |
| 平台管理员初始密码 | 部署后用命令行创建管理员账号时由你交互输入，不经过我 |

生成方式（任一机器上均可）：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 7.3 生成并绑定 SSH 密钥（你来操作）

① 在本机生成专用于部署的密钥对（PowerShell 或 PyCharm 终端均可，提示输入密码短语时可直接回车，或设置一个并在之后用 `ssh-agent` 加载）：

```bash
ssh-keygen -t ed25519 -C "eshop-deploy" -f ~/.ssh/eshop_deploy
```

会生成两个文件：`~/.ssh/eshop_deploy`（**私钥，留在本机，不要发给任何人**）和 `~/.ssh/eshop_deploy.pub`（公钥）。

② 把公钥绑定到服务器，任选一种：

- **腾讯云控制台**：云服务器 → SSH 密钥 → 创建密钥 → 选择"导入已有密钥"，粘贴 `eshop_deploy.pub` 的内容 → 绑定到实例（需要关机绑定）。轻量应用服务器在实例详情 → "密钥"页签中操作。
- **已能用密码登录服务器时**：用控制台的"登录"（网页终端）进入服务器，把公钥内容追加到 `~/.ssh/authorized_keys`。

③ 本机验证能登录：

```bash
ssh -i ~/.ssh/eshop_deploy ubuntu@<服务器IP> "echo ok && sudo -n true && echo sudo-ok"
```

输出 `ok` 和 `sudo-ok` 后，把**服务器 IP、用户名、私钥路径（如 `C:\Users\Administrator\.ssh\eshop_deploy`）**告诉我即可。

### 7.4 当前不需要、但请了解的凭证

| 凭证 | 什么时候需要 | 说明 |
|---|---|---|
| 腾讯云 API 密钥（SecretId / SecretKey） | 只有当你希望我用命令行（`tccli`）创建/管理云资源时 | **第一期不需要**，云资源由你在控制台创建更安全。如果以后需要，请在"访问管理 CAM"中创建**子用户**并只授予必要权限，密钥通过本机环境变量提供，不要发到对话里；**不要使用主账号密钥** |
| COS 存储桶 + 子用户密钥 | 二期：图片迁移到对象存储、异地备份 | 子用户只授权该存储桶的读写 |
| 短信签名、模板、SDK 密钥 | 二期：验证码登录 | 签名与模板需审核，通常 1~2 个工作日 |
| 域名 + ICP 备案 + SSL 证书 | 正式对外开放前 | 见 §6 |
| 微信支付：商户号、APIv3 密钥、商户 API 证书与私钥、平台证书 | 二期：接入微信支付 | 需营业执照申请商户号 |
| 支付宝：应用 AppID、应用私钥、支付宝公钥 | 二期：接入支付宝 | 同上 |
| 腾讯云内容安全密钥 | 二期：评价图片/文字机审 | |

## 8. 环境变量清单（`.env.example`）

```dotenv
# ---- 应用 ----
APP_ENV=production                 # development / production
APP_VERSION=v0.1.0
API_WORKERS=4
LOG_LEVEL=INFO

# ---- 数据库 ----
DATABASE_URL=postgresql+asyncpg://eshop_app:${PG_APP_PASSWORD}@postgres:5432/eshop
PG_OWNER_PASSWORD=                 # 部署时生成
PG_APP_PASSWORD=                   # 部署时生成

# ---- Redis ----
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
REDIS_PASSWORD=                    # 部署时生成

# ---- 安全 ----
JWT_SECRET=                        # 部署时生成
PRICE_TOKEN_SECRET=                # 部署时生成
PHONE_HASH_KEY=                    # 部署时生成
PHONE_ENC_KEY=                     # 部署时生成（32 字节，base64）

# ---- 支付 ----
PAYMENT_MOCK_ENABLED=true          # ★ 接入真实渠道上线前必须改为 false
MOCK_PAY_SECRET=                   # 部署时生成
PAY_NOTIFY_BASE_URL=http://<服务器IP>

# ---- 存储 ----
MEDIA_ROOT=/data/media
MEDIA_URL_PREFIX=/media/
```

## 9. 上线前检查清单

| # | 检查项 |
|---|---|
| 1 | 安全组只放行 22（限 IP）与 80；从外网 `telnet <IP> 5432`、`6379` 均不通 |
| 2 | SSH 已禁用密码登录 |
| 3 | `.env` 权限为 600，已备份到密码管理器；仓库中 `git log -p` 搜索不到任何真实密钥 |
| 4 | 生产环境 `/docs`、`/openapi.json` 返回 404 |
| 5 | 应用以 `eshop_app` 角色连接数据库，执行 `DROP TABLE` 会被拒绝 |
| 6 | Redis 设置了密码，`FLUSHALL`、`KEYS` 命令已禁用 |
| 7 | `pg_dump` 定时任务已生效，并完成过一次恢复演练 |
| 8 | 数据盘快照策略已开启 |
| 9 | 模拟支付的 7 种故障注入场景（[09 §3.2](09-payment.md)）全部验证通过，对账任务能发现并修复"不回调"的订单 |
| 10 | 停止 Redis 容器后下单走 DB 降级路径，恢复后对账任务能修正 Redis 库存 |
