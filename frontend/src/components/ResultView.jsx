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

/**
 * 결과 옵션(페이지 구분선, 블록 ID 주석)을 브라우저(localStorage)에 기억한다.
 * "다른 파일 올리기" 를 누르면 이 화면이 사라졌다 다시 만들어져서, 기억하지 않으면 체크가 기본값으로 돌아갔다.
 * 개인 창 등에서 저장이 막혀도 화면은 기본값으로 정상 동작한다.
 */
function useStoredOption(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const saved = localStorage.getItem(key)
      return saved === null ? initial : saved === 'true'
    } catch {
      return initial
    }
  })
  const update = (v) => {
    setValue(v)
    try {
      localStorage.setItem(key, String(v))
    } catch {
      // 저장이 막힌 환경: 이번 화면에서만 유지
    }
  }
  return [value, update]
}

export default function ResultView({ docId, data, fileName, onRetry, onEdit, onReset, onReload }) {
  const [selected, setSelected] = useState({ id: null, from: null })
  const [pageSeparator, setPageSeparator] = useStoredOption('ocr.result.pageSeparator', true)
  const [debug, setDebug] = useStoredOption('ocr.result.debug', false)
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
    let blob
    try {
      if (format === 'chunks') {
        // 청크는 서버(DB)에 저장된 목록을 그대로 JSON 파일로 받는다
        const data = await api.chunks(docId)
        blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' })
      } else if (format === 'pdf' || format === 'pptx') {
        flash(`${format === 'pdf' ? 'PDF' : 'PPT'} 파일을 만드는 중...`)
        blob = await api.resultFile(docId, format, { debug })
      } else {
        const text = await fetchResult(format)
        blob = new Blob([text], { type: format === 'md' ? 'text/markdown;charset=utf-8' : 'text/plain;charset=utf-8' })
      }
    } catch (err) {
      flash(`다운로드 실패: ${err.message}`)
      return
    }
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = fileName.replace(/\.[^.]+$/, '') + (format === 'chunks' ? '.chunks.json' : '.' + format)
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
        {/* 글자를 선택·수정할 수 있는 PDF, 페이지마다 슬라이드 하나인 PowerPoint */}
        <button onClick={() => download('pdf')} title="글자를 선택·복사·수정할 수 있는 PDF">PDF 다운로드</button>
        <button onClick={() => download('pptx')} title="페이지마다 슬라이드 하나, 원본 위치에 글상자·표·그림">PPT 다운로드</button>
        {/* 검색·RAG 용으로 나눈 청크(글자 수·페이지·블록 번호 포함). 크기·겹침은 backend .env 의 CHUNK_SIZE_CHARS / CHUNK_OVERLAP_CHARS */}
        <button onClick={() => download('chunks')} title="추출 텍스트를 검색·RAG 용 조각(청크)으로 나눈 JSON">청크 JSON 다운로드</button>
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
              {/* 블록 ID 주석(디버그): 내려받는 파일과 똑같이 화면에도 블록 앞에 [블록 p1-b003] 을 보여 준다 */}
              {debug && <div className="debug-marker">[블록 {block.block_id}]</div>}
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
