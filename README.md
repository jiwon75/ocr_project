# 문서 OCR — 업로드 → 구조 분석 → 텍스트 출력

이미지(png/jpg), PDF, HWP/HWPX 문서를 올리면 **문서 구조(제목·본문·표·이미지)를 먼저 분석**하고,
블록 단위로 텍스트를 뽑아 **읽기 순서대로 합친 결과(.txt / .md)** 를 보여 주는 웹 서비스입니다.

- **frontend**: React + Vite (JavaScript) — `http://localhost:5173`
- **backend**: FastAPI (Python 3.12.10) — `http://localhost:8000`, API 문서 `http://localhost:8000/docs`
- **OCR / 레이아웃**: PaddleOCR 3.7 (PP-DocLayout_plus-L, PP-OCRv5 한국어·영어, SLANet_plus)
- **DB**: SQLite (파이썬 기본 `sqlite3`, ORM 없음)

---

## 1. 반드시 지키는 규칙

| 규칙 | 내용 | 어디서 지키는지 | 테스트 |
|---|---|---|---|
| 1 | `block_id`·`order` 는 1단계에서만 정하고 절대 바꾸지 않는다. 실패 블록도 그 자리에 남긴다 | `pipeline/layout.py` (유일하게 번호를 만드는 곳), `pipeline/structure.py` (status 만 바꿀 수 있게 막음) | `tests/test_rules.py` |
| 2 | 잘못된 글자는 고치지 않고 `[추출 실패: p1-b003]` 로 표시. 원본·confidence 보존, 사용자 수정은 `edited_text` 에 따로 | `processors/common.py` `judge_text`, `merge.py` | `tests/test_rules.py` |
| 3 | 위치를 모르면 `"bbox": null` (HWP 등). 프론트는 상자를 그리지 않음 | `layout.py` `_hwp_structure`, `PageViewer.jsx` | `tests/test_rules.py` |
| 4 | 설정은 `.env` 에. `.env` 는 git 에 올리지 않고 `.env.example` 만 올림 | `app/config.py`, `src/config.js` | `tests/test_rules.py` (`.env.example` 항목 = Settings 항목인지까지 검사) |

---

## 2. 전체 설계

### 처리 흐름

```
업로드(API) ─▶ uploaded_files 에 기록 ─▶ 작업 대기열
                                         │
                ┌────────────── 워커 프로세스 (모델은 시작할 때 한 번만 로딩) ──────────────┐
                │ 전처리    파일 → 페이지 이미지 (PDF 는 텍스트 레이어 유무 표시, HWP 는 직접 파싱)  │
                │ 1단계     모든 페이지 레이아웃 분석 → 읽기 순서(XY-cut) → p1-b001 / order 부여   │
                │           → storage/{doc_id}/structure.json                               │
                │ 2단계     블록 crop 저장 → 종류별 처리 (text/title: OCR+confidence,          │
                │           table: 셀 구조+셀별 OCR, image: 저장+속 글자 OCR)                 │
                │           → blocks/{block_id}.png, results/{block_id}.json                 │
                │ 3단계     order 순서로 합치기 → output/result.txt|md → extracted_texts      │
                └──────────────────────────────────────────────────────────────────────────┘
       API 서버(메인 프로세스)가 시간을 재다가 PROCESS_TIMEOUT_SEC 를 넘으면 워커를 강제 종료(kill)
```

### 라이브러리를 고른 이유

| 역할 | 선택 | 이유 |
|---|---|---|
| 레이아웃(1단계) | PP-DocLayout_plus-L | 이 PC(CPU)에서 직접 비교: S 모델은 제목·문단을 놓쳤고 plus-L 은 모두 찾음. 페이지당 약 2초 |
| 글자 검출/인식 | PP-OCRv5 mobile det + `korean_` / `en_` rec | 빠름(블록당 0.1~0.3초). 한국어 모델은 영어도 읽음. **자동** 모드는 줄마다 두 모델 중 confidence 가 높은 쪽을 고름 → 섞인 문서 처리 |
| 표 구조 | SLANet_plus | 행·열·병합(colspan/rowspan)을 HTML 구조로 줌 |
| PDF | PyMuPDF | 페이지 렌더링 + 텍스트 레이어 직접 추출을 한 라이브러리로 |
| HWP | 직접 파싱 (olefile / zip+XML) | 아래 참고 |

