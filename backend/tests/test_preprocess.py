"""
[전처리 테스트] 파일 형식별로 "페이지 목록"이 올바르게 만들어지는지 확인한다.
"""
import struct

import pytest

from app.pipeline.errors import PipelineError
from app.pipeline.hwp import decode_para_text, parse_hwp_records, parse_hwpx
from app.pipeline.preprocess import pdf_words_in_bbox, preprocess
from app.pipeline.storage import DocStorage
from tests import samples


@pytest.fixture
def storage(settings):
    import uuid

    st = DocStorage(settings.STORAGE_DIR, str(uuid.uuid4()))
    st.create_dirs()
    return st


def test_png_becomes_one_page(tmp_path, storage, settings):
    path = samples.make_document_image(tmp_path / "doc.png")
    prepared = preprocess(path, "png", storage, settings)
    assert prepared["kind"] == "image"
    assert prepared["pages"] == [{"page": 1, "width": 1240, "height": 1754, "text_layer": False, "scale": 1.0}]
    assert storage.page_image(1).exists()


def test_jpg_becomes_one_page(tmp_path, storage, settings):
    from PIL import Image

    path = tmp_path / "doc.jpg"
    Image.open(samples.make_document_image(tmp_path / "doc.png")).save(path, "JPEG")
    prepared = preprocess(path, "jpg", storage, settings)
    assert len(prepared["pages"]) == 1


def test_digital_pdf_has_text_layer(tmp_path, storage, settings):
    path = samples.make_digital_pdf(tmp_path / "digital.pdf", pages=2)
    prepared = preprocess(path, "pdf", storage, settings)
    assert [p["page"] for p in prepared["pages"]] == [1, 2]
    assert all(p["text_layer"] for p in prepared["pages"])  # 디지털 원본 → OCR 대신 직접 추출 대상
    assert storage.page_image(2).exists()


def test_text_layer_words_extracted_inside_bbox(tmp_path, storage, settings):
    path = samples.make_digital_pdf(tmp_path / "digital.pdf")
    prepared = preprocess(path, "pdf", storage, settings)
    page = prepared["pages"][0]
    lines = pdf_words_in_bbox(path, 1, [0, 0, page["width"], page["height"]], page["scale"])
    text = "\n".join(l["text"] for l in lines)
    assert "텍스트 레이어에서 직접 추출되는 문장입니다." in text
    assert all(l["confidence"] is None for l in lines)  # OCR 이 아니므로 confidence 없음


def test_scanned_pdf_has_no_text_layer(tmp_path, storage, settings):
    img = samples.make_document_image(tmp_path / "doc.png")
    path = samples.make_scanned_pdf(tmp_path / "scan.pdf", img)
    prepared = preprocess(path, "pdf", storage, settings)
    assert prepared["pages"][0]["text_layer"] is False  # 스캔본 → OCR 대상


def test_corrupted_pdf_fails_in_preprocess(tmp_path, storage, settings):
    path = samples.make_corrupted_pdf(tmp_path / "bad.pdf")
    with pytest.raises(PipelineError) as e:
        preprocess(path, "pdf", storage, settings)
    assert e.value.stage == "preprocess" and e.value.reason == "corrupted_file"


def test_corrupted_image_fails_in_preprocess(tmp_path, storage, settings):
    path = tmp_path / "bad.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 100)
    with pytest.raises(PipelineError) as e:
        preprocess(path, "png", storage, settings)
    assert e.value.reason == "corrupted_file"


def test_too_many_pages(tmp_path, storage, settings):
    settings.MAX_PAGES = 2
    path = samples.make_digital_pdf(tmp_path / "many.pdf", pages=3)
    with pytest.raises(PipelineError) as e:
        preprocess(path, "pdf", storage, settings)
    assert e.value.reason == "too_many_pages"
    assert "2페이지" in e.value.message  # 안내 문장의 숫자도 설정값에서 온다


# ---------------- HWPX / HWP ----------------


