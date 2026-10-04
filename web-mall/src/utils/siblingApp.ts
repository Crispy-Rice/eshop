/**
 * 商家 / 运营后台的地址。
 *
 * 生产环境两个前端**同源**：商城挂 `/`，后台挂 `/admin/`
 * （deploy/nginx/nginx.conf）。同源带来一个有用的副作用 —— 两个前端用的是
 * 同一对 localStorage key（`eshop.accessToken`），所以从商城点过去**不用重新登录**。
 *
 * 开发环境是两个端口（商城 5173、后台 5174），属于不同 origin，
 * localStorage 不共享，跳过去是要重新登录的 —— 这是浏览器的同源策略，
 * 不是 bug。主机名取 `location.hostname` 而不是写死 localhost，
 * 这样用局域网 IP 打开演示时也能跳。
 */
export const ADMIN_APP_URL = import.meta.env.DEV
  ? `//${location.hostname}:5174/`
  : '/admin/'
