/**
 * 把登录凭据交给**浏览器自带的密码管理器**保存。
 *
 * 浏览器本来就会在表单提交后自己判断"要不要问用户存密码"，但那是个启发式：
 * SPA 用 XHR 登录、不产生整页跳转时经常不触发。这个 API 是直接告诉浏览器
 * 「这次登录成功了，存下它」，比等它猜可靠。
 *
 * ★ 只在**安全上下文**（https / localhost）下存在。演示环境是 `http://IP:端口`
 *   直连，`navigator.credentials` 压根不存在 —— 这时静默跳过。那种环境下浏览器
 *   自己也不提供保存密码，不是我们的 bug。
 *
 * ★ 只把手机号 + 密码交给浏览器，我们自己**不留副本**：密码始终由系统钥匙串保管。
 *   任何失败都吞掉 —— 存密码失败不能影响用户已经成功的登录。
 *
 * 商城那边是同一份实现（两个前端各自打包，工具函数按既有惯例各存一份）。
 */
export function rememberPasswordInBrowser(phone: string, password: string): void {
  // 用鸭子类型而不是直接引用 PasswordCredential：这个类型不一定在 TS 的 lib 里，
  // 而且旧浏览器（Firefox/Safari）根本没有这个全局。
  const globals = globalThis as unknown as Record<string, unknown>
  const Ctor = globals.PasswordCredential as
    | (new (data: { id: string; password: string; name?: string }) => Credential)
    | undefined
  const store = navigator.credentials?.store?.bind(navigator.credentials)
  if (!Ctor || !store) return

  try {
    void store(new Ctor({ id: phone, password, name: phone })).catch(() => {})
  } catch {
    // 有些实现对参数挑剔会直接抛，不影响登录
  }
}
