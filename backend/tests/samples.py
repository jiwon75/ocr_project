"""
테스트용 샘플 파일 만들기.
실제 문서 파일을 저장소에 넣어 두지 않고 테스트할 때마다 코드로 만든다.
(저작권 걱정이 없고, 어떤 내용이 들어 있는지 코드만 보고 알 수 있어서 테스트 기대값을 정하기 쉽다)
"""
import io
import os
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

KOREAN_FONT = Path(r"C:\Windows\Fonts\malgun.ttf")


def font(size: int):
    if KOREAN_FONT.exists():
        return ImageFont.truetype(str(KOREAN_FONT), size)
    return ImageFont.load_default(size)


# ---------------------------------------------------------------------
# 가짜 엔진(tests/fakes.py)용 페이지: 블록마다 다른 회색으로 칠해 두면, 가짜 엔진이 색만 보고 어떤 블록인지 안다
# ---------------------------------------------------------------------
FAKE_PAGE_SIZE = (1000, 1400)
FAKE_BLOCKS = [
    # (label, bbox, 회색값)
    ("doc_title", (100, 80, 900, 160), 40),
    ("text", (100, 200, 900, 400), 80),
    ("text", (100, 450, 900, 550), 120),  # 낮은 confidence
    ("text", (100, 600, 900, 650), 160),  # 빈 결과
    ("table", (100, 700, 900, 1000), None),  # 셀마다 다른 색 (아래)
    ("image", (100, 1050, 500, 1350), 230),
]
TABLE_CELL_GRAYS = [200, 205, 210, 215]  # 왼쪽 위, 오른쪽 위, 왼쪽 아래, 오른쪽 아래


def make_fake_page(path: Path, marker: tuple | None = None) -> Path:
    """
    가짜 엔진이 알아보는 페이지 PNG. marker 색을 (0,0) 픽셀에 칠하면 가짜 엔진이 특별 동작을 한다
    (빨강: 아주 느림 → 시간 초과 테스트, 파랑: 워커 강제 종료 → 비정상 종료 테스트)
    """
    img = Image.new("RGB", FAKE_PAGE_SIZE, "white")
    d = ImageDraw.Draw(img)
    for label, (x1, y1, x2, y2), gray in FAKE_BLOCKS:
        if label == "table":
            mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            quads = [(x1, y1, mx, my), (mx, y1, x2, my), (x1, my, mx, y2), (mx, my, x2, y2)]
            for q, g in zip(quads, TABLE_CELL_GRAYS):
                d.rectangle(q, fill=(g, g, g))
        else:
            d.rectangle((x1, y1, x2, y2), fill=(gray, gray, gray))
    if marker:
        d.rectangle((0, 0, 3, 3), fill=marker)
    img.save(path)
    return path


# ---------------------------------------------------------------------
# 실제 모델용 샘플
# ---------------------------------------------------------------------


def make_document_image(path: Path) -> Path:
    """제목 + 한/영 문단 + 3x3 표 + 문단이 있는 A4 비율 이미지 (실제 OCR 확인용)."""
    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    d.text((100, 100), "문서 처리 테스트 보고서", fill="black", font=font(56))
    body = font(28)
    y = 230
    for line in ["이 문서는 OCR 파이프라인을 확인하기 위한 샘플입니다.", "Korean and English text are mixed in this paragraph.", "레이아웃 분석 후 블록 단위로 텍스트를 추출합니다."]:
        d.text((100, y), line, fill="black", font=body)
        y += 48
    x0, y0, cw, ch = 100, 500, 330, 70
    rows = [["항목", "수량", "Price"], ["사과", "10", "1,000"], ["Banana", "5", "2,500"]]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            d.rectangle([x0 + c * cw, y0 + r * ch, x0 + (c + 1) * cw, y0 + (r + 1) * ch], outline="black", width=2)
            d.text((x0 + c * cw + 20, y0 + r * ch + 18), cell, fill="black", font=body)
    y = 800
    for line in ["표 아래에 있는 두 번째 문단입니다.", "End of the sample document."]:
        d.text((100, y), line, fill="black", font=body)
        y += 48
    img.save(path)
    return path


def _pdf_font_kwargs():
    # PyMuPDF 내장 한국어 글꼴. 맑은 고딕 파일을 넣으면 글꼴 전체(수 MB)가 PDF 에 들어가 5MB 제한을 넘어 버린다
    return {"fontname": "korea"}


