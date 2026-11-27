import { useEffect, useState } from 'react'
import { api } from './api/client'
import OcrUploader from './components/OcrUploader'

export default function App() {
  const [status, setStatus] = useState('checking...')

  useEffect(() => {
    api.health()
      .then((d) => setStatus(d.status))
      .catch(() => setStatus('backend 연결 실패'))
  }, [])

  return (
    <main className="container">
      <h1>OCR 텍스트 추출</h1>
      <p className="status">Backend status: <strong>{status}</strong></p>
      <OcrUploader />
    </main>
  )
}
