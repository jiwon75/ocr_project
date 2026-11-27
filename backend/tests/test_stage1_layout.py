"""
[1단계 테스트] 블록 검출 → 읽기 순서 → 번호 부여가 맞는지 확인한다.
"""
import pytest

from app.pipeline.layout import assign_block_ids, label_to_type, sort_reading_order
from app.pipeline.preprocess import preprocess
from app.pipeline.storage import DocStorage, read_json
from app.pipeline.layout import run_layout
from tests import samples
from tests.fakes import FakeEngines


def test_uncovered_text_becomes_text_block():
    """레이아웃 모델이 놓친 글자(예: 영상 자막)는 블록 밖 글자 상자를 묶어 text 블록으로 만든다."""
    from app.pipeline.layout import uncovered_text_blocks

    class _Det:
        @staticmethod
        def detect_text(page):
            return [[10, 10, 90, 40],  # 표 블록 안 → 무시
                    [100, 300, 200, 340], [215, 302, 330, 338],  # 자막 한 줄이 두 조각으로 검출됨 → 한 블록
                    [600, 500, 640, 520]]  # 멀리 떨어진 글자 → 별도 블록
    blocks = [{"type": "table", "bbox": [0, 0, 400, 200]}]
    found = uncovered_text_blocks(_Det(), None, blocks, "ocr")
    assert [b["bbox"] for b in found] == [[100, 300, 330, 340], [600, 500, 640, 520]]
    assert all(b["type"] == "text" and b["label"] == "uncovered_text" for b in found)


def test_uncovered_grid_becomes_one_table_block():
    """레이아웃 모델이 표를 못 잡아도, 블록 밖 글자가 행·열로 가지런하면 낱개 블록이 아니라 표 블록 하나가 된다."""
    import numpy as np
    from app.pipeline.layout import uncovered_text_blocks

    grid = [[100 + c * 100, 100 + r * 50, 130 + c * 100, 130 + r * 50] for r in range(4) for c in range(3)]
    tiny = [[10, 980, 60, 990]]  # 페이지 높이의 1% — 플레이어 재생 시간 같은 작은 글자

    class _Det:
        @staticmethod
        def detect_text(page):
            return grid + tiny + [[300, 700, 600, 760]]  # 큰 자막 한 줄

    found = uncovered_text_blocks(_Det(), np.zeros((1000, 1000, 3), np.uint8), [], "ocr", 0.03)
    assert [(b["type"], b["bbox"]) for b in found] == [("table", [100, 100, 330, 280]), ("text", [300, 700, 600, 760])]


def _b(name, x1, y1, x2, y2):
    return {"name": name, "bbox": [x1, y1, x2, y2]}


def _names(blocks):
    return [b["name"] for b in blocks]


def test_single_column_top_to_bottom():
    blocks = [_b("c", 100, 500, 900, 600), _b("a", 100, 100, 900, 200), _b("b", 100, 300, 900, 400)]
    assert _names(sort_reading_order(blocks)) == ["a", "b", "c"]


def test_two_columns_left_then_right():
    """2단 문서: 왼쪽 단을 끝까지 읽은 다음 오른쪽 단을 읽어야 한다 (줄마다 왔다 갔다 하면 안 됨)."""
    blocks = [
        _b("R1", 520, 100, 900, 300), _b("L1", 100, 100, 480, 300),
        _b("R2", 520, 350, 900, 600), _b("L2", 100, 350, 480, 600),
    ]
    assert _names(sort_reading_order(blocks)) == ["L1", "L2", "R1", "R2"]


def test_title_then_two_columns_then_full_width_figure_then_two_columns():
    blocks = [
        _b("title", 100, 50, 900, 90),
        _b("L1", 100, 120, 480, 400), _b("R1", 520, 120, 900, 400),
        _b("figure", 100, 430, 900, 600),
        _b("L2", 100, 630, 480, 900), _b("R2", 520, 630, 900, 900),
    ]
    assert _names(sort_reading_order(blocks)) == ["title", "L1", "R1", "figure", "L2", "R2"]


def test_side_by_side_in_same_row_left_to_right():
    blocks = [_b("text", 100, 100, 900, 200), _b("img2", 520, 250, 900, 400), _b("img1", 100, 250, 480, 400)]
    assert _names(sort_reading_order(blocks)) == ["text", "img1", "img2"]


