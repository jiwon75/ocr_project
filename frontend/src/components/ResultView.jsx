/**
 * [이 파일이 하는 일]
 * 결과 화면(2분할). 왼쪽 = 원본 + bbox(PageViewer), 오른쪽 = 합쳐진 텍스트(블록 목록).
 * 위쪽 도구 막대: 다른 파일 올리기(업로드 화면으로), 복사, .txt/.md 다운로드, 페이지 구분선/디버그 옵션.
 *
 * [양쪽 연결(하이라이트) 방법]
 * 선택된 블록 번호(selected)를 이 컴포넌트가 들고 있고, 양쪽 모두 그 값을 보고 하이라이트한다.
 * 왼쪽에서 클릭하면 오른쪽을, 오른쪽에서 클릭하면 왼쪽을 스크롤한다 (클릭한 쪽은 이미 보이므로 움직이지 않는다).
 */
import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import AssistPanel from './AssistPanel'
import BlockItem from './BlockItem'
import PageViewer from './PageViewer'

export default function ResultView({ docId, data, fileName, onRetry, onEdit, onReset, onReload }) {
  const [selected, setSelected] = useState({ id: null, from: null })
  const [pageSeparator, setPageSeparator] = useState(true)
  const [debug, setDebug] = useState(false)
  const [notice, setNotice] = useState('')
  const boxRefs = useRef({}) // block_id → 왼쪽 SVG 요소
  const itemRefs = useRef({}) // block_id → 오른쪽 블록 요소

  const select = (id, from) => setSelected({ id, from })

  useEffect(() => {
    if (!selected.id) return
    // 반대쪽만 스크롤한다
    const target = selected.from === 'left' ? itemRefs.current[selected.id] : boxRefs.current[selected.id]
    target?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [selected])

  const flash = (text) => {
    setNotice(text)
    setTimeout(() => setNotice(''), 2000)
  }

  /** 서버에서 최종 결과를 받아 온다. 화면 목록이 아니라 서버 결과를 쓰는 이유: DB 에 저장된 것과 같은 결과를 내려받게 하기 위해 */
  const fetchResult = (format) => api.result(docId, { format, pageSeparator, debug })

  const copy = async () => {
    await navigator.clipboard.writeText(await fetchResult('txt'))
    flash('복사했습니다')
  }

  const download = async (format) => {
    const text = await fetchResult(format)
    const blob = new Blob([text], { type: format === 'md' ? 'text/markdown;charset=utf-8' : 'text/plain;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = fileName.replace(/\.[^.]+$/, '') + '.' + format
    a.click()
    URL.revokeObjectURL(a.href)
  }

  const failedCount = data.blocks.filter((b) => b.status === 'failed').length

  return (
    <section className="result">
      <div className="toolbar card">
        {/* 업로드 화면으로 돌아가는 버튼. 도구 막대 맨 앞에 눈에 띄게 둔다 (예전 "새 파일" 버튼은 다른 버튼들 사이에 묻혀 안 보였다) */}
        <button className="primary back-to-upload" onClick={onReset}>
          ← 다른 파일 올리기
        </button>
        <strong className="grow">
          {fileName} · 블록 {data.blocks.length}개{failedCount > 0 && <span className="danger-text"> · 추출 실패 {failedCount}개</span>}
        </strong>
        <label>
          <input type="checkbox" checked={pageSeparator} onChange={(e) => setPageSeparator(e.target.checked)} /> 페이지 구분선
        </label>
        <label>
          <input type="checkbox" checked={debug} onChange={(e) => setDebug(e.target.checked)} /> 블록 ID 주석(디버그)
        </label>
        <button onClick={copy}>복사</button>
        <button onClick={() => download('txt')}>.txt 다운로드</button>
        <button onClick={() => download('md')}>.md 다운로드</button>
        {notice && <span className="notice">{notice}</span>}
      </div>

      <div className="split">
        <div className="pane left">
          <PageViewer
            docId={docId}
            pages={data.pages}
            blocks={data.blocks}
            selectedId={selected.id}
            onSelect={select}
            registerBox={(id, el) => (boxRefs.current[id] = el)}
          />
        </div>
        <div className="pane right">
          {/* LLM 도우미(요약·오타 교정 제안). 교정 제안의 블록 번호를 누르면 왼쪽에서 그 블록을 보여 준다 */}
          <AssistPanel docId={docId} onApplied={onReload} onSelectBlock={(id) => select(id, 'right')} />
          {data.blocks.map((block, i) => (
            <div key={block.block_id}>
              {pageSeparator && i > 0 && block.page !== data.blocks[i - 1].page && <div className="page-sep">--- page {block.page} ---</div>}
              <BlockItem
                block={block}
                selected={selected.id === block.block_id}
                onSelect={select}
                onRetry={onRetry}
                onEdit={onEdit}
                registerItem={(id, el) => (itemRefs.current[id] = el)}
              />
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
