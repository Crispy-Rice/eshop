import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

// https://vite.dev/config/
const BACKEND = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue(), vueDevTools()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 开发期把 /api 与 /media 代理到本地 FastAPI。
      // 前端与 API 始终同源，所以生产环境也不需要开 CORS（docs/15 §4）。
      '/api': { target: BACKEND, changeOrigin: true },
      '/media': { target: BACKEND, changeOrigin: true },
      // 健康检查在后端根路径下（/healthz、/readyz），一并代理
      '/healthz': { target: BACKEND, changeOrigin: true },
      '/readyz': { target: BACKEND, changeOrigin: true },
    },
  },
})