def make_digital_pdf(path: Path, pages: int = 1) -> Path:
    """텍스트 레이어가 있는 PDF (워드에서 PDF 로 저장한 것과 같은 종류)."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 90), f"디지털 PDF 제목 {i + 1}", fontsize=20, **_pdf_font_kwargs())
        page.insert_text((72, 140), "텍스트 레이어에서 직접 추출되는 문장입니다.", fontsize=12, **_pdf_font_kwargs())
        page.insert_text((72, 160), "This sentence comes from the text layer.", fontsize=12, **_pdf_font_kwargs())
    doc.save(path)
    return path


def make_two_column_pdf(path: Path) -> Path:
    """
    2단 편집 PDF. 왼쪽 단에 LEFT-1..3, 오른쪽 단에 RIGHT-1..3 문단.
    읽기 순서가 맞다면 결과에서 모든 LEFT 가 RIGHT 보다 먼저 나와야 한다.
    """
    doc = pymupdf.open()
    page = doc.new_page()  # 595 x 842 pt
    page.insert_text((60, 70), "Two Column Layout Test", fontsize=22)
    filler = "This paragraph belongs to the {} column and is long enough to wrap."
    for i in range(3):
        y = 130 + i * 200
        for col, x in (("LEFT", 50), ("RIGHT", 310)):
            rect = pymupdf.Rect(x, y, x + 235, y + 170)
            page.insert_textbox(rect, f"{col}-{i + 1}. " + " ".join([filler.format(col.lower())] * 4), fontsize=10)
    doc.save(path)
    return path


def make_scanned_pdf(path: Path, image_path: Path) -> Path:
    """이미지만 들어 있는 PDF (스캔본). 텍스트 레이어가 없다."""
    doc = pymupdf.open()
    with Image.open(image_path) as im:
        w, h = im.size
        buf = io.BytesIO()
        im.save(buf, "PNG")
    page = doc.new_page(width=w * 72 / 150, height=h * 72 / 150)
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(path)
    return path


def make_hwpx(path: Path) -> Path:
    """
    HWPX 샘플: 제목 스타일 문단, 일반 문단, 병합 셀이 있는 표, 머리말.
    한글 프로그램이 만드는 파일보다 단순하지만 구조(section/p/run/t/tbl/tc/cellAddr/cellSpan)는 같다.
    """
    hp = "http://www.hancom.co.kr/hwpml/2011/paragraph"
    hh = "http://www.hancom.co.kr/hwpml/2011/head"
    hs = "http://www.hancom.co.kr/hwpml/2011/section"
    header = f"""<?xml version="1.0" encoding="UTF-8"?>
<hh:head xmlns:hh="{hh}"><hh:refList><hh:styles>
  <hh:style id="0" name="바탕글" engName="Normal"/>
  <hh:style id="1" name="개요 1" engName="Outline 1"/>
</hh:styles></hh:refList></hh:head>"""

    def cell(r, c, text, rs=1, cs=1):
        return (
            f'<hp:tc><hp:subList><hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p></hp:subList>'
            f'<hp:cellAddr colAddr="{c}" rowAddr="{r}"/><hp:cellSpan colSpan="{cs}" rowSpan="{rs}"/></hp:tc>'
        )

    table = (
        '<hp:tbl rowCnt="3" colCnt="3">'
        f'<hp:tr>{cell(0, 0, "구분", cs=2)}{cell(0, 2, "합계")}</hp:tr>'
        f'<hp:tr>{cell(1, 0, "1분기", rs=2)}{cell(1, 1, "국내")}{cell(1, 2, "100")}</hp:tr>'
        f'<hp:tr>{cell(2, 1, "해외")}{cell(2, 2, "200")}</hp:tr>'
        "</hp:tbl>"
    )
    section = f"""<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="{hs}" xmlns:hp="{hp}">
  <hp:p styleIDRef="0"><hp:run><hp:ctrl><hp:header><hp:subList><hp:p><hp:run><hp:t>머리말 텍스트</hp:t></hp:run></hp:p></hp:subList></hp:header></hp:ctrl></hp:run></hp:p>
  <hp:p styleIDRef="1"><hp:run><hp:t>HWPX 문서 제목</hp:t></hp:run></hp:p>
  <hp:p styleIDRef="0"><hp:run><hp:t>첫 번째 문단입니다.</hp:t><hp:t> 이어지는<hp:tab/>글자.</hp:t></hp:run></hp:p>
  <hp:p styleIDRef="0"><hp:run>{table}</hp:run></hp:p>
  <hp:p styleIDRef="0"><hp:run><hp:t>표 다음 문단입니다.</hp:t></hp:run></hp:p>
</hs:sec>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("Contents/header.xml", header)
        zf.writestr("Contents/section0.xml", section)
    return path


def make_corrupted_pdf(path: Path) -> Path:
    """앞부분만 PDF 처럼 보이고 내용은 깨진 파일. 업로드 검사(매직 넘버)는 통과하지만 열리지 않는다."""
    path.write_bytes(b"%PDF-1.7\n" + os.urandom(2048))
    return path


def make_big_file(path: Path, size_mb: float) -> Path:
    """size_mb 보다 조금 큰 PNG 처럼 보이는 파일 (크기 제한 테스트용)."""
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * int(size_mb * 1024 * 1024 + 10))
    return path
