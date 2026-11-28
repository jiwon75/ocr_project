/**
 * [이 파일이 하는 일]
 * 결과 화면 오른쪽의 블록 하나: 번호·종류·상태 머리줄 + 최종 텍스트 + [수정] [재시도] 버튼.
 *
 * - 텍스트는 서버가 합칠 때 쓰는 것과 똑같은 글자(rendered)를 보여 준다. 화면과 다운로드 결과가 달라지지 않게 하기 위해서다.
 * - [규칙 2] 수정은 edited_text 로만 저장된다. 원본 OCR 결과는 "원본 보기"로 언제든 확인할 수 있다.
 */
import { useState } from 'react'
import { BLOCK_TYPES, FAILURE_REASONS } from '../config'

/** [규칙 2] 서버(merge.py)와 같은 실패 표시 */
const failMark = (blockId, where) => `[추출 실패: ${blockId} ${where}]`

const pieceText = (blockId, piece, where) => (piece.status === 'failed' ? failMark(blockId, where) : piece.text ?? '')

/**
 * 표 블록 결과(result.table)를 HTML 표로 그린다. 병합 칸(rowspan/colspan)도 그대로 살린다.
 * 표 제목(caption)은 표 위에, 칸 밖 글자(자막 등, outside_text)는 표 아래에 둔다 — 다운로드 결과와 같은 순서.
 */
function TableView({ blockId, result }) {
  const { n_rows: nRows, cells, caption } = result.table
  // 표 위 글자(제목·배너). 예전 결과는 caption 한 조각만 있다
  const head = [...(caption ? [caption] : []), ...(result.table.head_text ?? [])]
  const rows = Array.from({ length: nRows }, () => [])
  for (const c of cells) if (c.row < nRows) rows[c.row].push(c)
  rows.forEach((row) => row.sort((a, b) => a.col - b.col))
  const outside = result.outside_text ?? []

  return (
    <div className="table-view">
      {head.map((p, i) => (
        <p key={i} className={`table-caption ${p.status === 'failed' ? 'fail' : ''}`}>
          {pieceText(blockId, p, '제목')}
        </p>
      ))}
      <div className="table-scroll">
        <table>
          <tbody>
            {rows.map((row, r) => (
              <tr key={r}>
                {row.map((c) => {
                  const failed = c.status === 'failed'
                  const Tag = r === 0 ? 'th' : 'td'
                  return (
                    <Tag
                      key={c.col}
                      rowSpan={c.rowspan > 1 ? c.rowspan : undefined}
                      colSpan={c.colspan > 1 ? c.colspan : undefined}
                      className={failed ? 'fail' : undefined}
                      title={c.confidence != null ? `신뢰도 ${(c.confidence * 100).toFixed(1)}%` : undefined}
                    >
                      {failed
                        ? c.uncertain_text
                          ? `${c.uncertain_text} (?)`
                          : failMark(blockId, `r${c.row + 1}c${c.col + 1}`)
                        : c.text}
                    </Tag>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {outside.map((p, i) => (
        <p key={i} className={`table-outside ${p.status === 'failed' ? 'fail' : ''}`}>
          {pieceText(blockId, p, '표 밖')}
        </p>
      ))}
    </div>
  )
}

export default function BlockItem({ block, selected, onSelect, onRetry, onEdit, registerItem }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [showRaw, setShowRaw] = useState(false)
  const [error, setError] = useState('')

  const r = block.result
  const type = BLOCK_TYPES[block.type] ?? { label: block.type, color: '#495057' }
  const pending = block.status === 'pending'
  const failed = block.status === 'failed'
  const edited = r?.edited_text != null
  const failedCells = r?.failed_cells ?? 0
  // 표 블록은 글자(탭 구분) 대신 실제 표로 그린다. 한글 폭 때문에 탭으로는 열이 맞지 않기 때문.
  // 사용자가 고친 내용(edited_text)은 자유 글자라 그대로 글자로 보여 준다
  const showAsTable = block.type === 'table' && r?.status === 'done' && !edited && r?.table?.cells?.length > 0

  const startEdit = (e) => {
    e.stopPropagation()
    // 고칠 때 시작 글자: 이미 고친 내용 → 통과한 텍스트 → (실패라면) 원본 OCR 결과. 사용자가 처음부터 다시 치지 않아도 되게
    setDraft(r?.edited_text ?? r?.text ?? r?.raw_text ?? block.rendered ?? '')
    setEditing(true)
  }

  const save = async (value) => {
    setError('')
    try {
      await onEdit(block.block_id, value)
      setEditing(false)
    } catch (err) {
      setError(err.message)
    }
  }

  const retry = async (e) => {
    e.stopPropagation()
    setError('')
    try {
      await onRetry(block.block_id)
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <article
      ref={(el) => registerItem(block.block_id, el)}
      className={`block-item ${selected ? 'selected' : ''} ${failed ? 'failed' : ''}`}
      style={{ borderLeftColor: type.color }}
      onClick={() => onSelect(block.block_id, 'right')}
    >
      <header>
        <code>{block.block_id}</code>
        <span className="badge" style={{ background: type.color }}>
          {type.label}
        </span>
        {pending && <span className="badge muted">처리 중...</span>}
        {failed && <span className="badge danger">추출 실패 · {FAILURE_REASONS[r?.failure_reason] ?? r?.failure_reason}</span>}
        {failedCells > 0 && <span className="badge danger">실패 셀 {failedCells}개</span>}
        {edited && <span className="badge edited">수정됨</span>}
        {r?.confidence != null && <span className="conf">신뢰도 {(r.confidence * 100).toFixed(1)}%</span>}
        {block.bbox === null && <span className="conf" title="위치 정보가 없어 왼쪽 이미지에 표시되지 않습니다">위치 없음</span>}
      </header>

      {editing ? (
        <div className="edit" onClick={(e) => e.stopPropagation()}>
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={Math.min(12, Math.max(3, draft.split('\n').length + 1))} />
          <div className="row">
            <button className="primary" onClick={() => save(draft)}>
              저장
            </button>
            <button onClick={() => setEditing(false)}>취소</button>
          </div>
        </div>
      ) : showAsTable ? (
        <TableView blockId={block.block_id} result={r} />
      ) : (
        // 차트는 아래에 값 표를 따로 그리므로 여기서는 자리표시자(첫 줄)만 보여 준다
        <pre className="rendered">{r?.chart_table ? block.rendered.split('\n\n')[0] : block.rendered}</pre>
      )}

      {block.type === 'image' && r?.chart_table && <TableView blockId={block.block_id} result={{ table: r.chart_table }} />}
      {block.type === 'image' && r?.ocr_text && !r?.chart_table && <p className="sub">그림 속 글자: {r.ocr_text}</p>}

      <div className="row actions">
        {!editing && !pending && r && (
          <button onClick={startEdit}>수정</button>
        )}
        {edited && !editing && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              save(null)
            }}
          >
            원래대로
          </button>
        )}
        {(failed || failedCells > 0) && !pending && (
          <button className="warn" onClick={retry}>
            재시도
          </button>
        )}
        {r?.raw_text && (failed || edited) && (
          <button
            className="link"
            onClick={(e) => {
              e.stopPropagation()
              setShowRaw((v) => !v)
            }}
          >
            {showRaw ? '원본 숨기기' : '원본 OCR 결과 보기'}
          </button>
        )}
      </div>
      {showRaw && (
        <pre className="raw">
          {r.raw_text}
          {r.failure_detail && `\n\n(${r.failure_detail})`}
        </pre>
      )}
      {error && <p className="error">{error}</p>}
    </article>
  )
}
