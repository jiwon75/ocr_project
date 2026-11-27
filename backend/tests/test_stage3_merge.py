"""
[3단계 테스트] order 순서로 합치기, txt/md 형식, 페이지 구분선, 디버그 모드, DB 저장.
"""
import pytest

from app import db
from app.pipeline.merge import load_results, merge_document, table_to_markdown, table_to_tsv
from app.pipeline.storage import read_json
from tests import samples
from tests.fakes import FakeEngines


def _merged(st, fmt="txt", **kw):
    structure = read_json(st.structure_json)
    return merge_document(structure, load_results(st, structure), fmt, **kw)


def test_txt_output_in_order(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    txt = _merged(st, "txt", page_separator=True)
    expected_order = ["테스트 문서 제목", "첫 번째 문단입니다.", "[추출 실패: p1-b003]", "[추출 실패: p1-b004]", "항목\t값", "[이미지: p1-b006]"]
    positions = [txt.index(s) for s in expected_order]
    assert positions == sorted(positions)
    assert "사과\t[추출 실패: p1-b005 r2c2]" in txt  # 표는 탭 구분, 실패 셀은 블록+셀 번호


def test_md_output_title_table_image(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    md = _merged(st, "md", page_separator=True)
    assert "# 테스트 문서 제목" in md
    assert "| 항목 | 값 |\n| --- | --- |\n| 사과 | [추출 실패: p1-b005 r2c2] |" in md
    assert "![[이미지: p1-b006]](blocks/p1-b006.png)" in md


def test_page_separator_and_debug(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_digital_pdf(tmp_path / "d.pdf", pages=2), "pdf", FakeEngines())
    with_sep = _merged(st, "txt", page_separator=True)
    assert "--- page 2 ---" in with_sep and "--- page 1 ---" not in with_sep
    assert "--- page" not in _merged(st, "txt", page_separator=False)
    debug = _merged(st, "md", page_separator=False, debug=True)
    assert "<!-- p1-b001 -->" in debug and "<!-- p2-b001 -->" in debug


def test_merge_follows_order_field_not_file_order():
    """[규칙 1] 합치는 순서는 order 로만 정한다 (blocks 목록 순서가 섞여 있어도)."""
    structure = {"blocks": [
        {"block_id": "p1-b002", "order": 2, "page": 1, "type": "text"},
        {"block_id": "p1-b001", "order": 1, "page": 1, "type": "text"},
    ]}
    results = {"p1-b001": {"status": "done", "text": "첫째"}, "p1-b002": {"status": "done", "text": "둘째"}}
    assert merge_document(structure, results, "txt") == "첫째\n\n둘째\n"


def test_merged_table_with_spans_stays_markdown():
    table = {"n_rows": 2, "n_cols": 2, "cells": [
        {"row": 0, "col": 0, "rowspan": 1, "colspan": 2, "text": "병합", "status": "done"},
        {"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "text": "a|b", "status": "done"},
        {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "c", "status": "done"},
    ]}
    md = table_to_markdown("p1-b001", table)
    assert md == "| 병합 |  |\n| --- | --- |\n| a\\|b | c |"  # HTML 없이, 병합 글자는 첫 칸·가려진 칸은 빈칸
    assert table_to_tsv("p1-b001", table) == "병합\t\na|b\tc"  # 가려진 칸은 빈칸으로 자리 유지


def test_table_caption_and_outside_text_rendered():
    structure = {"blocks": [{"block_id": "p1-b001", "order": 1, "page": 1, "type": "table"}]}
    table = {"n_rows": 2, "n_cols": 2, "caption": {"text": "가격 비교", "status": "done"}, "cells": [
        {"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "순위", "status": "done"},
        {"row": 0, "col": 1, "rowspan": 1, "colspan": 1, "text": "가격", "status": "done"},
        {"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "text": "1", "status": "done"},
        {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "168원", "status": "done"},
    ]}
    outside = [{"text": "자막 글자", "status": "done"}, {"text": None, "raw_text": "??", "status": "failed"}]
    results = {"p1-b001": {"status": "done", "table": table, "outside_text": outside}}
    md = merge_document(structure, results, "md")
    assert md == ("가격 비교\n\n| 순위 | 가격 |\n| --- | --- |\n| 1 | 168원 |\n\n자막 글자\n\n"
                  "[추출 실패: p1-b001 표 밖]\n")
    assert merge_document(structure, results, "txt").startswith("가격 비교\n\n순위\t가격\n1\t168원\n\n자막 글자")


def test_db_saved_after_merge(tmp_path, run_doc, settings):
    _, file_id, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    rows = db.get_extracted_texts(settings.DB_PATH, file_id)
    assert len(rows) == 1
    assert rows[0]["extracted_text"] == st.output("txt").read_text(encoding="utf-8")
    assert "[추출 실패: p1-b003]" in rows[0]["extracted_text"]  # 실패 표시 포함 그대로 저장
    assert st.output("md").exists()


@pytest.mark.models
def test_real_end_to_end_under_time_limit(tmp_path, run_doc, real_engines, settings):
    """실제 모델로 1~3단계 전체가 시간 제한 안에 끝나는지 (모델 로딩 시간 제외 — 서버에서는 시작 시 미리 불러 둔다)."""
    import time

    start = time.perf_counter()
    _, file_id, st, _ = run_doc(samples.make_document_image(tmp_path / "doc.png"), "png", real_engines)
    elapsed = time.perf_counter() - start
    assert read_json(st.status_json)["state"] == "done"
    assert elapsed < settings.PROCESS_TIMEOUT_SEC, f"{elapsed:.1f}s"
    md = st.output("md").read_text(encoding="utf-8")
    # 모델이 제목을 doc_title(#) 또는 paragraph_title(##) 로 분류할 수 있으므로 둘 다 허용한다
    first_line = md.splitlines()[0]
    assert first_line.startswith("#") and first_line.lstrip("# ") == "문서 처리 테스트 보고서", md[:200]
    assert "| 사과 | 10 | 1,000 |" in md
