#!/bin/bash
# ============================================================
# PostgreSQL 首次初始化：建角色、建 schema、建扩展、授权
#
# 只在数据目录为空时执行一次（docker-entrypoint-initdb.d 的机制）。
# 以 POSTGRES_USER（eshop_owner，超级用户）身份对 POSTGRES_DB 执行。
#
# 之后所有表结构由 Alembic 迁移创建，这里只负责"容器"层面的东西。
# 角色划分见 docs/16-deployment.md §4.2：
#   eshop_owner     拥有全部对象，执行 DDL（迁移用）
#   eshop_app       应用运行角色，只能增删改查（无 DDL 权限）
#   eshop_readonly  只读（报表、排查）
# ============================================================
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v app_user="$APP_USER" \
     -v app_password="$APP_PASSWORD" \
     -v ro_user="$READONLY_USER" \
     -v ro_password="$READONLY_PASSWORD" <<'EOSQL'

-- ---------- 1. 角色 ----------
-- 先按需创建（\gexec 执行 SELECT 生成的语句），再统一设置密码
SELECT format('CREATE ROLE %I LOGIN', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'app_user', :'app_password')
\gexec

SELECT format('CREATE ROLE %I LOGIN', :'ro_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user')
\gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'ro_user', :'ro_password')
\gexec

-- 应用角色：语句超时兜底，防止慢查询拖住连接池
ALTER ROLE :"app_user" SET statement_timeout = '5s';
ALTER ROLE :"app_user" SET idle_in_transaction_session_timeout = '60s';

-- ---------- 2. 扩展 ----------
CREATE EXTENSION IF NOT EXISTS pg_trgm;              -- 商品/评价模糊搜索（docs/02 §7）
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;   -- 慢 SQL 分析（compose 里已 preload）
CREATE EXTENSION IF NOT EXISTS btree_gin;            -- 复合 GIN 索引用

-- ---------- 3. Schema（一个模块一个，见 docs/13-schema.md §0.1）----------
-- 注意：用户模块的 schema 叫 account 而不是 user，因为 user 是保留字
CREATE SCHEMA IF NOT EXISTS account;      -- 账号、店铺、地址、积分
CREATE SCHEMA IF NOT EXISTS product;      -- 类目、SPU、SKU、规格
CREATE SCHEMA IF NOT EXISTS inventory;    -- 仓库、库存、库存流水
CREATE SCHEMA IF NOT EXISTS cart;         -- 购物车
CREATE SCHEMA IF NOT EXISTS promotion;    -- 券、活动、叠加规则
CREATE SCHEMA IF NOT EXISTS freight;      -- 运费模板与规则
CREATE SCHEMA IF NOT EXISTS trade;        -- 母单、子单、订单项、发货单
CREATE SCHEMA IF NOT EXISTS payment;      -- 支付单、回调日志、退款、对账
CREATE SCHEMA IF NOT EXISTS aftersale;    -- 售后单
CREATE SCHEMA IF NOT EXISTS review;       -- 评价
CREATE SCHEMA IF NOT EXISTS settlement;   -- 商家账单（二期）
CREATE SCHEMA IF NOT EXISTS notify;       -- 站内信
CREATE SCHEMA IF NOT EXISTS core;         -- 基础设施（本地消息表等）
CREATE SCHEMA IF NOT EXISTS ops;          -- 运维（告警、降级开关）

-- ---------- 4. 授权 ----------
GRANT USAGE ON SCHEMA account, product, inventory, cart, promotion, freight,
                       trade, payment, aftersale, review, settlement, notify, core, ops
      TO :"app_user";
GRANT USAGE ON SCHEMA account, product, inventory, cart, promotion, freight,
                       trade, payment, aftersale, review, settlement, notify, core, ops
      TO :"ro_user";

-- 已存在对象的权限
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA
      account, product, inventory, cart, promotion, freight, trade, payment,
      aftersale, review, settlement, notify, core, ops
      TO :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA
      account, product, inventory, cart, promotion, freight, trade, payment,
      aftersale, review, settlement, notify, core, ops
      TO :"app_user";
GRANT SELECT ON ALL TABLES IN SCHEMA
      account, product, inventory, cart, promotion, freight, trade, payment,
      aftersale, review, settlement, notify, core, ops
      TO :"ro_user";

-- 将来由 eshop_owner（即当前用户）创建的对象，自动授予上述权限，
-- 这样 Alembic 迁移新建的表不需要每次手工 GRANT
ALTER DEFAULT PRIVILEGES IN SCHEMA account, product, inventory, cart, promotion, freight,
                                      trade, payment, aftersale, review, settlement, notify, core, ops
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA account, product, inventory, cart, promotion, freight,
                                      trade, payment, aftersale, review, settlement, notify, core, ops
      GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA account, product, inventory, cart, promotion, freight,
                                      trade, payment, aftersale, review, settlement, notify, core, ops
      GRANT SELECT ON TABLES TO :"ro_user";

EOSQL

echo ">>> eshop 数据库初始化完成：角色、扩展、14 个 schema、权限"
