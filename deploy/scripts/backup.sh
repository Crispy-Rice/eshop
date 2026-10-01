#!/usr/bin/env bash
# ============================================================
# eshop 每日备份（在宿主机上由 cron 调用）
#
# 安装：
#   sudo install -m 700 -o root -g root deploy/scripts/backup.sh /opt/eshop/backup.sh
#   sudo crontab -e
#     30 3 * * * /opt/eshop/backup.sh >> /var/log/eshop-backup.log 2>&1
#
# 备份内容与保留期（docs/16-deployment.md §5.4）：
#   PostgreSQL  每日 03:30，pg_dump -Fc，本机保留 7 天
#   Redis       AOF 持续写入；这里额外拷一份 RDB，保留 3 天
#   图片        不在本脚本内 —— 随数据盘快照走（腾讯云控制台策略）
#
# ★ 备份必须演练过才算数：上线前用最新的 .dump 在本地 pg_restore 一次，
#   确认能恢复并能启动应用（docs/16 §5.4）。
# ============================================================
set -euo pipefail

# deploy/ 目录。
#
# ★ 不给"从脚本自身位置往上推"的默认值：这个脚本会被 cron 调用，而部署时
#   往往被拷到 deploy 之外（早先文档就写的是拷到 /opt/eshop/backup.sh）——
#   那样 `dirname/..` 推出来是 /opt，找 .env 会找到 /opt/.env 上去。
#   直接把文档里约定的部署路径写成默认值，换地方用 ES_DEPLOY_DIR 覆盖。
DEPLOY_DIR="${ES_DEPLOY_DIR:-/opt/eshop/deploy}"
BACKUP_DIR="${BACKUP_DIR:-/data/backup}"

PG_KEEP_DAYS=7
REDIS_KEEP_DAYS=3

cd "$DEPLOY_DIR"

if [[ ! -f .env ]]; then
    echo "[backup] 找不到 $DEPLOY_DIR/.env" >&2
    exit 1
fi

# 只读取需要的几个变量，不 source 整个 .env
env_val() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- ; }

PG_OWNER="$(env_val POSTGRES_OWNER_USER)"; PG_OWNER="${PG_OWNER:-eshop_owner}"
PG_DB="$(env_val POSTGRES_DB)";            PG_DB="${PG_DB:-eshop}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F)"

# ---------- PostgreSQL ----------
echo "[backup] $(date '+%F %T') 开始：pg_dump $PG_DB"

pg_tmp="$BACKUP_DIR/.eshop-$STAMP.dump.part"
pg_out="$BACKUP_DIR/eshop-$STAMP.dump"

# 先写 .part 再改名：中途失败不会留下一个看起来正常、实际截断的备份文件
cleanup() { rm -f "$pg_tmp"; }
trap cleanup EXIT

# -T：不分配 TTY（cron 里没有终端）
docker compose exec -T postgres \
    pg_dump -Fc -U "$PG_OWNER" -d "$PG_DB" > "$pg_tmp"

# 自检：pg_dump 有时会返回 0 但内容异常，用 -l 列出归档目录确认可读。
# 这一步失败就整脚本退出 —— 宁可这一天没有备份，也不要留一个坏备份
# 让人以为有得可恢复。
docker compose exec -T postgres pg_restore -l < "$pg_tmp" > /dev/null

mv "$pg_tmp" "$pg_out"
trap - EXIT
echo "[backup] 完成：$pg_out（$(du -h "$pg_out" | cut -f1)）"

# 清理超期备份
find "$BACKUP_DIR" -maxdepth 1 -name 'eshop-*.dump' -mtime "+$PG_KEEP_DAYS" -print -delete

# ---------- Redis ----------
echo "[backup] 开始：Redis RDB"

# BGSAVE 会 fork 一次写盘；等它落定再拷，避免拿到写了一半的文件
docker compose exec -T redis redis-cli BGSAVE > /dev/null

# wait 会阻塞到 RDB 写完（最多 30 秒）
for _ in $(seq 1 30); do
    if [[ "$(docker compose exec -T redis redis-cli INFO persistence \
            | tr -d '\r' | grep '^rdb_bgsave_in_progress:' | cut -d: -f2)" == "0" ]]; then
        break
    fi
    sleep 1
done

# RDB 就在宿主机绑定的 /data/redis 里，直接拷
redis_tmp="$BACKUP_DIR/.redis-$STAMP.rdb.part"
redis_out="$BACKUP_DIR/redis-$STAMP.rdb"
cp /data/redis/dump.rdb "$redis_tmp"
mv "$redis_tmp" "$redis_out"
echo "[backup] 完成：$redis_out（$(du -h "$redis_out" | cut -f1)）"

find "$BACKUP_DIR" -maxdepth 1 -name 'redis-*.rdb' -mtime "+$REDIS_KEEP_DAYS" -print -delete

echo "[backup] $(date '+%F %T') 全部完成"
