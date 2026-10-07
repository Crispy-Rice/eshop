/**
 * 买家商城的地址。
 *
 * 生产环境两个前端**同源**：商城挂 `/`，后台挂 `/admin/`（deploy/nginx/nginx.conf）。
 * 但登录态**各存各的** —— 同源意味着 localStorage 是共享的，共用一个键就会两边串号
 * （在商城登录买家 B，后台这边的商家 A 立刻被顶掉），而演示时要能一边演商家
 * 一边演买家，所以键名各带一个命名空间（`utils/storageKeys.ts`）。
 *
 * ★ 代价：点过去**要各自登录一次**。这是取舍，不是 bug —— 别把它"修"回共用键。
 *
 * 开发环境本来就是两个端口（商城 5173、后台 5174）、两个 origin，表现一致。
 * 主机名取 `location.hostname` 而不是写死 localhost，这样用局域网 IP 演示也能跳。
 */
export const MALL_APP_URL = import.meta.env.DEV ? `//${location.hostname}:5173/` : '/'
