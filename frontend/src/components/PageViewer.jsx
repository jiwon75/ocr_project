/**
 * [이 파일이 하는 일]
 * 결과 화면 왼쪽: 원본 페이지 이미지 위에 블록 bbox 를 종류별 색 상자로 그리고, 블록 번호 라벨을 단다.
 * 상자를 클릭하면 오른쪽의 해당 텍스트로 스크롤·하이라이트된다.
 *
 * [어떻게 그리는지]
 * 이미지 위에 같은 크기의 SVG 를 겹쳐 놓고 viewBox 를 "원본 픽셀 크기"로 맞춘다.
 * 그러면 화면에서 이미지가 줄어들어도 SVG 도 같은 비율로 줄어서, 서버가 준 픽셀 좌표를 그대로 쓸 수 있다.
 *
 * [규칙 3] bbox 가 null 인 블록(HWP 등)은 위치를 모르므로 상자를 그리지 않는다 (오른쪽 텍스트에만 나온다).
 */
import { api } from '../api/client'
import { BLOCK_TYPES } from '../config'

export default function PageViewer({ docId, pages, blocks, selectedId, onSelect, registerBox }) {
  const drawable = pages.filter((p) => p.width && p.height)
  if (drawable.length === 0) {
    return (
      <div className="no-pages">
        이 문서(HWP/HWPX)는 원본 페이지 이미지와 위치 정보가 없어서 상자를 그릴 수 없습니다.
        <br />
        오른쪽 텍스트에서 내용을 확인하세요.
      </div>
    )
  }

  return (
    <div className="pages">
      {drawable.map((page) => (
        <figure key={page.page} className="page">
          <figcaption>page {page.page}</figcaption>
          <div className="page-canvas">
            <img src={api.pageImageUrl(docId, page.page)} alt={`page ${page.page}`} />
            <svg viewBox={`0 0 ${page.width} ${page.height}`} preserveAspectRatio="none">
              {blocks
                .filter((b) => b.page === page.page && b.bbox) // [규칙 3] bbox 가 null 이면 그리지 않는다
                .map((b) => {
                  const [x1, y1, x2, y2] = b.bbox
                  const color = BLOCK_TYPES[b.type]?.color ?? '#495057'
                  const selected = b.block_id === selectedId
                  const failed = b.status === 'failed'
                  // 라벨 글자 크기를 페이지 폭에 비례시킨다. 고정 크기면 큰 페이지에서는 너무 작게 보이기 때문
                  const fontSize = page.width / 60
                  return (
                    <g key={b.block_id} ref={(el) => registerBox(b.block_id, el)} className="bbox" onClick={() => onSelect(b.block_id, 'left')}>
                      <rect
                        x={x1}
                        y={y1}
                        width={x2 - x1}
                        height={y2 - y1}
                        fill={color}
                        fillOpacity={selected ? 0.25 : 0.08}
                        stroke={failed ? '#e03131' : color}
                        strokeWidth={selected ? page.width / 250 : page.width / 600}
                        strokeDasharray={failed ? `${page.width / 120}` : undefined}
                      />
                      <rect x={x1} y={Math.max(0, y1 - fontSize * 1.3)} width={fontSize * 4.6} height={fontSize * 1.3} fill={failed ? '#e03131' : color} />
                      <text x={x1 + fontSize * 0.2} y={Math.max(fontSize, y1 - fontSize * 0.3)} fontSize={fontSize} fill="#fff">
                        {b.block_id}
                      </text>
                    </g>
                  )
                })}
            </svg>
          </div>
        </figure>
      ))}
    </div>
  )
}
