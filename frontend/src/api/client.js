/**
 * [이 파일이 하는 일]
 * backend API 를 부르는 함수 모음. 컴포넌트는 fetch 를 직접 쓰지 않고 이 함수들만 쓴다.
 *
 * [왜 이렇게 나눴는지]
 * API 주소나 오류 처리 방식이 바뀌어도 이 파일만 고치면 된다.
 */
import { API_URL } from '../config'

/** API 호출 공통 함수. 실패하면 backend 가 보낸 한국어 오류 문장(detail)을 담아 Error 를 던진다. */
async function request(path, { raw = false, ...options } = {}) {
  const isForm = options.body instanceof FormData
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    // FormData(파일 업로드)는 브라우저가 Content-Type 을 직접 정해야 한다 (파일 구분선 정보가 들어가기 때문)
    headers: isForm || !options.body ? {} : { 'Content-Type': 'application/json' },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const error = new Error(body?.detail ?? `서버 오류 (${res.status})`)
    error.status = res.status
    throw error
  }
  return raw ? res.text() : res.json()
}

export const api = {
  /** 서버 준비 상태. 준비 전에는 503 이 오므로 오류로 던지지 않고 { ready: false } 로 돌려준다 */
  async health() {
    try {
      const res = await fetch(`${API_URL}/health`)
      const data = await res.json()
      return { ready: res.ok && data.status === 'ready', data }
    } catch {
      return { ready: false, data: null, offline: true }
    }
  },

  upload(file, language) {
    const form = new FormData()
    form.append('file', file)
    form.append('language', language)
    return request('/documents', { method: 'POST', body: form })
  },

  status: (docId) => request(`/documents/${docId}/status`),
  blocks: (docId) => request(`/documents/${docId}/blocks`),
  block: (docId, blockId) => request(`/documents/${docId}/blocks/${blockId}`),
  retry: (docId, blockId) => request(`/documents/${docId}/blocks/${blockId}/retry`, { method: 'POST' }),
  edit: (docId, blockId, editedText) =>
    request(`/documents/${docId}/blocks/${blockId}`, { method: 'PATCH', body: JSON.stringify({ edited_text: editedText }) }),

  result(docId, { format = 'txt', pageSeparator = true, debug = false } = {}) {
    const q = new URLSearchParams({ format, page_separator: pageSeparator, debug })
    return request(`/documents/${docId}/result?${q}`, { raw: true })
  },

  pageImageUrl: (docId, page) => `${API_URL}/documents/${docId}/pages/${page}/image`,
}