- **PP-StructureV3 전체 파이프라인을 쓰지 않은 이유**: 방향 보정·수식·도장 모델까지 한꺼번에 돌려 이 CPU 로는 30초 안에 끝내기 어렵고, 블록 순서를 스스로 정해서 규칙 1(우리가 1단계에서 번호를 정함)과 맞지 않습니다.
- **Surya**: 레이아웃·읽기 순서 품질은 좋지만 트랜스포머 모델이라 CPU 에서 매우 느리고, 상업적 이용에 라이선스 제한이 있습니다.
- **docling**: 디지털 PDF 에는 아주 좋지만, 한국어 OCR 경로가 약합니다. 나중에 디지털 PDF 전용 처리기로 붙여 볼 만합니다.
- **paddlepaddle 3.2.2 로 고정한 이유**: 3.3.1 은 이 PC 에서 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute` (oneDNN 오류)로 레이아웃 모델이 실행되지 않았습니다.

### HWP: (A) LibreOffice 변환 vs (B) 직접 파싱 → **(B)를 선택**

| | (A) LibreOffice + H2Orestart → PDF | (B) 직접 파싱 (선택) |
|---|---|---|
| 장점 | 페이지 이미지와 좌표가 생겨 왼쪽에 상자를 그릴 수 있음 | 파일에 저장된 글자를 그대로 → **오인식 없음**, 빠름(0.2초), 표 병합 정보 정확, 추가 설치 없음 |
| 단점 | 서버마다 LibreOffice 설치 필요, 변환이 수 초(30초 제한에 불리), 변환된 PDF 를 OCR 하면 오인식 생김 | 페이지·좌표 정보 없음 → `bbox: null`, 모두 page 1. 글상자·수식 일부 누락 가능 |

HWPX(zip 안의 XML)는 문단·제목 스타일·표(병합 셀)·머리말·그림을 읽습니다.
HWP(바이너리)는 문단·표(병합 셀)·머리말을 읽고, 그림은 자리표시자만 남깁니다(그림 데이터 연결은 해석하지 않음). 배포용·암호 문서는 안내 문장과 함께 실패 처리합니다.

### 시간 제한(30초)을 "실제로 멈추는" 방법

- 파이썬 스레드는 밖에서 강제로 멈출 수 없어서, OCR 을 **별도 프로세스(워커)** 에서 돌립니다.
- 메인 프로세스의 감시 스레드가 작업을 넘긴 순간부터 시간을 재고, 넘으면 `process.kill()` → 그 파일만 `failed (timeout)` → 새 워커를 띄웁니다.
- 시간은 **워커가 작업을 받은 순간부터** 잽니다 (대기열에서 기다린 시간은 세지 않음).
- 트레이드오프: 강제 종료된 워커의 모델도 함께 사라지므로 새 워커가 모델을 다시 불러옵니다(약 12초). 그동안 health 는 `not_ready`, 새 파일은 `queued` 로 기다립니다.

### DB 테이블 관계

```
uploaded_files (id PK, doc_id UNIQUE, file_name, file_path, file_type, created_at)
      ▲ 1
      │  FOREIGN KEY (file_id → uploaded_files.id)
      │ 0..1
