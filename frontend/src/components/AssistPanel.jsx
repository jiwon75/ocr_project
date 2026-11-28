/**
 * [이 파일이 하는 일]
 * 결과 화면 오른쪽 맨 위의 "AI 도우미"(Ollama LLM) 상자: 문서 요약 + OCR 오타 교정 제안.
 *
 * - 선택 기능이다. Ollama 가 꺼져 있거나 모델이 없으면 이유만 보여 주고 버튼을 막는다. OCR 결과 화면은 그대로 쓸 수 있다.
 * - [규칙 2] 교정은 "제안"만 보여 준다. 사용자가 체크해서 [적용] 한 것만 edited_text 로 저장된다 (원본은 그대로, "원래대로"로 되돌릴 수 있음).
 * - LLM 답변은 오래 걸려서(첫 요청은 모델 로딩 때문에 1~3분) 시작 요청 후 STATUS_POLL_MS 마다 결과를 확인한다.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { STATUS_POLL_MS } from '../config'

const isRunning = (job) => job?.state === 'running'

export default function AssistPanel({ docId, onApplied, onSelectBlock }) {
  const [info, setInfo] = useState(null) // Ollama 상태
  const [jobs, setJobs] = useState({ summary: null, corrections: null })
  const [checked, setChecked] = useState(new Set())
  const [open, setOpen] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const usable = info?.reachable && info?.model_available

  const refresh = useCallback(async () => setJobs(await api.assist(docId)), [docId])

  useEffect(() => {
    api.assistStatus().then(setInfo).catch(() => setInfo({ message: 'LLM 도우미 상태를 확인하지 못했습니다.' }))
    refresh().catch(() => {})
  }, [refresh])

  // 실행 중인 작업이 있으면 끝날 때까지 확인한다
  useEffect(() => {
    if (!isRunning(jobs.summary) && !isRunning(jobs.corrections)) return
    const timer = setTimeout(() => refresh().catch((err) => setError(err.message)), STATUS_POLL_MS)
    return () => clearTimeout(timer)
  }, [jobs, refresh])

  const start = async (kind) => {
    setError('')
    setNotice('')
    try {
      const job = await api.startAssist(docId, kind)
      setJobs((j) => ({ ...j, [kind]: job }))
      if (kind === 'corrections') setChecked(new Set())
    } catch (err) {
      setError(err.message)
    }
  }

  const suggestions = jobs.corrections?.state === 'done' ? jobs.corrections.suggestions ?? [] : []
  const pendingSuggestions = suggestions.filter((s) => !s.applied)

  const toggle = (sid) =>
    setChecked((prev) => {
      const next = new Set(prev)
      next.has(sid) ? next.delete(sid) : next.add(sid)
      return next
    })

  const apply = async () => {
    setBusy(true)
    setError('')
    try {
      const res = await api.applyCorrections(docId, [...checked])
      setJobs((j) => ({ ...j, corrections: res.corrections }))
      setChecked(new Set())
      setNotice(
        `${res.applied.length}건 적용했습니다` +
          (res.skipped.length ? ` (${res.skipped.length}건은 그사이 글자가 바뀌어 건너뜀)` : ''),
      )
      await onApplied()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const summary = jobs.summary
  const corrections = jobs.corrections

  return (
    <section className="assist card">
      <header className="row">
        <strong className="grow">AI 도우미 {info?.model && <small className="muted">({info.model})</small>}</strong>
        <button className="link" onClick={() => setOpen((v) => !v)}>
          {open ? '접기' : '펼치기'}
        </button>
      </header>

      {open && (
        <>
          {info && !usable && <p className="assist-off">{info.message}</p>}
          <div className="row">
            <button onClick={() => start('summary')} disabled={!usable || isRunning(summary)}>
              {isRunning(summary) ? '요약 중...' : summary?.state === 'done' ? '요약 다시 만들기' : '요약 만들기'}
            </button>
            <button onClick={() => start('corrections')} disabled={!usable || isRunning(corrections)}>
              {isRunning(corrections) ? '오타 찾는 중...' : '오타 교정 제안'}
            </button>
            {(isRunning(summary) || isRunning(corrections)) && (
              <small className="muted">처음 실행은 모델을 불러오느라 1~3분 걸릴 수 있습니다</small>
            )}
          </div>

          {summary?.state === 'failed' && <p className="error">요약 실패: {summary.error}</p>}
          {summary?.state === 'done' && (
            <div className="assist-summary">
              <p>{summary.summary || '(요약할 글자가 없습니다)'}</p>
              {summary.chunks > 1 && <small className="muted">긴 문서라 {summary.chunks}부분으로 나눠 요약했습니다</small>}
            </div>
          )}

          {corrections?.state === 'failed' && <p className="error">교정 제안 실패: {corrections.error}</p>}
          {corrections?.state === 'done' && (
            <div className="assist-corrections">
              {suggestions.length === 0 ? (
                <p className="muted">고칠 만한 오타를 찾지 못했습니다.</p>
              ) : (
                <>
                  <ul>
                    {suggestions.map((s) => (
                      <li key={s.sid} className={s.applied ? 'applied' : ''}>
                        <label>
                          <input
                            type="checkbox"
                            disabled={s.applied}
                            checked={s.applied || checked.has(s.sid)}
                            onChange={() => toggle(s.sid)}
                          />
                          <span className="fix">
                            <del>{s.wrong}</del> → <ins>{s.right}</ins>
                          </span>
                        </label>
                        <button className="link" onClick={() => onSelectBlock(s.block_id)} title="이 블록으로 이동">
                          {s.id}
                        </button>
                        {s.applied && <span className="badge edited">적용됨</span>}
                        {s.reason && <small className="muted reason">{s.reason}</small>}
                      </li>
                    ))}
                  </ul>
                  <div className="row">
                    <button
                      onClick={() =>
                        setChecked(checked.size === pendingSuggestions.length ? new Set() : new Set(pendingSuggestions.map((s) => s.sid)))
                      }
                      disabled={pendingSuggestions.length === 0}
                    >
                      {checked.size === pendingSuggestions.length && checked.size > 0 ? '선택 해제' : '모두 선택'}
                    </button>
                    <button className="primary" onClick={apply} disabled={busy || checked.size === 0}>
                      선택한 {checked.size}건 적용
                    </button>
                  </div>
                </>
              )}
              {corrections.rejected > 0 && (
                <small className="muted">
                  원래 글자와 너무 다르거나 숫자 자릿수를 바꾸는 제안 {corrections.rejected}건은 자동으로 뺐습니다
                </small>
              )}
            </div>
          )}

          {notice && <p className="notice">{notice}</p>}
          {error && <p className="error">{error}</p>}
        </>
      )}
    </section>
  )
}
