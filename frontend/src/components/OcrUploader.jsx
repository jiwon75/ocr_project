import { useEffect, useState } from 'react'
import { api } from '../api/client'

const ACCEPT = '.pdf,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp'
const LANGS = [
  { value: 'kor+eng', label: '한국어 + 영어' },
  { value: 'kor', label: '한국어' },
  { value: 'eng', label: '영어' },
]

export default function OcrUploader() {
  const [file, setFile] = useState(null)
  const [preview, setPreview] = useState(null)
  const [lang, setLang] = useState('kor+eng')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [dragging, setDragging] = useState(false)
  const [copied, setCopied] = useState(false)

  // 이미지 미리보기 URL 생성/해제
  useEffect(() => {
    if (!file || !file.type.startsWith('image/')) return setPreview(null)
    const url = URL.createObjectURL(file)
    setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const selectFile = (f) => {
    if (!f) return
    setFile(f)
    setResult(null)
    setError('')
  }

  const onDrop = (e) => {
    e.preventDefault()
    setDragging(false)
    selectFile(e.dataTransfer.files[0])
  }

  const runOcr = async () => {
    if (!file) return
    setLoading(true)
    setError('')
    setResult(null)
    try {
      setResult(await api.ocr(file, lang))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const copyText = async () => {
    await navigator.clipboard.writeText(result.text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const downloadText = () => {
    const blob = new Blob([result.text], { type: 'text/plain;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = result.filename.replace(/\.[^.]+$/, '') + '.txt'
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <section className="ocr">
      <label
        className={`dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input type="file" accept={ACCEPT} hidden onChange={(e) => selectFile(e.target.files[0])} />
        {file ? (
          <span><strong>{file.name}</strong> ({(file.size / 1024).toFixed(0)} KB)</span>
        ) : (
          <span>이미지 또는 PDF 파일을 끌어다 놓거나 클릭해서 선택하세요</span>
        )}
      </label>

      {preview && <img className="preview" src={preview} alt="미리보기" />}

      <div className="controls">
        <select value={lang} onChange={(e) => setLang(e.target.value)}>
          {LANGS.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
        </select>
        <button onClick={runOcr} disabled={!file || loading}>
          {loading ? '텍스트 추출 중...' : '텍스트 추출'}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {result && (
        <div className="result">
          <div className="result-header">
            <span>
              {result.page_count}페이지
              {result.pages.some((p) => p.method === 'text-layer') && ' · PDF 내장 텍스트 사용'}
            </span>
            <div>
              <button onClick={copyText}>{copied ? '복사됨' : '복사'}</button>
              <button onClick={downloadText}>.txt 저장</button>
            </div>
          </div>
          <textarea readOnly value={result.text || '(인식된 텍스트가 없습니다)'} rows={16} />
        </div>
      )}
    </section>
  )
}