def test_hwpx_parse_text_title_table_header(tmp_path):
    blocks = parse_hwpx(samples.make_hwpx(tmp_path / "doc.hwpx"))
    assert [b["type"] for b in blocks] == ["header", "title", "text", "table", "text"]
    assert blocks[1]["text"] == "HWPX 문서 제목"  # "개요 1" 스타일 → title
    assert blocks[2]["text"] == "첫 번째 문단입니다. 이어지는\t글자."
    table = blocks[3]["table"]
    assert (table["n_rows"], table["n_cols"]) == (3, 3)
    first = next(c for c in table["cells"] if c["text"] == "구분")
    assert (first["row"], first["col"], first["colspan"]) == (0, 0, 2)  # 병합 셀 정보 유지
    q1 = next(c for c in table["cells"] if c["text"] == "1분기")
    assert q1["rowspan"] == 2


def test_hwpx_preprocess_has_no_page_size(tmp_path, storage, settings):
    prepared = preprocess(samples.make_hwpx(tmp_path / "doc.hwpx"), "hwpx", storage, settings)
    # [규칙 3] 좌표/페이지 크기를 모르면 추측하지 않고 null
    assert prepared["pages"][0]["width"] is None and prepared["pages"][0]["height"] is None


def test_broken_hwpx_fails(tmp_path, storage, settings):
    path = tmp_path / "bad.hwpx"
    path.write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(PipelineError):
        preprocess(path, "hwpx", storage, settings)


def _record(tag: int, level: int, data: bytes) -> bytes:
    """HWP 레코드 하나를 바이트로 만든다 (헤더 = 태그 10비트 | 깊이 10비트 | 크기 12비트)."""
    return struct.pack("<I", tag | (level << 10) | (len(data) << 20)) + data


def _text(s: str) -> bytes:
    return s.encode("utf-16-le")


def test_hwp_binary_records_paragraph_and_table():
    """
    실제 .hwp 파일은 한글 프로그램 없이 만들 수 없어서, 본문 레코드를 직접 조립해 파서를 검사한다.
    문단 → 표(2x2, 첫 행 병합) → 문단
    """
    table_ctrl = b"tbl "[::-1] + b"\0" * 4
    table_rec = struct.pack("<IHH", 0, 2, 2)

    def cell(col, row, colspan, rowspan):
        return struct.pack("<HHI", 1, 0, 0) + struct.pack("<HHHH", col, row, colspan, rowspan) + b"\0" * 8

    ctrl_char = struct.pack("<H", 11) + b"\0" * 14  # 표 자리의 확장 제어 문자 (8글자 크기) → 건너뛰어야 함
    data = b"".join(
        [
            _record(66, 0, b"\0" * 8),
            _record(67, 1, _text("첫 문단\r")),
            _record(66, 0, b"\0" * 8),
            _record(67, 1, ctrl_char + _text("\r")),
            _record(71, 1, table_ctrl),
            _record(77, 2, table_rec),
            _record(72, 2, cell(0, 0, 2, 1)),
            _record(66, 2, b"\0" * 8),
            _record(67, 3, _text("제목칸")),
            _record(72, 2, cell(0, 1, 1, 1)),
            _record(66, 2, b"\0" * 8),
            _record(67, 3, _text("A")),
            _record(72, 2, cell(1, 1, 1, 1)),
            _record(66, 2, b"\0" * 8),
            _record(67, 3, _text("B")),
            _record(66, 0, b"\0" * 8),
            _record(67, 1, _text("끝 문단\r")),
        ]
    )
    blocks = parse_hwp_records(data)
    assert [b["type"] for b in blocks] == ["text", "table", "text"]
    assert blocks[0]["text"] == "첫 문단"
    cells = blocks[1]["table"]["cells"]
    assert [(c["row"], c["col"], c["colspan"], c["text"]) for c in cells] == [(0, 0, 2, "제목칸"), (1, 0, 1, "A"), (1, 1, 1, "B")]
    assert blocks[2]["text"] == "끝 문단"


def test_hwp_para_text_skips_control_chars():
    data = _text("가") + struct.pack("<H", 11) + b"\0" * 14 + _text("나") + struct.pack("<H", 9) + b"\0" * 14 + _text("다")
    assert decode_para_text(data) == "가나\t다"
