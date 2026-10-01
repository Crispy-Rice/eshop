import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

const BACKEND = 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
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
})
