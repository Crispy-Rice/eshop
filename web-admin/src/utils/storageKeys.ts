/**
 * 后台用到的 storage 键都从这里出，统一挂一个命名空间。
 *
 * ★ **为什么必须有命名空间**：生产上商城挂 `/`、后台挂 `/admin/`，两者**同源**，
 *   而 localStorage 是按 origin 分的 —— 共用一个键就等于**共用一个登录态**：
 *   在商城登录买家 B，后台这边的商家 A 立刻被顶掉（页面上还写着 A，
 *   请求带出去的已经是 B 的令牌了）。演示时要同时演商家和买家，所以各自一个命名空间。
 * ★ 代价：两个前端互跳不再免登录（见 `utils/siblingApp.ts`），跳过去要各自登一次。
 * ★ 商城那份在 `web-mall/src/utils/storageKeys.ts`，前缀是 `eshop.mall.`。
 */
const PREFIX = 'eshop.admin.'

export const ACCESS_TOKEN_KEY = `${PREFIX}accessToken`
export const REFRESH_TOKEN_KEY = `${PREFIX}refreshToken`
/** 登录页「记住手机号」 */
export const REMEMBERED_PHONE_KEY = `${PREFIX}rememberedPhone`
/** 只用于 sessionStorage：发布后让旧标签页自己刷新的冷却时间戳 */
export const CHUNK_RELOAD_KEY = `${PREFIX}chunkReloadAt`
