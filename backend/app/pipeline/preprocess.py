"""
[전처리 단계 — 이 파일이 하는 일]
업로드된 파일을 형식에 상관없이 "페이지 목록"이라는 같은 모양으로 바꾼다.
  - png/jpg : 그대로 1페이지 이미지
  - pdf     : 페이지마다 이미지로 변환(PyMuPDF). 글자 정보(텍스트 레이어)가 있는 페이지인지도 표시해 둔다
  - hwp/hwpx: 직접 파싱(hwp.py)해서 문단·표 목록을 얻는다. 페이지 이미지와 좌표는 없다

[왜 이렇게 나눴는지]
1단계(구조 분석)부터는 "페이지 이미지 + 페이지 정보"만 알면 되도록 만들기 위해서다.
새 형식을 지원하려면 이 파일에 변환 함수 하나만 추가하면 되고, 1~3단계 코드는 바꿀 필요가 없다.

PDF 텍스트 레이어: 워드 등에서 "PDF 로 저장"한 디지털 원본에는 글자가 글자 그대로 들어 있다.
이런 페이지는 OCR 보다 직접 꺼내는 편이 빠르고 100% 정확하다. 스캔본(종이를 찍은 사진)에는 글자 정보가 없어서 OCR 해야 한다.
"""
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps

from app.config import Settings
from app.pipeline.errors import PipelineError, user_message
from app.pipeline.hwp import parse_hwp, parse_hwpx
from app.pipeline.storage import DocStorage

IMAGE_TYPES = ("png", "jpg")


def preprocess(file_path: Path, file_type: str, storage: DocStorage, settings: Settings) -> dict:
    """
    받는 값: 원본 파일 경로, 파일 형식(png/jpg/pdf/hwp/hwpx), 저장소, 설정
    돌려주는 값:
      {"kind": "image"|"pdf"|"hwp",
       "pages": [{"page": 1, "width": 1240, "height": 1754, "text_layer": False, "scale": 1.0}],
       "hwp_blocks": [...] 또는 None}
    실패하면 PipelineError(stage="preprocess") 를 던진다.
    """
    storage.create_dirs()
    if file_type in IMAGE_TYPES:
        return _from_image(file_path, storage)
    if file_type == "pdf":
        return _from_pdf(file_path, storage, settings)
    if file_type in ("hwp", "hwpx"):
        blocks = parse_hwpx(file_path) if file_type == "hwpx" else parse_hwp(file_path)
        if not blocks:
            raise PipelineError("preprocess", "empty_document", user_message("empty_document"))
        # [규칙 3] HWP 는 페이지 크기·좌표를 알 수 없으므로 null 로 둔다 (A4 크기 등으로 추측하지 않는다)
        return {"kind": "hwp", "pages": [{"page": 1, "width": None, "height": None, "text_layer": True, "scale": None}], "hwp_blocks": blocks}
    raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file"))


def _from_image(file_path: Path, storage: DocStorage) -> dict:
    """이미지 파일 → 1페이지. 깨진 파일은 여기서 걸러낸다."""
    try:
        with Image.open(file_path) as img:
            img.load()  # 실제로 픽셀을 끝까지 읽어 본다. open 만 하면 파일 앞부분만 보고 성공해서, 뒤가 잘린 파일을 못 걸러낸다
            # 휴대폰 사진은 "회전 정보(EXIF)"만 붙어 있고 픽셀은 누워 있는 경우가 많다. 실제로 돌려서 저장해야 좌표가 맞는다
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.save(storage.page_image(1))
            width, height = img.size
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file")) from e
    return {"kind": "image", "pages": [{"page": 1, "width": width, "height": height, "text_layer": False, "scale": 1.0}], "hwp_blocks": None}


def _from_pdf(file_path: Path, storage: DocStorage, settings: Settings) -> dict:
    """PDF → 페이지마다 PNG. 텍스트 레이어 유무도 함께 기록한다."""
    try:
        doc = pymupdf.open(file_path)
    except Exception as e:  # PyMuPDF 는 손상 종류에 따라 여러 오류를 던져서 넓게 잡는다
        raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file")) from e

    with doc:
        if doc.needs_pass:
            raise PipelineError("preprocess", "encrypted", user_message("encrypted"))
        if doc.page_count == 0:
            raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file"))
        if doc.page_count > settings.MAX_PAGES:
            # 업로드 API 에서도 검사하지만, 다른 경로로 들어온 파일도 막기 위해 한 번 더 검사한다
            raise PipelineError("preprocess", "too_many_pages", user_message("too_many_pages", max_pages=settings.MAX_PAGES))

        # PDF 좌표 단위는 1/72인치(포인트). 이미지로 바꾸면 DPI/72 배 커지므로, 나중에 좌표를 맞출 때 이 배율이 필요하다
        scale = settings.PDF_RENDER_DPI / 72
        pages = []
        for page in doc:
            try:
                pix = page.get_pixmap(dpi=settings.PDF_RENDER_DPI)
                pix.save(storage.page_image(page.number + 1))
                text_layer = len(page.get_text().strip()) >= settings.PDF_TEXT_LAYER_MIN_CHARS
            except Exception as e:
                raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file")) from e
            pages.append({"page": page.number + 1, "width": pix.width, "height": pix.height, "text_layer": text_layer, "scale": scale})
    return {"kind": "pdf", "pages": pages, "hwp_blocks": None}


def pdf_words_in_bbox(pdf_path: Path, page_no: int, bbox_px, scale: float) -> list[dict]:
    """
    디지털 PDF 페이지에서 bbox(픽셀 좌표) 안에 있는 단어를 꺼내 "줄" 목록으로 돌려준다.
    2단계에서 텍스트 레이어 페이지를 처리할 때 쓴다.

    돌려주는 값: [{"text": "한 줄", "confidence": None, "box": [x1,y1,x2,y2](픽셀)}]
      confidence 가 None 인 이유: OCR 로 읽은 게 아니라 파일에 저장된 글자라서 "신뢰도"라는 개념이 없기 때문.

    단어의 "가운데 점"이 bbox 안에 있을 때만 포함한다. 단어가 블록 경계에 걸쳐 있으면
    양쪽 블록에 두 번 들어가는 일을 막기 위해서다.
    """
    x1, y1, x2, y2 = (v / scale for v in bbox_px)
    with pymupdf.open(pdf_path) as doc:
        words = doc[page_no - 1].get_text("words")  # (x0, y0, x1, y1, 단어, 블록번호, 줄번호, 단어번호)
    lines: dict[tuple, list] = {}
    for wx0, wy0, wx1, wy1, word, block_no, line_no, word_no in words:
        cx, cy = (wx0 + wx1) / 2, (wy0 + wy1) / 2
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            lines.setdefault((block_no, line_no), []).append((word_no, wx0, wy0, wx1, wy1, word))
    result = []
    for key in sorted(lines, key=lambda k: (min(w[2] for w in lines[k]), min(w[1] for w in lines[k]))):
        ws = sorted(lines[key])
        box = [min(w[1] for w in ws) * scale, min(w[2] for w in ws) * scale, max(w[3] for w in ws) * scale, max(w[4] for w in ws) * scale]
        result.append({"text": " ".join(w[5] for w in ws), "confidence": None, "box": [round(v, 1) for v in box]})
    return result
