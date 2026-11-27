import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// [규칙 4] 포트 같은 설정도 .env 에서 읽는다.
// loadEnv: vite.config.js 는 브라우저 코드가 아니라서 import.meta.env 를 못 쓰므로, .env 를 직접 읽어 오는 함수
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  return {
    plugins: [react()],
    server: {
      port: Number(env.VITE_DEV_PORT),
      strictPort: true, // 포트가 사용 중이면 다른 포트로 몰래 바꾸지 않고 오류를 낸다 (backend CORS 허용 주소와 어긋나지 않게)
    },
  }
})
