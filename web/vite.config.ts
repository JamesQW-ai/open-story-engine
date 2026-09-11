import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 默认代理到本机 API；切换端口示例：STORY_API_PORT=8001 npm run dev
const apiTarget = `http://127.0.0.1:${process.env.STORY_API_PORT ?? '8000'}`

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
    },
  },
})
