"""
[2단계 테스트] 블록 crop 저장, 종류별 처리, 결과 JSON, 실패 격리를 확인한다.
"""
import pytest

from app.pipeline.engines import pick_language
from app.pipeline.processors.table import assign_lines_to_cells, parse_table_tokens, reread_empty_cells, tidy_table
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


def test_auto_language_keeps_korean_when_english_drops_hangul():
    """영어 모델은 '원'을 몰라 '324'로 읽고도 점수가 더 높다. 한국어 모델이 한글을 확실히 읽었으면 한국어를 쓴다."""
    cands = {"ko": {"text": "324원", "confidence": 0.993}, "en": {"text": "324", "confidence": 0.9999}}
    assert pick_language(cands, 0.8) == "ko"
    # 한글이 없는 줄(숫자만)은 여전히 점수가 높은 쪽
    assert pick_language({"ko": {"text": "2", "confidence": 0.997}, "en": {"text": "2", "confidence": 0.99998}}, 0.8) == "en"
    # 한국어 점수가 기준 미달이면 한글이 있어도 우선하지 않는다
    assert pick_language({"ko": {"text": "가", "confidence": 0.5}, "en": {"text": "A", "confidence": 0.9}}, 0.8) == "en"


def _cell(row, col, box, texts=(), colspan=1):
    lines = [{"text": t, "confidence": 0.99, "box": box} for t in texts]
    return {"row": row, "col": col, "rowspan": 1, "colspan": colspan, "box": box, "_lines": lines}


def test_tidy_table_caption_empty_row_and_zigzag_columns():
    """실제 사례: 3열 표를 구조 모델이 4열로 나눠 가격이 3·4열에 번갈아 들어가고, 제목은 전체 병합 행, 맨 아래는 빈 병합 행."""
    cells = [
        _cell(0, 0, [0, 0, 880, 58], ["가격 비교"], colspan=4),
        _cell(1, 0, [7, 77, 241, 125], ["순위"]), _cell(1, 1, [223, 78, 518, 123], ["브랜드"]),
        _cell(1, 2, [480, 78, 726, 124]), _cell(1, 3, [630, 77, 870, 124], ["100mL당 가격"]),
        _cell(2, 0, [12, 125, 237, 172], ["1"]), _cell(2, 1, [230, 123, 532, 171], ["줌"]),
        _cell(2, 2, [502, 123, 764, 171], ["168원"]), _cell(2, 3, [714, 122, 877, 171]),
        _cell(3, 0, [8, 170, 215, 220], ["2"]), _cell(3, 1, [228, 168, 520, 219], ["탐사"]),
        _cell(3, 2, [483, 169, 713, 215]), _cell(3, 3, [629, 168, 875, 212], ["180원"]),
        _cell(4, 0, [3, 569, 849, 605], colspan=4),
    ]
    cells, n_rows, n_cols, caption = tidy_table(cells, 5, 4)
    assert caption["_lines"][0]["text"] == "가격 비교"
    assert (n_rows, n_cols) == (3, 3)
    grid = {(c["row"], c["col"]): [l["text"] for l in c["_lines"]] for c in cells}
    assert grid == {(0, 0): ["순위"], (0, 1): ["브랜드"], (0, 2): ["100mL당 가격"],
                    (1, 0): ["1"], (1, 1): ["줌"], (1, 2): ["168원"],
                    (2, 0): ["2"], (2, 1): ["탐사"], (2, 2): ["180원"]}


def test_tidy_table_keeps_real_sparse_columns():
    """칸이 나란히 붙어 있을 뿐 겹치지 않는 진짜 두 열은 행마다 한쪽만 차 있어도 합치지 않는다."""
    cells = [_cell(0, 0, [0, 0, 100, 30], ["예"]), _cell(0, 1, [100, 0, 200, 30]),
             _cell(1, 0, [0, 30, 100, 60]), _cell(1, 1, [100, 30, 200, 60], ["아니오"])]
    _, n_rows, n_cols, caption = tidy_table(cells, 2, 2)
    assert (n_rows, n_cols, caption) == (2, 2, None)


def test_reread_empty_cell_fills_missed_digit():
    """글자 검출이 놓친 칸만 그 열·행 범위로 잘라 다시 읽고, 기준 미달·의미 없는 글자는 채우지 않는다."""
    import numpy as np

    class _Ctx:
        language = "auto"

        class engines:
            @staticmethod
            def recognize(images, language):
                assert len(images) == 3  # 빈 칸 3개: (1,0) (2,1) (3,0)
                return [{"text": "1", "confidence": 0.99, "lang": "en", "candidates": None},
                        {"text": "—", "confidence": 0.95, "lang": "ko", "candidates": None},
                        {"text": "7", "confidence": 0.30, "lang": "en", "candidates": None}]

    cells = [_cell(0, 0, [0, 0, 50, 30], ["순위"]), _cell(0, 1, [50, 0, 150, 30], ["브랜드"]),
             _cell(1, 0, [0, 30, 50, 60]), _cell(1, 1, [50, 30, 150, 60], ["줌"]),
             _cell(2, 0, [0, 60, 50, 90], ["2"]), _cell(2, 1, [50, 60, 150, 90]),
             _cell(3, 0, [0, 90, 50, 120]), _cell(3, 1, [50, 90, 150, 120], ["탐사"])]
    cells[1 * 2 + 1]["_lines"][0]["box"] = [60, 33, 100, 57]
    cells[3 * 2 + 1]["_lines"][0]["box"] = [60, 93, 100, 117]
    found = reread_empty_cells(_Ctx(), np.zeros((120, 150, 3), np.uint8), cells, 0.8)
    assert [l["text"] for l in found] == ["1"]
    assert [l["text"] for l in cells[2]["_lines"]] == ["1"] and cells[2]["_lines"][0]["reread"]
    assert cells[5]["_lines"] == []  # "—" 는 글자·숫자가 아니라 채우지 않는다
    assert cells[6]["_lines"] == []  # 점수가 기준 미달이면 채우지 않는다


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
