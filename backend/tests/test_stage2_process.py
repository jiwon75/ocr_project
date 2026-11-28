"""
[2단계 테스트] 블록 crop 저장, 종류별 처리, 결과 JSON, 실패 격리를 확인한다.
"""
import pytest

from app.pipeline.engines import pick_language
from app.pipeline.processors.table import assign_lines_to_cells, parse_table_tokens, grid_from_lines, reread_empty_cells, structure_agrees, tidy_table
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
                assert len(images) == 12  # 빈 칸 3개 (1,0) (2,1) (3,0) × 자르는 폭 2가지 × (원본, 보정)
                def r(t, c):
                    return {"text": t, "confidence": c, "lang": "en", "candidates": None}
                return [r("1", 0.70), r("1", 0.60), r("1", 0.99), r("1", 0.50),  # 넷 중 점수가 높은 쪽을 쓴다
                        r("—", 0.95), r("", 0.0), r("", 0.0), r("", 0.0),
                        r("7", 0.30), r("7", 0.40), r("7", 0.20), r("7", 0.10)]

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


def test_reread_replaces_partially_detected_digit():
    """흐린 "9" 의 일부만 검출돼 "•" 로 읽힌 칸은 다시 읽은 결과로 바꾼다. 긴 글자 칸은 건드리지 않는다."""
    import numpy as np

    class _Ctx:
        language = "auto"

        class engines:
            @staticmethod
            def recognize(images, language):
                assert len(images) == 4  # "•" 칸 하나 × 폭 2가지 × (원본, 보정)
                return [{"text": "9", "confidence": 0.98, "lang": "en", "candidates": None}] * 4

    cells = [_cell(0, 0, [0, 0, 50, 60], ["8"]), _cell(0, 1, [50, 0, 150, 60], ["카포드"]),
             _cell(1, 0, [0, 60, 50, 120], ["9"]), _cell(1, 1, [50, 60, 150, 120], ["피.잉크.우유"])]
    cells[0]["_lines"][0]["box"] = [20, 22, 30, 32]  # 행 높이(60)의 절반도 안 되는 조각 상자
    cells[0]["_lines"][0]["text"] = "•"
    cells[1]["_lines"][0]["box"] = [60, 0, 140, 60]
    cells[2]["_lines"][0]["box"] = [15, 60, 35, 120]
    cells[3]["_lines"][0].update(box=[55, 60, 145, 120], confidence=0.7)  # 길어서 다시 읽지 않는다
    found = reread_empty_cells(_Ctx(), np.zeros((120, 150, 3), np.uint8), cells, 0.8)
    assert [l["text"] for l in cells[0]["_lines"]] == ["9"] and found[0]["replaced"] == ["•"]
    assert cells[3]["_lines"][0]["text"] == "피.잉크.우유"


def _row_lines(texts_xs, y, row):
    return [{"text": t, "confidence": 0.99, "box": [x, y, x + 30, y + 30], "row": row} for t, x in texts_xs]


def test_grid_from_lines_rebuilds_rows_and_caption():
    """구조 모델이 행을 잘못 나눠도, 글자 줄 위치만으로 행·열과 제목·표 밖 글자를 나눈다."""
    lines = (
        [{"text": "곰", "confidence": 0.53, "box": [0, 0, 60, 60], "row": 0},
         {"text": "종합 순위", "confidence": 0.98, "box": [110, 10, 225, 50], "row": 0}]  # 두 열에 걸친 제목
        + _row_lines([("순위", 0), ("브랜드", 100), ("와인", 200)], 70, 1)
        + _row_lines([("1", 0), ("프로쉬", 100), ("1", 200)], 110, 2)
        + _row_lines([("2", 0), ("퍼실", 100)], 150, 3)  # 와인 칸을 검출이 놓침
        + _row_lines([("3", 0), ("다우니", 100), ("2", 200)], 190, 4)
        + [{"text": "다만 혈흔 제거 능력은", "confidence": 0.99, "box": [0, 240, 260, 270], "row": 5}]
    )
    grid = grid_from_lines(lines)
    assert (grid["n_rows"], grid["n_cols"]) == (4, 3)
    cells = {(c["row"], c["col"]): [l["text"] for l in c["_lines"]] for c in grid["cells"]}
    assert cells[(0, 0)] == ["순위"] and cells[(1, 1)] == ["프로쉬"] and cells[(2, 2)] == [] and cells[(3, 2)] == ["2"]
    assert [l["text"] for l in grid["head"]] == ["곰", "종합 순위"]
    assert [l["text"] for l in grid["tail"]] == ["다만 혈흔 제거 능력은"]

    # 구조 모델이 순위 1~3 을 한 칸에 넣었다면 = 서로 다른 행이 한 칸에 → 구조 모델 결과를 쓰지 않는다
    rank_lines = [l for c in grid["cells"] if c["col"] == 0 for l in c["_lines"]]
    assert not structure_agrees([{"_lines": rank_lines}], grid)
    assert structure_agrees([{"_lines": c["_lines"]} for c in grid["cells"]], grid)


