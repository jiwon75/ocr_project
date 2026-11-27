/**
 * [이 파일이 하는 일]
 * 파일 1개를 고르고(드래그 앤 드롭 또는 클릭), 언어를 고른 뒤 업로드 버튼을 누르는 화면.
 *
 * 업로드 전에 브라우저에서 먼저 검사한다: 파일 개수(1개), 확장자, 크기(VITE_MAX_FILE_SIZE_MB).
 * 서버도 똑같이 검사하지만, 5MB 넘는 파일을 다 올린 뒤에 거절당하면 사용자가 기다린 시간이 아깝기 때문에 먼저 막는다.
 */
import { useState } from 'react'
import { ACCEPT_EXTENSIONS, LANGUAGES, MAX_FILE_SIZE_MB } from '../config'

export default function UploadPanel({ onUpload, busy, serverReady }) {
  const [file, setFile] = useState(null)
  const [language, setLanguage] = useState('auto') // 기본값: 자동
  const [message, setMessage] = useState('')
  const [dragging, setDragging] = useState(false)

  /** 파일 목록을 검사해서 통과하면 선택, 아니면 안내 문장을 띄운다. 돌려주는 값 없음 */
  const pick = (fileList) => {
    setMessage('')
    if (!fileList || fileList.length === 0) return
    if (fileList.length > 1) {
      setFile(null)
      setMessage('한 번에 파일 1개만 업로드할 수 있습니다.')
      return
    }
    const f = fileList[0]
    const ext = f.name.includes('.') ? f.name.split('.').pop().toLowerCase() : ''
    if (!ACCEPT_EXTENSIONS.includes(ext)) {
      setFile(null)
      setMessage(`지원하지 않는 형식입니다 (.${ext || '없음'}). ${ACCEPT_EXTENSIONS.join(', ')} 파일만 가능합니다.`)
      return
    }
    // [규칙 4] 기준값은 .env 의 VITE_MAX_FILE_SIZE_MB 에서 온다
    if (f.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      setFile(null)
      setMessage(`파일 크기는 ${MAX_FILE_SIZE_MB}MB 이하만 가능합니다. (선택한 파일: ${(f.size / 1024 / 1024).toFixed(1)}MB)`)
      return
    }
    setFile(f)
  }

  const submit = async () => {
    if (!file) return
    try {
      await onUpload(file, language)
    } catch (err) {
      setMessage(err.message)
    }
  }

  return (
    <section className="card upload">
      <label
        className={`dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault() // 이걸 막지 않으면 브라우저가 파일을 새 탭에서 열어 버린다
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          pick(e.dataTransfer.files)
        }}
      >
        {/* multiple 을 주지 않아 파일 선택 창에서도 1개만 고를 수 있다 */}
        <input type="file" hidden accept={ACCEPT_EXTENSIONS.map((e) => '.' + e).join(',')} onChange={(e) => pick(e.target.files)} />
        {file ? (
          <span>
            <strong>{file.name}</strong> ({(file.size / 1024).toFixed(0)} KB)
          </span>
        ) : (
          <span>
            파일을 끌어다 놓거나 클릭해서 선택하세요
            <br />
            <small>
              {ACCEPT_EXTENSIONS.join(', ')} · 최대 {MAX_FILE_SIZE_MB}MB · 1개
            </small>
          </span>
        )}
      </label>

      <div className="controls">
        <label>
          언어&nbsp;
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            {LANGUAGES.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
        </label>
        <button className="primary" onClick={submit} disabled={!file || busy}>
          {busy ? '업로드 중...' : '업로드 후 분석'}
        </button>
      </div>
      {!serverReady && <p className="hint">OCR 모델을 불러오는 중입니다. 지금 올리면 준비가 끝난 뒤 처리됩니다.</p>}
      {message && <p className="error">{message}</p>}
    </section>
  )
}