extracted_texts (id PK, file_id, file_name, extracted_text, created_at)
```

- 업로드 검사 통과 → `uploaded_files` 1행. 3단계 완료 → `extracted_texts` 1행 (retry/수정 시 **같은 행 갱신**).
- 파일 전체 실패(시간 초과·손상 등) → `extracted_texts` 에 저장하지 않음 (실패 사유는 status API·로그).
- 같은 이름의 파일이 여러 번 올라와도 `file_id` 로 연결되므로 섞이지 않습니다.
- 프로세스마다 연결을 따로 열고, WAL 모드를 켭니다. SQL 값은 모두 `?` 자리표시자로 넣습니다.

---

## 3. 폴더 구조

```
.gitignore                .env, storage/, logs/, data/*.db 제외
backend/
  .env.example            설정 예시 (모든 항목)
  requirements.txt        실행용 (버전 고정)
  requirements-dev.txt    테스트용 (pytest, httpx2)
  pytest.ini
  app/
    main.py               create_app(), lifespan: 로그·DB 테이블·워커(모델 로딩) 시작
    config.py             .env 읽기 (pydantic-settings)
    logger.py             로그 형식, 날짜별 로테이션, 워커 로그 → 큐 → 메인
    db.py                 SQLite 연결(WAL), 테이블 생성, 저장/조회
    api/                  health.py, documents.py
    models/schemas.py     API 요청/응답 모양 (Pydantic)
    pipeline/
      preprocess.py       전처리: 형식별 → 페이지 이미지 / 텍스트 레이어
      hwp.py              HWP·HWPX 직접 파싱
      engines.py          PaddleOCR 모델 묶음 (교체 가능한 한 곳)
      layout.py           1단계: 레이아웃, 읽기 순서(XY-cut), 번호 부여
      structure.py        structure.json 읽기/status 만 바꾸기 (규칙 1 보호)
      processors/         2단계: common.py, text.py, table.py, image.py
      process.py          2단계 실행 (블록 하나 / 전체)
      merge.py            3단계: 합치기 (.txt/.md) + DB 저장
      runner.py           단계 순서·로그·실패 기록, retry, 수정 반영
      worker.py           워커 프로세스, 시간 제한, 강제 종료/재시작
      status.py storage.py errors.py
  tests/                  단계별·규칙·운영·API/DB 테스트, 가짜 엔진(fakes.py), 샘플 생성(samples.py)
  data/  storage/  logs/  (실행하면 생김, git 제외)
frontend/
  .env.example            VITE_API_URL, VITE_MAX_FILE_SIZE_MB 등
  src/
    config.js             VITE_ 설정 읽기
    api/client.js         backend 호출
    pages/DocumentPage.jsx  업로드 → 진행 → 결과 흐름
    components/           UploadPanel, ProgressPanel, ResultView, PageViewer, BlockItem
```

---

## 4. 설치

### 준비물
- **Python 3.12.10** (64bit), **Node.js 20 이상**
- 인터넷 연결 (첫 실행 때 OCR 모델 약 300MB 를 `C:\Users\<사용자>\.paddlex\official_models` 에 내려받음)
- 시스템 프로그램 추가 설치 **없음** (LibreOffice·Tesseract 불필요)
- 메모리: 워커 1개당 약 1.5GB

### backend
```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt     # 실행만 할 거면 requirements.txt
copy .env.example .env                  # 설정 파일 만들기 (필요하면 값 수정)
```

### frontend
```powershell
cd frontend
npm install
copy .env.example .env
```

> `.env` 는 PC 마다 다를 수 있고 git 에 올라가지 않습니다. 새 항목을 추가하면 `.env.example` 에도 같이 추가해 주세요 (테스트가 검사합니다).

### 주요 설정 (`backend/.env`)

| 항목 | 기본값 | 설명 |
|---|---|---|
| `MAX_FILE_SIZE_MB` | 5 | 업로드 최대 크기. 넘으면 413 + "파일 크기는 5MB 이하만 가능합니다." |
| `MAX_PAGES` | 10 | PDF 최대 페이지 |
| `PROCESS_TIMEOUT_SEC` | 30 | 파일 하나(1~3단계) 시간 제한 |
| `OCR_CONFIDENCE_THRESHOLD` | 0.8 | 이보다 낮으면 "추출 실패" |
| `OCR_CONFIDENCE_AGG` | mean | 블록 점수 = 글자 수 가중 평균(`mean`) 또는 최솟값(`min`) |
| `WORKER_COUNT` | 1 | 동시에 처리할 파일 수 |
| `DB_PATH` / `STORAGE_DIR` / `LOG_DIR` | ./data/app.db 등 | backend 폴더 기준 |
| `CORS_ORIGINS` | 5173 포트 | 프론트 주소 (JSON 배열) |

`frontend/.env` 의 `VITE_MAX_FILE_SIZE_MB` 는 backend 의 `MAX_FILE_SIZE_MB` 와 같게 맞춰 주세요.

---

## 5. 실행

```powershell
# 터미널 1 — backend
cd backend
.venv\Scripts\activate
python -m uvicorn app.main:app --port 8000
```
서버는 바로 뜨고, 모델은 워커에서 백그라운드로 불러옵니다 (약 12초, 첫 실행은 다운로드 포함 수 분).
그동안 `GET /api/health` 는 503 `not_ready`, 끝나면 200 `ready`.

```powershell
# 터미널 2 — frontend
cd frontend
npm run dev
```
브라우저에서 http://localhost:5173 → 오른쪽 위가 "● OCR 준비 완료" 가 되면 파일을 올립니다.

> `--reload` 옵션은 코드를 고칠 때마다 서버와 워커(모델)를 다시 띄우므로 개발 중에만 쓰세요.

### 이 PC(i5-6500, CPU) 에서 측정한 처리 시간

| 파일 | 블록 | 시간 |
|---|---|---|
| 스캔 이미지 1장 (png/jpg/스캔 PDF) | 4 | 4.9 ~ 5.5초 |
| 2단 편집 디지털 PDF 1쪽 | 7 | 2.1초 (OCR 없이 텍스트 레이어 사용) |
| 디지털 PDF 2쪽 | 6 | 3.5초 |
| HWPX | 5 | 0.2초 |

→ 스캔본은 **페이지당 약 5초** 라서 30초 제한이면 대략 **5~6쪽**까지입니다. 더 긴 스캔본은 `PROCESS_TIMEOUT_SEC` 를 늘리세요.

---

## 6. API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/health` | 서버·모델 준비 상태 (준비 전 503) |
| POST | `/api/documents` | 파일 1개 + `language`(auto/ko/en) → `doc_id` (202, 처리는 백그라운드) |
| GET | `/api/documents/{doc_id}/status` | `queued/analyzing/processing/merging/done/failed`, 진행률, 실패 사유 |
| GET | `/api/documents/{doc_id}/structure` | structure.json |
| GET | `/api/documents/{doc_id}/blocks` | 전체 블록 + 결과 (화면용) |
| GET | `/api/documents/{doc_id}/blocks/{block_id}` | 블록 하나 결과 + crop 이미지 URL |
| POST | `/api/documents/{doc_id}/blocks/{block_id}/retry` | 블록 하나 다시 처리 (번호 유지) |
| PATCH | `/api/documents/{doc_id}/blocks/{block_id}` | `{"edited_text": "..."}` 사용자 수정 (null = 취소) |
| GET | `/api/documents/{doc_id}/pages/{page}/image` | 원본 페이지 이미지 |
| GET | `/api/documents/{doc_id}/result?format=txt\|md&page_separator=true&debug=false` | 최종 결과 |

실패 예시: `{"status": "failed", "error": {"stage": "layout", "reason": "timeout (30s)", "message": "처리 시간이 제한(30초)을 넘어 중단되었습니다. ..."}}`

---

## 7. 테스트

```powershell
cd backend
.venv\Scripts\activate
python -m pytest                    # 전체 (실제 모델 테스트 포함, 약 1~2분)
python -m pytest -m "not models"    # 실제 모델 없이 빠르게 (약 1분)
```

| 파일 | 확인하는 것 |
|---|---|
| `test_preprocess.py` | png/jpg/pdf 페이지 변환, 텍스트 레이어 판별, 손상·페이지 초과, HWPX 파싱, HWP 레코드 파싱 |
| `test_stage1_layout.py` | 읽기 순서(1단·2단·전체 폭 그림), 번호 형식, structure.json, (실제 모델) 2단 PDF 순서 |
| `test_stage2_process.py` | crop·결과 JSON, confidence, 표 셀/병합, 이미지, 블록 실패 격리, (실제 모델) 한/영·표 인식 |
| `test_stage3_merge.py` | order 순서, txt/md, 구분선, 디버그 주석, DB 저장, (실제 모델) 30초 안에 끝나는지 |
| `test_rules.py` | 규칙 1~4 (retry 후 번호 불변, 낮은 confidence → 추출 실패, bbox null, .env/.gitignore) |
| `test_runtime.py` | health not_ready→ready, 모델 한 번만 로딩, **시간 초과 시 실제 중단 + 다음 파일 정상**, 워커 비정상 종료, 손상 파일, 로그 형식·traceback |
| `test_api_db.py` | 5MB 초과 413, 2개 업로드 거절, MIME 검사, uploaded_files/extracted_texts 행, 실패 파일은 결과 없음, retry 시 같은 행 갱신, FOREIGN KEY, WAL |

- 시간 초과 테스트는 테스트 시간을 줄이려고 `PROCESS_TIMEOUT_SEC=3` 으로 바꾸고 "120초 걸리는 가짜 파일"을 넣습니다. 30이라는 값도 설정이라 같은 코드가 동작합니다.
- 규칙·DB 테스트는 **가짜 엔진**(`tests/fakes.py`)을 씁니다. 실제 모델은 점수가 매번 조금씩 달라서 "0.42 면 실패" 같은 규칙을 정확히 검사하기 어렵기 때문입니다.
- 실제 `.hwp`(바이너리) 파일은 한글 프로그램 없이 만들 수 없어서, 레코드를 직접 조립해 파서를 검사합니다. **실제 한글 파일로는 아직 확인하지 못했습니다.**

---

## 8. DB 내용 확인

**방법 1 — 파이썬 한 줄** (추가 설치 없음)
```powershell
cd backend
.venv\Scripts\python -c "import sqlite3; c=sqlite3.connect('data/app.db'); [print(r) for r in c.execute('SELECT id, doc_id, file_name, file_type, created_at FROM uploaded_files')]"
.venv\Scripts\python -c "import sqlite3; c=sqlite3.connect('data/app.db'); [print(r[0], r[1], r[2][:80]) for r in c.execute('SELECT file_id, file_name, extracted_text FROM extracted_texts')]"
```

**방법 2 — 두 테이블 연결해서 보기**
```sql
SELECT u.id, u.file_name, u.created_at, length(e.extracted_text) AS 글자수
FROM uploaded_files u LEFT JOIN extracted_texts e ON e.file_id = u.id
ORDER BY u.id;
```
(`글자수` 가 비어 있으면 처리에 실패한 파일)

**방법 3 — 화면 도구**: [DB Browser for SQLite](https://sqlitebrowser.org/) 로 `backend/data/app.db` 를 엽니다.

---

## 9. 알아 둘 점 / 한계

- **confidence 만으로는 모든 오인식을 잡지 못합니다.** 실제 스캔 샘플에서 "문서 처리 테스트 보고서" 가 "문서 처리 **|**테스트 보고서" 로 읽혔는데 confidence 는 0.999 였습니다. 규칙 2에 따라 고치지 않고 그대로 두며, 화면의 [수정] 으로 `edited_text` 에 고칠 수 있습니다.
- 표 셀 하나가 기준 미달이면 표 전체가 아니라 그 셀만 `[추출 실패: p1-b003 r2c3]` (블록 번호 + 행/열) 로 표시합니다.
- 표 안에서 두 칸에 반씩 걸친 글자 줄이나 표 영역 안의 칸 밖 글자(영상 자막 등)는 어느 칸인지 추측하지 않고 결과 JSON 의 `unassigned_lines`/`outside_text` 에 남기며, 결과 파일에서는 표 아래에 이어 씁니다.
- `.md` 의 표는 항상 순수 Markdown 표입니다. 병합 칸의 글자는 첫 칸에 두고 가려진 칸은 빈칸으로 둡니다. 맨 윗줄이 전체 병합 칸 하나뿐이면 표 제목으로 보고 표 위에 씁니다.
- 막대 차트(레이아웃 라벨 chart)는 막대 위 값·항목 이름·범례를 읽어 그림 아래에 값 표를 붙입니다. 항목마다 값 개수가 다르면 표를 만들지 않습니다.
- 병합 칸이 있는 이미지 표는 병합 글자를 그룹의 첫 행에 두고 아래 행은 빈칸으로 둡니다. 표 위 제목·배너, 표 아래 "*단위" 같은 글자는 표 밖 줄로 씁니다.
- 이미지 표는 OCR 글자 줄 위치로도 행·열을 만들어, 표 구조 모델이 서로 다른 행의 글자를 한 칸에 넣으면 그쪽을 씁니다 (병합 칸은 표현하지 못함).
- 레이아웃 모델이 놓친 글자는 페이지 전체에서 다시 찾아 블록으로 추가합니다. 행·열로 가지런하면 표 블록 하나로 묶고, 페이지 높이의 3% 미만 작은 글자는 버립니다 (`LAYOUT_SWEEP_*`).
- 이미지·PDF 에서 머리말·꼬리말·쪽번호로 잡힌 블록은 .txt/.md 에서 뺍니다 (`MERGE_SKIP_PAGE_FURNITURE`, 화면에는 보임).
- 표 칸이 신뢰도 기준 미달이면 `[추출 실패]` 대신 `읽은 글자 (?)` 로 보여 줍니다 (`TABLE_SHOW_UNCERTAIN_TEXT=false` 로 끄면 예전처럼 실패 표시).
- 선 없는 표에서 표 구조 모델이 열을 하나 더 만들어 글자가 두 열에 번갈아 흩어지면, 칸 위치가 겹치고 같은 행에 동시에 글자가 없는 두 열을 하나로 합칩니다.
- 글자 검출이 놓친 칸(예: 반투명 배경 위 한 자리 숫자)은 그 열·행 범위만 잘라 인식을 한 번 더 돌리고, 기준 점수 이상일 때만 채웁니다.
- 언어 auto 에서는 한국어 모델이 한글을 기준 점수 이상으로 읽은 줄은 한국어 결과를 씁니다. 영어 모델은 "324원" 을 "324" 로 읽고도 점수를 더 높게 매기기 때문입니다.
- 레이아웃 모델이 블록을 하나도 못 찾은 페이지는 페이지 전체를 text 블록 하나로 처리합니다 (`layout_fallback: true`).
- 읽기 순서(XY-cut)는 일반적인 1단·2단 문서를 기준으로 합니다. 표·그림이 복잡하게 얽힌 잡지형 레이아웃은 순서가 틀릴 수 있습니다.
- 처리 로직(`runner.run_document`)은 API 와 분리돼 있어, `worker.py` 만 Celery/RQ 로 바꾸면 작업 큐로 옮길 수 있습니다.
