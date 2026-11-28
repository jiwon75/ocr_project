/**
 * [이 파일이 하는 일]
 * 화면 맨 위 머리줄(서버 준비 상태 표시) + 본문(DocumentPage).
 * 서버 health 를 주기적으로 확인해서, OCR 모델이 준비됐는지 사용자에게 알려 준다.
 */
import { useState } from 'react'
// import { useEffect } from 'react'
// import { api } from './api/client'
// import { HEALTH_POLL_MS } from './config'
import { CONFIG_ERRORS } from './config'
import DocumentPage from './pages/DocumentPage'

export default function App() {
  // [health 체크 주석 처리] 서버 상태를 주기적으로 묻지 않는다. 준비된 것으로 보고 화면을 띄운다.
  // 다시 쓰려면 아래 초기값을 { ready: false, offline: false } 로 되돌리고 useEffect 주석을 풀면 된다.
  const [health] = useState({ ready: true, offline: false })

  // useEffect(() => {
  //   if (CONFIG_ERRORS.length) return
  //   let stopped = false
  //   const check = async () => {
  //     const h = await api.health()
  //     if (!stopped) setHealth(h)
  //   }
  //   check()
  //   const timer = setInterval(check, HEALTH_POLL_MS)
  //   return () => {
  //     stopped = true
  //     clearInterval(timer)
  //   }
  // }, [])

  if (CONFIG_ERRORS.length) {
    return (
      <main className="container">
        <p className="error">
          frontend/.env 설정이 없습니다: {CONFIG_ERRORS.join(', ')}
          <br />
          frontend 폴더에서 <code>copy .env.example .env</code> 를 실행한 뒤 개발 서버를 다시 시작하세요.
        </p>
      </main>
    )
  }

  // const label = health.offline ? '서버 연결 안 됨' : health.ready ? 'OCR 준비 완료' : 'OCR 모델 불러오는 중...'
  return (
    <main className="container">
      <header className="app-header">
        <h1>문서 OCR</h1>
        {/* <span className={`health ${health.ready ? 'ok' : health.offline ? 'down' : 'wait'}`}>● {label}</span> */}
      </header>
      <DocumentPage serverReady={health.ready} />
    </main>
  )
}