def test_block_id_format_and_global_order():
    pages = [(1, [{"type": "title"}, {"type": "text"}]), (2, [{"type": "text"}])]
    blocks = assign_block_ids(pages)
    assert [(b["block_id"], b["order"], b["page"]) for b in blocks] == [("p1-b001", 1, 1), ("p1-b002", 2, 1), ("p2-b001", 3, 2)]
    assert all(b["status"] == "pending" for b in blocks)


def test_label_mapping():
    assert label_to_type("doc_title") == "title"
    assert label_to_type("paragraph_title") == "title"
    assert label_to_type("table") == "table"
    assert label_to_type("chart") == "image"
    assert label_to_type("figure_title") == "caption"
    assert label_to_type("abstract") == "text"


def _layout(tmp_path, settings, path, file_type, engines):
    import uuid

    st = DocStorage(settings.STORAGE_DIR, str(uuid.uuid4()))
    prepared = preprocess(path, file_type, st, settings)
    structure = run_layout(prepared, st, engines, {"doc_id": st.doc_id, "file_name": path.name, "language": "auto"})
    return st, structure


def test_run_layout_writes_structure_json(tmp_path, settings):
    """가짜 엔진은 블록을 거꾸로 돌려준다 → 1단계가 직접 읽기 순서를 정해 번호를 붙이는지 확인."""
    st, structure = _layout(tmp_path, settings, samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    saved = read_json(st.structure_json)
    assert saved == structure
    assert saved["language"] == "auto"
    assert saved["pages"][0]["width"] == 1000
    assert [b["type"] for b in saved["blocks"]] == ["title", "text", "text", "text", "table", "image"]
    assert [b["block_id"] for b in saved["blocks"]] == [f"p1-b00{i}" for i in range(1, 7)]
    assert [b["order"] for b in saved["blocks"]] == [1, 2, 3, 4, 5, 6]
    assert saved["blocks"][0]["bbox"] == [100.0, 80.0, 900.0, 160.0]


def test_no_blocks_detected_falls_back_to_full_page(tmp_path, settings):
    st, structure = _layout(tmp_path, settings, samples.make_document_image(tmp_path / "doc.png"), "png", FakeEngines())
    assert len(structure["blocks"]) == 1
    assert structure["blocks"][0]["bbox"] == [0.0, 0.0, 1240.0, 1754.0]
    assert structure["blocks"][0]["layout_fallback"] is True


def test_hwpx_blocks_have_null_bbox(tmp_path, settings):
    st, structure = _layout(tmp_path, settings, samples.make_hwpx(tmp_path / "d.hwpx"), "hwpx", engines=None)
    assert [b["block_id"] for b in structure["blocks"]][:2] == ["p1-b001", "p1-b002"]
    assert all(b["bbox"] is None for b in structure["blocks"])  # [규칙 3]
    assert st.block_source("p1-b004").exists()  # 표 원문이 source 폴더에 저장됨


# ---------------- 실제 모델 ----------------


@pytest.mark.models
def test_real_layout_finds_title_text_table(tmp_path, settings, real_engines):
    st, structure = _layout(tmp_path, settings, samples.make_document_image(tmp_path / "doc.png"), "png", real_engines)
    types = [b["type"] for b in structure["blocks"]]
    assert types[0] == "title", types  # 맨 위 제목이 첫 블록
    assert "table" in types
    assert types.count("text") >= 2


@pytest.mark.models
def test_real_layout_two_column_reading_order(tmp_path, settings, real_engines):
    """2단 PDF 에서 왼쪽 단 문단들이 오른쪽 단 문단들보다 앞 번호를 받는지 (실제 레이아웃 모델 + 텍스트 레이어)."""
    from app.pipeline.preprocess import pdf_words_in_bbox

    path = samples.make_two_column_pdf(tmp_path / "two.pdf")
    st, structure = _layout(tmp_path, settings, path, "pdf", real_engines)
    page = structure["pages"][0]
    labels = []
    for b in structure["blocks"]:
        words = " ".join(l["text"] for l in pdf_words_in_bbox(path, 1, b["bbox"], page["scale"]))
        for tag in ("LEFT-1", "LEFT-2", "LEFT-3", "RIGHT-1", "RIGHT-2", "RIGHT-3"):
            if tag in words:
                labels.append(tag)
    assert labels == ["LEFT-1", "LEFT-2", "LEFT-3", "RIGHT-1", "RIGHT-2", "RIGHT-3"], labels
