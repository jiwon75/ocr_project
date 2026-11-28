/**
 * [이 파일이 하는 일]
 * 화면 전체의 흐름을 관리한다:  업로드 → 진행 상태(1초마다 확인) → 결과 화면
 *
 * [왜 이렇게 나눴는지]
 * "지금 어느 화면인지, 어떤 문서인지" 같은 상태는 이 파일 한 곳에서만 들고 있고,
 * 아래 컴포넌트(UploadPanel/ProgressPanel/ResultView)는 받은 값을 그리기만 한다.
 * 상태가 여러 곳에 흩어지면 서로 어긋나는 버그가 생기기 쉽기 때문이다.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { STATUS_POLL_MS } from '../config'
import ProgressPanel from '../components/ProgressPanel'
import ResultView from '../components/ResultView'
import UploadPanel from '../components/UploadPanel'

export default function DocumentPage({ serverReady }) {
  const [docId, setDocId] = useState(null)
  const [status, setStatus] = useState(null)
  const [data, setData] = useState(null) // 블록 목록 (결과 화면용)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')

  const reset = () => {
    setDocId(null)
    setStatus(null)
    setData(null)
    setError('')
  }

  const upload = async (file, language) => {
    setUploading(true)
    try {
      const res = await api.upload(file, language)
      setDocId(res.doc_id)
      setStatus({ doc_id: res.doc_id, file_name: res.file_name, status: 'queued', progress: { done: 0, total: 0, percent: 0 } })
    } finally {
      setUploading(false)
    }
  }

  const loadBlocks = useCallback(async (id) => setData(await api.blocks(id)), [])

  // 진행 상태 폴링(일정 간격으로 물어보기). 끝나거나 실패하면 멈춘다
  useEffect(() => {
    if (!docId || !status || ['done', 'failed'].includes(status.status)) return
    const timer = setTimeout(async () => {
      try {
        const s = await api.status(docId)
        setStatus(s)
        if (s.status === 'done') await loadBlocks(docId)
      } catch (err) {
        setError(err.message)
      }
    }, STATUS_POLL_MS)
    return () => clearTimeout(timer)
  }, [docId, status, loadBlocks])

  /** 블록 재시도: 요청 후 그 블록이 pending 에서 벗어날 때까지 기다렸다가 목록을 새로 받는다 */
  const retry = async (blockId) => {
    await api.retry(docId, blockId)
    setData((d) => ({ ...d, blocks: d.blocks.map((b) => (b.block_id === blockId ? { ...b, status: 'pending' } : b)) }))
    for (;;) {
      await new Promise((r) => setTimeout(r, STATUS_POLL_MS))
      const b = await api.block(docId, blockId)
      if (b.status !== 'pending') break
    }
    await loadBlocks(docId)
  }

  const edit = async (blockId, editedText) => {
    await api.edit(docId, blockId, editedText)
    await loadBlocks(docId)
  }

  return (
    <>
      {!docId && <UploadPanel onUpload={upload} busy={uploading} serverReady={serverReady} />}
      {docId && status && status.status !== 'done' && <ProgressPanel status={status} onReset={reset} />}
      {docId && status?.status === 'done' && data && (
        <ResultView docId={docId} data={data} fileName={status.file_name} onRetry={retry} onEdit={edit} onReset={reset} onReload={() => loadBlocks(docId)} />
      )}
      {error && <p className="error">{error}</p>}
    </>
  )
}
