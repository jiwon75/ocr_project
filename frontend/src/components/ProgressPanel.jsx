/**
 * [이 파일이 하는 일]
 * 처리 진행 상태를 단계별로 보여 준다:  대기 → 구조 분석 → 블록 처리 n/N → 합치기 → 완료
 * 실패하면 어느 단계에서 왜 실패했는지 사용자용 문장으로 보여 준다.
 */
import { STAGE_NAMES } from '../config'

const STEPS = [
  { key: 'queued', label: '대기' },
  { key: 'analyzing', label: '구조 분석' },
  { key: 'processing', label: '블록 처리' },
  { key: 'merging', label: '합치기' },
  { key: 'done', label: '완료' },
]

export default function ProgressPanel({ status, onReset }) {
  if (!status) return null
  const failed = status.status === 'failed'
  // 실패했으면 "실패한 단계"까지를 진행된 것으로 표시한다
  const currentKey = failed ? stageToState(status.error?.stage) : status.status
  const currentIndex = STEPS.findIndex((s) => s.key === currentKey)
  const { done, total, percent } = status.progress

  return (
    <section className="card progress">
      <h2>{status.file_name}</h2>
      <ol className="steps">
        {STEPS.map((step, i) => {
          const state = i < currentIndex ? 'past' : i === currentIndex ? (failed ? 'failed' : 'current') : 'future'
          return (
            <li key={step.key} className={state}>
              {step.label}
              {step.key === 'processing' && total > 0 && ` ${done}/${total}`}
            </li>
          )
        })}
      </ol>
      {status.status === 'processing' && (
        <div className="bar">
          <div style={{ width: `${percent}%` }} />
        </div>
      )}
      {failed && (
        <div className="error-box">
          <strong>처리에 실패했습니다 — {STAGE_NAMES[status.error?.stage] ?? status.error?.stage} 단계</strong>
          <p>{status.error?.message}</p>
          <small>사유 코드: {status.error?.reason}</small>
          <div>
            <button onClick={onReset}>다른 파일 올리기</button>
          </div>
        </div>
      )}
    </section>
  )
}

/** 실패 단계 이름(preprocess/layout/...) → 진행 단계(analyzing/processing/...) */
function stageToState(stage) {
  if (stage === 'preprocess' || stage === 'layout') return 'analyzing'
  if (stage === 'process') return 'processing'
  if (stage === 'merge' || stage === 'db') return 'merging'
  return 'queued'
}
