/**
 * [이 파일이 하는 일]
 * .env 의 VITE_ 설정값을 한 곳에서 읽어 숫자 등으로 바꿔 둔다.
 *
 * [왜 이렇게 나눴는지]
 * [규칙 4] 설정 값을 컴포넌트 여기저기에 직접 쓰면 바꿀 때 놓치기 쉽다.
 * 다른 파일은 import { MAX_FILE_SIZE_MB } from './config' 처럼 여기서만 가져다 쓴다.
 */

const env = import.meta.env

export const API_URL = env.VITE_API_URL
export const MAX_FILE_SIZE_MB = Number(env.VITE_MAX_FILE_SIZE_MB)
export const STATUS_POLL_MS = Number(env.VITE_STATUS_POLL_MS)
export const HEALTH_POLL_MS = Number(env.VITE_HEALTH_POLL_MS)

// 설정이 빠졌는지 검사한다. 기본값을 몰래 넣지 않는 이유: .env 를 안 만든 실수를 바로 알아차리게 하기 위해서
export const CONFIG_ERRORS = [
  !API_URL && 'VITE_API_URL',
  !(MAX_FILE_SIZE_MB > 0) && 'VITE_MAX_FILE_SIZE_MB',
  !(STATUS_POLL_MS > 0) && 'VITE_STATUS_POLL_MS',
  !(HEALTH_POLL_MS > 0) && 'VITE_HEALTH_POLL_MS',
].filter(Boolean)

// 지원 형식. backend 가 처리할 줄 아는 형식이라 설정이 아니라 코드에 둔다
export const ACCEPT_EXTENSIONS = ['png', 'jpg', 'jpeg', 'pdf', 'hwp', 'hwpx']

export const LANGUAGES = [
  { value: 'auto', label: '자동 (한국어+영어)' },
  { value: 'ko', label: '한국어' },
  { value: 'en', label: '영어' },
]

// 블록 종류별 색과 이름 (왼쪽 bbox 와 오른쪽 목록에서 같은 색을 쓴다)
export const BLOCK_TYPES = {
  title: { label: '제목', color: '#e8590c' },
  text: { label: '본문', color: '#1c7ed6' },
  table: { label: '표', color: '#2f9e44' },
  image: { label: '이미지', color: '#ae3ec9' },
  caption: { label: '캡션', color: '#f59f00' },
  header: { label: '머리말', color: '#868e96' },
  footer: { label: '꼬리말', color: '#868e96' },
}

// 실패 사유 코드 → 사용자용 설명
export const FAILURE_REASONS = {
  low_confidence: '인식 신뢰도가 기준보다 낮음',
  empty: '인식된 글자 없음',
  error: '처리 중 오류',
  table_structure_failed: '표 구조 인식 실패',
  timeout: '시간 초과',
}

export const STAGE_NAMES = {
  queued: '대기',
  upload: '업로드',
  preprocess: '전처리',
  layout: '구조 분석',
  process: '블록 처리',
  merge: '합치기',
  db: 'DB 저장',
}
