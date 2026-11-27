import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // /api 요청을 FastAPI(8000)로 프록시
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
