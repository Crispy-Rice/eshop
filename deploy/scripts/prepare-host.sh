#!/usr/bin/env bash
# ============================================================
# 宿主机准备：建数据目录、对齐属主
#
# 用法（在 deploy/ 目录下，服务器上执行，需要 sudo）：
#   sudo ./scripts/prepare-host.sh
#
# 为什么需要它：compose 里的 volume 全是宿主机绝对路径绑定
# （/data/pgdata、/data/redis、/data/media）。Docker 不会替你把属主设对 ——
# 目录属主不是容器内的运行用户时，PG 会拒绝启动，或图片写不进去。
#
# 属主对照：
#   /data/pgdata  999   postgres 镜像里的 postgres 用户
#   /data/redis   999   redis 镜像里的 redis 用户
#   /data/media   10001 backend/Dockerfile 里建的 eshop 用户（api/worker 以它运行）
#   /data/backup  root  只有备份脚本（cron，root）需要写
# ============================================================
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "需要 root：sudo $0" >&2
    exit 1
fi

# 各容器内的运行用户 id。
# ★ 这几个数字是对着 postgres:17.6 / redis:7.4-alpine / 本项目镜像实测的，
#   不是猜的 —— 注意 redis 的 **gid 是 1000 不是 999**，两者不一样。
# ★ 只认数字，不认名字：这台机器上 uid 999 恰好被 caddy 占着，`ls -l` 会
#   把 /data/pgdata 显示成 caddy 所有 —— 那只是宿主机的名字映射，内核按数字
#   判权，在容器里它就是 postgres 用户。看到 caddy 不要慌。
PG_UID=999;    PG_GID=999       # postgres 镜像
REDIS_UID=999; REDIS_GID=1000   # redis:alpine
APP_UID=10001; APP_GID=10001    # backend/Dockerfile 里建的 eshop 用户

echo "[prepare-host] 数据目录根：$DATA_ROOT"

# /data 应当是独立挂载的数据盘（docs/16 §5.1：数据与系统分离，便于快照）。
# 不是挂载点也不影响运行，但快照策略会覆盖不到，值得提醒一句。
if ! mountpoint -q "$DATA_ROOT"; then
    echo "[prepare-host] ⚠ $DATA_ROOT 不是挂载点 —— 数据会和系统盘一起丢。"
    echo "                演示环境可以接受，正式环境请挂云硬盘并写入 /etc/fstab。"
fi

mkdir -p "$DATA_ROOT"/{pgdata,redis,media,backup}

chown -R "${PG_UID}:${PG_GID}"        "$DATA_ROOT/pgdata"
chown -R "${REDIS_UID}:${REDIS_GID}"  "$DATA_ROOT/redis"
chown -R "${APP_UID}:${APP_GID}"      "$DATA_ROOT/media"

# 备份目录只有 root 的 cron 要写
chown root:root "$DATA_ROOT/backup"
chmod 700       "$DATA_ROOT/backup"

# 权限：数据目录不给 group/other 任何权限；media 要给 nginx（root master）之外的
# 读取能力，但只需要同组或属主读。nginx 以 root 运行 master，实际读得到。
chmod 700 "$DATA_ROOT/pgdata"
chmod 700 "$DATA_ROOT/redis"
chmod 755 "$DATA_ROOT/media"

echo "[prepare-host] 目录就绪："
ls -ld "$DATA_ROOT"/{pgdata,redis,media,backup}

# ---------- 内存告警 ----------
MEM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
SWAP_MB="$(awk '/SwapTotal/ {printf "%d", $2/1024}' /proc/meminfo)"

echo "[prepare-host] 内存 ${MEM_MB}MB / swap ${SWAP_MB}MB"

if [[ "$MEM_MB" -lt 4096 ]]; then
    echo
    echo "★ 内存不足 4GB。按 docs/16 §5.5 的结论，这台机器只能用于演示，"
    echo "  必须叠加 docker-compose.demo.yml 使用，并加 2G swap："
    echo
    echo "    sudo fallocate -l 2G /swapfile"
    echo "    sudo chmod 600 /swapfile"
    echo "    sudo mkswap /swapfile"
    echo "    sudo swapon /swapfile"
    echo "    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab"
    echo "    echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-eshop.conf"
    echo "    sudo sysctl -p /etc/sysctl.d/99-eshop.conf"
    echo
    echo "  另外：部署前请先停掉这台机器上的其他应用，否则 PG 会被 OOM Killer 干掉。"
fi

echo "[prepare-host] 完成"
