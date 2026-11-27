// 개발 시 Vite 프록시를 통해 /api -> http://localhost:8000/api
const BASE_URL = import.meta.env.VITE_API_URL ?? '/api'

async function request(path, options = {}) {
  // FormData 는 브라우저가 multipart boundary 를 포함한 Content-Type 을 직접 설정해야 함
  const isForm = options.body instanceof FormData
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: isForm ? {} : { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  health: () => request('/health'),
  listItems: () => request('/items'),
  createItem: (data) => request('/items', { method: 'POST', body: JSON.stringify(data) }),
  deleteItem: (id) => request(`/items/${id}`, { method: 'DELETE' }),
  ocr: (file, lang = 'kor+eng') => {
    const form = new FormData()
    form.append('file', file)
    form.append('lang', lang)
    return request('/ocr', { method: 'POST', body: form })
  },
}
