from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.core.config import settings
from app.schemas.ocr import OcrResponse
from app.services.ocr import OcrError, extract_text

router = APIRouter()

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
ALLOWED_LANGS = {"kor", "eng", "kor+eng"}


# OCR 은 CPU 작업(블로킹)이므로 async 가 아닌 def 로 선언 → FastAPI 가 스레드풀에서 실행
@router.post("", response_model=OcrResponse)
def run_ocr(
    file: UploadFile = File(...),
    lang: str = Form("kor+eng"),
):
    filename = file.filename or "upload"
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"지원하지 않는 파일 형식입니다: {ext or '(없음)'}")
    if lang not in ALLOWED_LANGS:
        raise HTTPException(400, f"지원하지 않는 언어입니다: {lang}")

    data = file.file.read(settings.OCR_MAX_FILE_MB * 1024 * 1024 + 1)
    if len(data) > settings.OCR_MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(413, f"파일이 너무 큽니다. (최대 {settings.OCR_MAX_FILE_MB}MB)")
    if not data:
        raise HTTPException(400, "빈 파일입니다.")

    try:
        pages = extract_text(data, filename, file.content_type, lang)
    except OcrError as e:
        raise HTTPException(422, str(e))

    return OcrResponse(
        filename=filename,
        page_count=len(pages),
        pages=pages,
        text="\n\n".join(p["text"] for p in pages),
    )
