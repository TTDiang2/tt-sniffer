import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// P0 脚手架：构建产物 dist/ 提交入库，FastAPI 直接托管（run.bat 零心智）。
// dev 代理默认打隔离实例 8788（勿动生产 8787）；可用 TTS_DEV_PROXY 覆盖。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    proxy: {
      '/api': {
        target: process.env.TTS_DEV_PROXY || 'http://127.0.0.1:8788',
        changeOrigin: true,
      },
      '/static': {
        target: process.env.TTS_DEV_PROXY || 'http://127.0.0.1:8788',
        changeOrigin: true,
      },
    },
  },
})