def test_grid_from_lines_merged_label_column():
    """병합 칸 열("월 25만원" 이 두 행에 걸침)은 새 열이 되고, 병합 글자는 그룹의 첫 행에 들어간다."""
    def L(t, x, y, row, w=30):
        return {"text": t, "confidence": 0.99, "box": [x, y, x + w, y + 30], "row": row}

    lines = [
        L("구분", 90, 0, 0), L("1년차", 200, 0, 0), L("5년차", 300, 0, 0),
        L("배당재투자O", 100, 50, 1, 60), L("1", 200, 50, 1), L("5", 300, 50, 1),
        L("월 25만원", 0, 75, 2, 60),  # 두 행 사이 높이
        L("배당재투자X", 100, 100, 3, 60), L("1", 200, 100, 3), L("5", 300, 100, 3),
        L("배당재투자O", 100, 150, 4, 60), L("2", 200, 150, 4), L("11", 300, 150, 4),
        L("월 50만원", 0, 175, 5, 60),
        L("배당재투자X", 100, 200, 6, 60), L("2", 200, 200, 6), L("10", 300, 200, 6),
    ]
    grid = grid_from_lines(lines)
    assert (grid["n_rows"], grid["n_cols"]) == (5, 4)
    rows = {}
    for c in grid["cells"]:
        rows.setdefault(c["row"], {})[c["col"]] = " ".join(l["text"] for l in c["_lines"])
    assert [rows[r][0] for r in range(5)] == ["", "월 25만원", "", "월 50만원", ""]
    assert rows[0][1] == "구분"
    assert rows[1][1] == "배당재투자O" and rows[2][1] == "배당재투자X"


def test_drop_duplicate_lines_keeps_one():
    from app.pipeline.processors.common import drop_duplicate_lines

    lines = [{"text": "구분", "confidence": 1.0, "box": [24, 258, 400, 336]},
             {"text": "구분", "confidence": 1.0, "box": [310, 264, 405, 332]},
             {"text": "1년차", "confidence": 1.0, "box": [665, 264, 785, 330]}]
    kept = drop_duplicate_lines(lines)
    assert [l["box"] for l in kept] == [[310, 264, 405, 332], [665, 264, 785, 330]]  # 같은 점수면 작은 상자


def test_chart_to_table_from_bar_labels():
    """막대 위 값·항목 이름·범례로 값 표를 만들고, 세로축 눈금과 신뢰도 미달 숫자(포인터 점)는 뺀다."""
    from app.pipeline.processors.common import sort_lines
    from app.pipeline.processors.image import chart_to_table

    def L(t, x, y, c=0.99):
        return {"text": t, "confidence": c, "box": [x, y, x + 30, y + 20]}

    lines = sort_lines([
        L("20", 5, 10), L("10", 5, 110), L("0", 5, 210),  # 세로축 눈금
        L("18", 100, 20), L("12", 140, 80), L("0", 160, 30, 0.5),  # 0 은 포인터 점(신뢰도 미달)
        L("9", 300, 120), L("4.5", 340, 160),
        L("25만원", 120, 240), L("50만원", 320, 240),
        L("1번2번", 200, 280),  # 범례가 한 줄로 붙어 읽힘
    ])
    t = chart_to_table(lines, 0.8)
    rows = {}
    for c in t["cells"]:
        rows.setdefault(c["row"], []).append(c["text"])
    assert rows == {0: ["구분", "1번", "2번"], 1: ["25만원", "18", "12"], 2: ["50만원", "9", "4.5"]}


