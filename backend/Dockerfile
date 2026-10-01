# syntax=docker/dockerfile:1.7
# ============================================================
# 后端镜像 —— api / worker / migrate 三个服务共用同一个镜像，
# 只是启动命令不同（见 deploy/docker-compose.yml）：
#
#   api      uvicorn app.main:app --host 0.0.0.0 --port 8000
#   worker   arq app.worker.main.WorkerSettings
#   migrate  alembic upgrade head（一次性，跑完退出）
#
# 构建（在仓库根目录）：
#   docker build -f backend/Dockerfile -t eshop-api:v0.1.0 backend
#
# 依赖锁定：用 uv.lock，不用 requirements.txt。所以镜像里要有一个 uv，
# 而 uv 的官方镜像在 ghcr.io —— 国内服务器可能拉不动，此时用 build-arg 换源：
#   --build-arg UV_IMAGE=<可达的 uv 镜像>
# 或改用 pip 装 uv（把下面 COPY --from=uv 换成
#   RUN pip install --no-cache-dir -i <腾讯云 PyPI 镜像> uv==0.12.18）
# ============================================================

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.18
# 装依赖用的 PyPI 源。国内服务器（腾讯云）直连 pypi.org 很慢，
# 换镜像能省下大量构建时间：
#   --build-arg PYPI_INDEX=https://mirrors.cloud.tencent.com/pypi/simple
ARG PYPI_INDEX=https://pypi.org/simple

FROM ${UV_IMAGE} AS uv

# ============================================================
# 依赖层：只 COPY pyproject.toml + uv.lock
# 这样改业务代码不会让这层缓存失效，重建时不用重装依赖
# ============================================================
FROM ${PYTHON_IMAGE} AS deps

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# uv 读 UV_DEFAULT_INDEX 决定去哪拉包
ARG PYPI_INDEX
ENV UV_DEFAULT_INDEX=${PYPI_INDEX}

WORKDIR /app
COPY pyproject.toml uv.lock ./

# --frozen：lock 与 pyproject 不一致就直接失败，不要在构建机上偷偷改依赖
# --no-dev：生产镜像不装 pytest / ruff / mypy
# --no-install-project：本项目是 package=false，没有 wheel 要装
RUN uv sync --frozen --no-dev --no-install-project

# ============================================================
# 运行层
# ============================================================
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# 非 root 运行。uid/gid 固定，方便宿主机对挂载目录（/data/media）的属主管理
RUN groupadd --gid 10001 eshop \
 && useradd --uid 10001 --gid 10001 --home-dir /home/eshop --create-home \
            --shell /usr/sbin/nologin eshop

COPY --from=deps /opt/venv /opt/venv

WORKDIR /app
# --chown：否则 COPY 出来的是 root 属主，非 root 进程可能读不了
COPY --chown=eshop:eshop . .

USER eshop

# 默认命令给个能直接跑的（api）；worker / migrate 由 compose 覆盖
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
