# web-admin · 商家 / 运营后台

Vue 3 + Vite + TypeScript + Pinia + Element Plus。商家、平台运营、平台财务共用一套，按角色区分菜单与接口权限。

**完整的启动步骤见仓库根目录的 [README](../README.md)**。

简单说（先确保后端和数据库已启动）：

```bash
cd web-admin; npm.cmd run dev
```

跑在 http://localhost:5174，通过 Vite 把 `/api` 代理到 http://127.0.0.1:8000。

> Windows 上写 `npm.cmd` 是为了绕过 PowerShell 的脚本执行策略（它会拦截 `npm.ps1`）。
> 在 Git Bash 或 cmd 里写 `npm` 即可。

## 目录

```
src/
├── api/          # 接口层：errors / http / auth / product（含商家端接口）/ system
├── stores/       # Pinia：auth
├── router/       # 路由与守卫
├── utils/        # money（分↔元）、placeholder（图片占位）
└── views/        # 登录、我的商品、发布商品（规格编辑器）、编辑商品、系统状态
```

## 脚本

| 命令 | 作用 |
|---|---|
| `npm.cmd run dev` | 开发服务器（5174） |
| `npm.cmd run build` | 生产构建 |
| `npm.cmd run type-check` | vue-tsc 类型检查 |
| `npm.cmd run lint` | oxlint + eslint |
| `npm.cmd run format` | prettier 格式化 |