def test_strip_sparse_edge_rows_moves_title_and_unit_out():
    from app.pipeline.processors.table import _strip_sparse_edge_rows

    cells = [_cell(0, 0, [0, 0, 50, 30], ["표 제목"]), _cell(0, 1, [50, 0, 100, 30]),
             _cell(1, 0, [0, 30, 50, 60], ["a"]), _cell(1, 1, [50, 30, 100, 60], ["b"]),
             _cell(2, 0, [0, 60, 50, 90], ["c"]), _cell(2, 1, [50, 60, 100, 90], ["d"]),
             _cell(3, 0, [0, 90, 50, 120]), _cell(3, 1, [50, 90, 100, 120], ["*단위: 만원"])]
    cells, n_rows, head, tail = _strip_sparse_edge_rows(cells, 4)
    assert n_rows == 2 and [l["text"] for l in head] == ["표 제목"] and [l["text"] for l in tail] == ["*단위: 만원"]
    assert sorted((c["row"], c["col"]) for c in cells) == [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_grid_from_lines_none_for_plain_paragraph():
    lines = [{"text": "문장 하나", "confidence": 0.99, "box": [0, 0, 300, 30], "row": 0},
             {"text": "문장 둘", "confidence": 0.99, "box": [0, 40, 280, 70], "row": 1}]
    assert grid_from_lines(lines) is None


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


# ---------------- 칸 선 표 / 워터마크 (그랑 콜레오스 오일 제원 사례) ----------------


def _ruled_image():
    """흰 바탕에 연한 회색 칸 선 표: 3열 × 4행, 첫 열 2~3행은 병합(가운데 가로 선 없음)."""
    import numpy as np

    img = np.full((220, 400, 3), 255, np.uint8)
    xs, ys = [10, 130, 260, 390], [10, 60, 110, 160, 210]
    for x in xs:
        img[10:211, x] = 180
    for i, y in enumerate(ys):
        x_from = 130 if i == 2 else 10  # 첫 열은 y=110 선이 없다 → 1~2행 병합
        img[y, x_from:391] = 180
    return img, xs, ys


def _line(text, box, gray=30, img=None):
    if img is not None:
        x1, y1, x2, y2 = box
        img[y1 + 4:y2 - 4, x1 + 4:x2 - 4:3] = gray  # 글자 획 흉내
    return {"text": text, "confidence": 0.99, "box": list(box)}


def test_ruled_grid_reads_merged_cells_from_rules():
    from app.pipeline.processors.table import ruled_grid

    img, xs, ys = _ruled_image()
    lines = [
        _line("항목", (30, 20, 100, 50), img=img), _line("값", (150, 20, 220, 50), img=img), _line("규격", (280, 20, 360, 50), img=img),
        _line("냉각수", (30, 95, 100, 125), img=img),  # 두 행에 걸친 병합 칸 가운데
        _line("GV120", (150, 70, 240, 100), img=img), _line("7.5", (280, 70, 330, 100), img=img),
        _line("GV115", (150, 120, 240, 150), img=img), _line("13.9", (280, 120, 330, 150), img=img),
        _line("연료", (30, 170, 100, 200), img=img), _line("2륜", (150, 170, 240, 200), img=img), _line("55", (280, 170, 330, 200), img=img),
    ]
    g = ruled_grid(img, lines)
    assert g is not None and (g["n_rows"], g["n_cols"]) == (4, 3)
    by_text = {l["text"]: c for c in g["cells"] for l in c["_lines"]}
    assert by_text["냉각수"]["rowspan"] == 2 and by_text["냉각수"]["row"] == 1
    assert (by_text["13.9"]["row"], by_text["13.9"]["col"]) == (2, 2)  # 값이 아래 행으로 밀리지 않는다


def test_ruled_grid_needs_rules():
    import numpy as np

    from app.pipeline.processors.table import ruled_grid

    img = np.full((200, 300, 3), 255, np.uint8)  # 선 없는 표
    assert ruled_grid(img, [_line("a", (10, 10, 50, 40), img=img)]) is None


def test_drop_stray_lines_removes_watermark_and_padding_text():
    from app.pipeline.processors.table import drop_stray_lines

    img, _, _ = _ruled_image()
    real = _line("7.5", (280, 70, 330, 100), img=img)
    mark = _line("Oiltop", (150, 70, 240, 100), gray=225, img=img)  # 연한 회색 워터마크
    above = _line("확인하십시오", (20, 0, 200, 8), img=img)  # 블록 위 여백(위 블록의 글자)
    keep, dropped = drop_stray_lines([real, mark, above], img, [12, 12, 400, 220], (0.0, 0.0))
    assert [l["text"] for l in keep] == ["7.5"]
    assert {d["text"]: d["drop_reason"] for d in dropped} == {"Oiltop": "faint_watermark", "확인하십시오": "outside_block"}
