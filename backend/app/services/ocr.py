"""이미지 / PDF 에서 텍스트를 추출하는 OCR 서비스 (Tesseract + PyMuPDF)."""
import io

import pymupdf
import pytesseract
from PIL import Image, ImageOps

from app.core.config import settings

if settings.TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD

# PDF 페이지에 이 글자 수 이상 텍스트 레이어가 있으면 OCR 없이 그대로 사용
MIN_TEXT_LAYER_CHARS = 20


class OcrError(Exception):
    pass


def ocr_image(image: Image.Image, lang: str) -> str:
    image = ImageOps.exif_transpose(image)  # 휴대폰 사진 회전 보정
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return pytesseract.image_to_string(image, lang=lang).strip()


def extract_from_image(data: bytes, lang: str) -> list[dict]:
    try:
        image = Image.open(io.BytesIO(data))
    except Exception as e:
        raise OcrError("이미지 파일을 읽을 수 없습니다.") from e

    # 멀티 프레임 TIFF 등은 프레임별로 처리
    pages = []
    for i in range(getattr(image, "n_frames", 1)):
        image.seek(i)
        pages.append({"page": i + 1, "method": "ocr", "text": ocr_image(image.copy(), lang)})
    return pages


def extract_from_pdf(data: bytes, lang: str) -> list[dict]:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise OcrError("PDF 파일을 읽을 수 없습니다.") from e

    with doc:
        if doc.page_count > settings.OCR_MAX_PAGES:
            raise OcrError(f"PDF 페이지 수가 너무 많습니다. (최대 {settings.OCR_MAX_PAGES}페이지)")

        pages = []
        for page in doc:
            text = page.get_text().strip()
            if len(text) >= MIN_TEXT_LAYER_CHARS:
                method = "text-layer"
            else:
                # 스캔본: 페이지를 이미지로 렌더링 후 OCR
                pix = page.get_pixmap(dpi=settings.OCR_PDF_DPI)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                text = ocr_image(image, lang)
                method = "ocr"
            pages.append({"page": page.number + 1, "method": method, "text": text})
        return pages


def extract_text(data: bytes, filename: str, content_type: str | None, lang: str) -> list[dict]:
    is_pdf = (content_type == "application/pdf") or filename.lower().endswith(".pdf")
    try:
        return extract_from_pdf(data, lang) if is_pdf else extract_from_image(data, lang)
    except pytesseract.TesseractNotFoundError as e:
        raise OcrError("Tesseract 가 설치되어 있지 않거나 경로(TESSERACT_CMD)가 잘못되었습니다.") from e
    except pytesseract.TesseractError as e:
        raise OcrError(f"OCR 처리 중 오류: {e.message}") from e
