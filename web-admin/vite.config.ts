import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

const BACKEND = 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  // 后台在生产环境挂在 /admin/ 下（docs/16-deployment.md §4.2），所以构建时
  // base 要跟着变 —— 否则 index.html 里的资源全是绝对路径 /assets/...，
  // 在 /admin/ 下会 404。
  //
  // 开发期仍然用 '/'：改成本地 dev server 地址会变成 localhost:5174/admin/，
  // 现有工作流（含 .claude/launch.json）就断了，没必要。
  base: command === 'build' ? (process.env.VITE_BASE ?? '/admin/') : '/',
  plugins: [vue(), vueDevTools()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 与商城的 5173 区分开，两个前端可以同时跑
    port: 5174,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/media': { target: BACKEND, changeOrigin: true },
      '/healthz': { target: BACKEND, changeOrigin: true },
      '/readyz': { target: BACKEND, changeOrigin: true },
    },
  },
}))
