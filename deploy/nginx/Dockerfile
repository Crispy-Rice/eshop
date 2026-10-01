# syntax=docker/dockerfile:1.7
# ============================================================
# 前端静态资源 + nginx 反向代理
#
# 构建（在仓库根目录，context 必须是根目录）：
#   docker build -f deploy/nginx/Dockerfile -t eshop-nginx:v0.1.0 .
#
# 产物布局（与 nginx.conf 的路由一一对应）：
#   web-mall  dist  → /usr/share/nginx/html         （挂在 /）
#   web-admin dist  → /usr/share/nginx/html/admin   （挂在 /admin/）
#
# 后台的 base 由 web-admin/vite.config.ts 在 build 时自动设为 /admin/，
# 这里不需要额外传环境变量。
# ============================================================

ARG NODE_IMAGE=node:24-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine

# ============================================================
# 商城
# ============================================================
FROM ${NODE_IMAGE} AS build-mall

WORKDIR /build
# 先只拷清单：改源码不会让 npm ci 这层失效
COPY web-mall/package.json web-mall/package-lock.json ./

# 国内服务器若直连 npm 慢，用 --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN npm ci --no-audit --no-fund --registry="${NPM_REGISTRY}"

COPY web-mall/ ./
# 用 build-only（只有 vite build）。类型检查是提交前的门禁，
# 放在镜像构建里会让 2C2G 的演示机多跑一遍 vue-tsc，性价比很低。
RUN npm run build-only

# ============================================================
# 商家 / 运营后台
# ============================================================
FROM ${NODE_IMAGE} AS build-admin

WORKDIR /build
COPY web-admin/package.json web-admin/package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN npm ci --no-audit --no-fund --registry="${NPM_REGISTRY}"

COPY web-admin/ ./
RUN npm run build-only

# ============================================================
# 运行：nginx
# ============================================================
FROM ${NGINX_IMAGE}

# 用自带的 nginx.conf 整体替换（里面显式 include snippets/，
# 不 include conf.d/，所以随镜像附带的 default.conf 不会生效）
COPY deploy/nginx/nginx.conf          /etc/nginx/nginx.conf
COPY deploy/nginx/snippets/           /etc/nginx/snippets/

COPY --from=build-mall  /build/dist   /usr/share/nginx/html
COPY --from=build-admin /build/dist   /usr/share/nginx/html/admin

# 顺手删掉镜像自带的 server 块，避免以后有人改回 include conf.d 时踩到
RUN rm -f /etc/nginx/conf.d/default.conf

EXPOSE 80

# nginx:alpine 自带的 CMD 就是 nginx -g 'daemon off;'，这里不覆盖
