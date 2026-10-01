#!/usr/bin/env bash
# ============================================================
# 生成 deploy/.env —— 生产环境的所有密钥
#
# 用法（在 deploy/ 目录下，服务器上执行）：
#   ./scripts/gen-secrets.sh 49.232.44.51          # 参数是公网 IP
#
# 行为：
#   1. 从 .env.production.example 复制出 .env，权限立刻设为 600
#   2. 把每个 __GENERATE__ 换成新的安全随机值
#   3. 用刚生成的口令拼出 DATABASE_URL / MIGRATION_DATABASE_URL / REDIS_URL
#   4. 校验没有占位符残留
#
# ★ 只生成一次。.env 已存在时本脚本拒绝覆盖 —— 覆盖会让 PHONE_ENC_KEY 变化，
#   已存的手机号就再也解不出来了。
# ============================================================
set -euo pipefail

# 生成的 .env 里全是密钥。先把 umask 收紧，避免从创建到 chmod 之间
# 有一瞬间是 644（这个窗口在共享机器上是真实存在的）。
umask 077

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEPLOY_DIR"

TEMPLATE=".env.production.example"
ENV_FILE=".env"

SERVER_IP="${1:-}"
if [[ -z "$SERVER_IP" ]]; then
    echo "用法：$0 <服务器公网 IP>" >&2
    echo "     （用来拼模拟支付回调的地址 PAY_NOTIFY_BASE_URL）" >&2
    exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
    echo "★ $DEPLOY_DIR/$ENV_FILE 已存在，拒绝覆盖。" >&2
    echo "  覆盖会换掉 PHONE_ENC_KEY，已存储的手机号将无法解密。" >&2
    echo "  确实要重新生成，请先手工备份并删除它。" >&2
    exit 1
fi

if [[ ! -f "$TEMPLATE" ]]; then
    echo "找不到模板 $TEMPLATE" >&2
    exit 1
fi

# ---------- 找一个真能跑的 python ----------
# ★ 不能只判断 `command -v python3`：Windows 上它可能命中 Microsoft Store 的
#   应用执行别名，那个 shim 存在但一执行就报错、返回空串。结果就是"生成了"
#   一串空密码而脚本毫不知情。所以要**真的执行一次**再认。
PY_BIN=""
for cand in python3 python; do
    if "$cand" -c 'import secrets' >/dev/null 2>&1; then
        PY_BIN="$cand"
        break
    fi
done
if [[ -z "$PY_BIN" ]]; then
    echo "找不到可用的 python3（需要能 import secrets）" >&2
    exit 1
fi
echo "[gen-secrets] 用 $PY_BIN 生成随机数"

# ---------- 生成器 ----------
gen_token() {
    # 48 字节 → 64 个 url-safe 字符，远超 HS256 要求的 32 字节
    "$PY_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))'
}
gen_aes_key() {
    # PHONE_ENC_KEY 必须是 32 字节的 base64（AES-256-GCM）
    "$PY_BIN" -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'
}

env_val() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- ; }

set_var() {
    local var="$1" value="$2"
    # 生成值一律校验非空：空值会让应用以"空密码"连库（甚至可能成功），
    # 比直接报错危险得多。
    if [[ -z "$value" ]]; then
        echo "★ 生成 $var 得到空值，中止" >&2
        exit 1
    fi
    # 用 | 作分隔符：生成的是 base64url 字符集，不含 |
    sed -i "s|^${var}=.*|${var}=${value}|" "$ENV_FILE"
}

# ---------- 1. 复制模板 ----------
cp "$TEMPLATE" "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "[gen-secrets] 已创建 $ENV_FILE（权限 600）"

# ---------- 2. 生成随机密钥 ----------
for var in \
    POSTGRES_OWNER_PASSWORD \
    POSTGRES_APP_PASSWORD \
    POSTGRES_READONLY_PASSWORD \
    REDIS_PASSWORD \
    JWT_SECRET \
    PRICE_TOKEN_SECRET \
    PHONE_HASH_KEY \
    MOCK_PAY_SECRET
do
    set_var "$var" "$(gen_token)"
done

# 单独处理：这个不是 token，是 32 字节的 base64
set_var PHONE_ENC_KEY "$(gen_aes_key)"

echo "[gen-secrets] 已生成 9 个随机密钥"

# ---------- 3. 拼出连接串 ----------
PG_OWNER_PW="$(env_val POSTGRES_OWNER_PASSWORD)"
PG_APP_PW="$(env_val POSTGRES_APP_PASSWORD)"
REDIS_PW="$(env_val REDIS_PASSWORD)"
PG_DB="$(env_val POSTGRES_DB)"
PG_APP_USER="$(env_val POSTGRES_APP_USER)"
PG_OWNER_USER="$(env_val POSTGRES_OWNER_USER)"

# 主机名用 compose 服务名（postgres / redis），不是 127.0.0.1
set_var DATABASE_URL \
    "postgresql+asyncpg://${PG_APP_USER}:${PG_APP_PW}@postgres:5432/${PG_DB}"
set_var MIGRATION_DATABASE_URL \
    "postgresql+asyncpg://${PG_OWNER_USER}:${PG_OWNER_PW}@postgres:5432/${PG_DB}"
set_var REDIS_URL "redis://:${REDIS_PW}@redis:6379/0"

# ---------- 4. 回调地址 ----------
# 用 ESHOP_HTTP_PORT 拼出对外地址；端口是默认的 80 时省略，避免写出 :80
ESHOP_PORT="$(env_val ESHOP_HTTP_PORT)"
if [[ -z "$ESHOP_PORT" || "$ESHOP_PORT" == "80" ]]; then
    set_var PAY_NOTIFY_BASE_URL "http://${SERVER_IP}"
else
    set_var PAY_NOTIFY_BASE_URL "http://${SERVER_IP}:${ESHOP_PORT}"
fi

# ---------- 5. 收尾校验 ----------
# 只看**非注释行**：模板的注释里也写着 __GENERATE__ 这几个字（在解释它是
# 什么），不排除掉的话这里永远是个假阳性。
if grep -vE '^[[:space:]]*#' "$ENV_FILE" | grep -qE '__GENERATE__|__DERIVED__|CHANGE_ME'; then
    echo "★ 仍有占位符未替换，请检查：" >&2
    grep -vE '^[[:space:]]*#' "$ENV_FILE" | grep -nE '__GENERATE__|__DERIVED__|CHANGE_ME' >&2
    exit 1
fi

if [[ "$(stat -c '%a' "$ENV_FILE")" != "600" ]]; then
    echo "★ $ENV_FILE 权限不是 600，请手工修正" >&2
    exit 1
fi

echo "[gen-secrets] 完成。"
echo
echo "  下一步："
echo "    1. 把 $DEPLOY_DIR/$ENV_FILE 备份到密码管理器（丢失即不可恢复）"
echo "    2. ./scripts/prepare-host.sh          建数据目录并设好属主"
echo "    3. docker compose build"
echo "    4. docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm migrate"
echo "    5. docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d"
