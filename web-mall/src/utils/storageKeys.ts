/**
 * 商城用到的 storage 键都从这里出，统一挂一个命名空间。
 *
 * ★ **为什么必须有命名空间**：生产上商城挂 `/`、后台挂 `/admin/`，两者**同源**，
 *   而 localStorage / sessionStorage 是按 origin 分的 —— 共用一个键就等于
 *   **共用一个登录态**：在商城登录买家 B，后台那边的商家 A 立刻被顶掉
 *   （页面上还写着 A，请求带出去的已经是 B 的令牌了）。演示时要同时演商家和买家，
 *   所以两个前端各自一个命名空间。
 * ★ 代价：两个前端互跳不再免登录（见 `utils/siblingApp.ts`），跳过去要各自登一次。
 * ★ 后台那份在 `web-admin/src/utils/storageKeys.ts`，前缀是 `eshop.admin.`。
 */
const PREFIX = 'eshop.mall.'

export const ACCESS_TOKEN_KEY = `${PREFIX}accessToken`
export const REFRESH_TOKEN_KEY = `${PREFIX}refreshToken`
/** 登录页「记住手机号」 */
export const REMEMBERED_PHONE_KEY = `${PREFIX}rememberedPhone`
/** 只用于 sessionStorage：发布后让旧标签页自己刷新的冷却时间戳 */
export const CHUNK_RELOAD_KEY = `${PREFIX}chunkReloadAt`
/** ★ 要与 `index.html` 里那段内联脚本里的字符串一致（它够不到这个模块） */
export const THEME_KEY = `${PREFIX}theme`
