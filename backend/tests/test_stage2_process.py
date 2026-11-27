"""
[2단계 테스트] 블록 crop 저장, 종류별 처리, 결과 JSON, 실패 격리를 확인한다.
"""
import pytest

from app.pipeline.processors.table import assign_lines_to_cells, parse_table_tokens
from app.pipeline.storage import read_json
from tests import samples
from tests.fakes import FakeEngines


def _results(storage, structure):
    return {b["block_id"]: read_json(storage.block_result(b["block_id"])) for b in structure["blocks"]}


def test_each_block_has_crop_and_result_json(tmp_path, run_doc):
    doc_id, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    structure = read_json(st.structure_json)
    for b in structure["blocks"]:
        assert st.block_image(b["block_id"]).exists(), b["block_id"]  # storage/{doc_id}/blocks/{block_id}.png
        assert st.block_result(b["block_id"]).exists(), b["block_id"]  # storage/{doc_id}/results/{block_id}.json


def test_text_block_ok_with_confidence(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    r = read_json(st.block_result("p1-b002"))
    assert r["status"] == "done"
    assert r["text"] == "첫 번째 문단입니다."
    assert r["confidence"] == 0.97
    assert r["source"] == "ocr"


def test_table_cells_and_low_confidence_cell(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    r = read_json(st.block_result("p1-b005"))
    assert r["type"] == "table" and r["status"] == "done"
    cells = {(c["row"], c["col"]): c for c in r["table"]["cells"]}
    assert cells[(0, 0)]["text"] == "항목" and cells[(1, 0)]["text"] == "사과"
    low = cells[(1, 1)]
    assert low["status"] == "failed" and low["text"] is None
    assert low["raw_text"] == "흐린셀" and low["confidence"] == 0.30  # 원본은 남긴다
    assert r["failed_cells"] == 1


def test_image_block_saves_image_and_inner_text(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    r = read_json(st.block_result("p1-b006"))
    assert r["type"] == "image" and r["status"] == "done"
    assert r["image_path"] == "p1-b006.png"
    assert r["ocr_text"] == "그림 속 글자"


def test_one_block_error_does_not_stop_others(tmp_path, run_doc, monkeypatch):
    """블록 하나가 오류를 내도 그 블록만 failed 로 남고, 나머지 블록은 계속 처리된다."""
    engines = FakeEngines(flaky=True)
    from tests import fakes

    monkeypatch.setitem(fakes._flaky_state, "failed_once", False)
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", engines)
    structure = read_json(st.structure_json)
    statuses = {b["block_id"]: b["status"] for b in structure["blocks"]}
    assert statuses["p1-b002"] == "failed"
    r = read_json(st.block_result("p1-b002"))
    assert r["failure_reason"] == "error" and "일시적 오류" in r["failure_detail"]
    assert statuses["p1-b001"] == "done" and statuses["p1-b006"] == "done"  # 뒤 블록도 처리됨
    assert read_json(st.status_json)["state"] == "done"  # 블록 실패는 파일 전체 실패가 아니다


def test_digital_pdf_uses_text_layer_not_ocr(tmp_path, run_doc):
    engines = FakeEngines()
    _, _, st, _ = run_doc(samples.make_digital_pdf(tmp_path / "d.pdf"), "pdf", engines)
    r = read_json(st.block_result("p1-b001"))
    assert r["source"] == "text-layer"
    assert "텍스트 레이어에서 직접 추출되는 문장입니다." in r["text"]
    assert r["confidence"] is None
    assert engines.calls["recognize"] == 0  # OCR 모델을 부르지 않았다


def test_hwpx_table_keeps_spans(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_hwpx(tmp_path / "d.hwpx"), "hwpx", engines=None)
    r = read_json(st.block_result("p1-b004"))
    assert r["source"] == "hwp" and r["status"] == "done"
    spans = {c["text"]: (c["rowspan"], c["colspan"]) for c in r["table"]["cells"]}
    assert spans["구분"] == (1, 2) and spans["1분기"] == (2, 1)


def test_parse_table_tokens_with_spans():
    tokens = ["<tr>", "<td", ' colspan="2"', ">", "</td>", "<td></td>", "</tr>",
              "<tr>", "<td", ' rowspan="2"', ">", "</td>", "<td></td>", "<td></td>", "</tr>",
              "<tr>", "<td></td>", "<td></td>", "</tr>"]
    cells, n_rows, n_cols = parse_table_tokens(tokens)
    assert (n_rows, n_cols) == (3, 3)
    assert [(c["row"], c["col"], c["rowspan"], c["colspan"]) for c in cells] == [
        (0, 0, 1, 2), (0, 2, 1, 1), (1, 0, 2, 1), (1, 1, 1, 1), (1, 2, 1, 1), (2, 1, 1, 1), (2, 2, 1, 1)
    ]  # 2행의 첫 칸은 위에서 rowspan 으로 차지됐으므로 col=1 부터 배치


def test_assign_lines_does_not_guess_ambiguous_line():
    cells = [[0, 0, 100, 50], [100, 0, 200, 50]]
    lines = [{"text": "A", "box": [10, 10, 60, 40]}, {"text": "걸침", "box": [60, 10, 140, 40]}]
    per_cell, unassigned = assign_lines_to_cells(lines, cells)
    assert [l["text"] for l in per_cell[0]] == ["A"]
    assert [l["text"] for l in unassigned] == ["걸침"]  # 두 칸에 반씩 걸친 줄은 억지로 넣지 않는다


# ---------------- 실제 모델 ----------------


@pytest.mark.models
def test_real_ocr_korean_english_and_table(tmp_path, run_doc, real_engines):
    _, _, st, _ = run_doc(samples.make_document_image(tmp_path / "doc.png"), "png", real_engines)
    structure = read_json(st.structure_json)
    results = _results(st, structure)
    texts = "\n".join(r.get("raw_text") or "" for r in results.values())
    assert "파이프라인" in texts
    assert "Korean and English text are mixed" in texts
    table = next(r for r in results.values() if r["type"] == "table")
    cell_texts = {c.get("raw_text") for c in table["table"]["cells"]}
    assert {"항목", "사과", "Banana"} <= cell_texts
    assert (table["table"]["n_rows"], table["table"]["n_cols"]) == (3, 3)


@pytest.mark.models
def test_real_ocr_english_only_language(tmp_path, run_doc, real_engines):
    _, _, st, _ = run_doc(samples.make_document_image(tmp_path / "doc.png"), "png", real_engines, language="en")
    structure = read_json(st.structure_json)
    assert structure["language"] == "en"
    langs = {l.get("lang") for b in structure["blocks"] for l in (read_json(st.block_result(b["block_id"])) or {}).get("lines", [])}
    assert langs <= {"en", None}
