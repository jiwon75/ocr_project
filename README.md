# Team Project

- **frontend**: React + Vite (`http://localhost:5173`)
- **backend**: FastAPI (`http://localhost:8000`, API 문서: `/docs`)

## 구조

```
teamproject/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI 앱, CORS, 라우터 등록
│   │   ├── core/config.py   # 환경설정 (.env)
│   │   ├── api/router.py    # /api 하위 라우터 모음
│   │   ├── api/routes/      # 엔드포인트 (health, items)
│   │   └── schemas/         # Pydantic 모델
│   └── requirements.txt
└── frontend/
    ├── src/
    │   ├── api/client.js    # 백엔드 API 호출
    │   ├── App.jsx
    │   └── main.jsx
    └── vite.config.js       # /api → :8000 프록시
```

## OCR 기능

`POST /api/ocr` (multipart: `file`, `lang`=`kor+eng`|`kor`|`eng`) → 이미지/PDF 에서 텍스트 추출

- 이미지(png/jpg/bmp/tiff/webp): Tesseract 로 OCR
- PDF: 페이지에 텍스트 레이어가 있으면 그대로 사용, 없으면(스캔본) 300dpi 로 렌더링 후 OCR
- **사전 요구사항**: [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) 설치 + 한국어(`kor`) 언어 데이터.
  PATH 에 없으면 `.env` 의 `TESSERACT_CMD` 에 tesseract.exe 경로 지정

## 실행

### Backend
```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

### Frontend
```powershell
cd frontend
npm install
npm run dev
```

브라우저에서 http://localhost:5173 접속 → "Backend status: ok"가 보이면 연결 성공.
