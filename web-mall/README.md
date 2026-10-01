# web-mall · 买家 PC 商城

Vue 3 + Vite + TypeScript + Pinia + Element Plus。

**完整的启动步骤见仓库根目录的 [README](../README.md)** —— 那里写了终端选择、依赖安装、数据库准备和演示数据。

简单说：

```bash
cd web-mall; npm.cmd run dev
```

跑在 http://localhost:5173，通过 Vite 把 `/api` 代理到 http://127.0.0.1:8000。

> Windows 上写 `npm.cmd` 是为了绕过 PowerShell 的脚本执行策略（它会拦截 `npm.ps1`）。
> 在 Git Bash 或 cmd 里写 `npm` 即可。

## 目录

```
src/
├── api/          # 接口层：errors（错误码镜像）/ http（axios 封装）/ auth / product / system
├── stores/       # Pinia：auth（令牌与登录态）
├── router/       # 路由与守卫（requiresAuth / guestOnly）
├── utils/        # money（分↔元）、placeholder（图片占位）
└── views/        # 登录、商品列表、商品详情（含规格选择器）、我的、系统状态
```

## 脚本

| 命令 | 作用 |
|---|---|
| `npm.cmd run dev` | 开发服务器（5173） |
| `npm.cmd run build` | 生产构建 |
| `npm.cmd run type-check` | vue-tsc 类型检查 |
| `npm.cmd run lint` | oxlint + eslint |
| `npm.cmd run format` | prettier 格式化 |
