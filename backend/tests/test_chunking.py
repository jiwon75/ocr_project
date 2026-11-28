"""
[청킹 테스트] 추출 텍스트를 청크로 나누는 규칙(크기·겹침·제목·표·페이지 정보)과 DB/API 저장을 확인한다.
"""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import Settings
from app.main import create_app
from app.pipeline.chunking import Unit, build_chunks, build_units, overlap_tail, split_long
from app.pipeline.storage import read_json
from tests import samples
from tests.fakes import FakeEngines
from tests.test_runtime import _upload, _wait, _wait_ready


def _block(i, btype="text", page=1, **kw):
    return {"block_id": f"p{page}-b{i:03d}", "order": i, "page": page, "type": btype, **kw}


def _done(text):
    return {"status": "done", "text": text}


# ---------------- 규칙 ----------------


def test_small_blocks_packed_into_one_chunk():
    structure = {"blocks": [_block(1), _block(2)]}
    results = {"p1-b001": _done("첫 문단."), "p1-b002": _done("둘째 문단.")}
    chunks = build_chunks(build_units(structure, results, True), 1000, 150)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "첫 문단.\n\n둘째 문단."
    assert chunks[0]["block_ids"] == ["p1-b001", "p1-b002"] and chunks[0]["overlap_chars"] == 0


def test_chunks_respect_size_and_overlap():
    sentence = "가나다라마바사 아자차카타파하. "
    blocks = [_block(i) for i in range(1, 21)]
    results = {b["block_id"]: _done((sentence * 5).strip()) for b in blocks}  # 블록마다 약 90자
    chunks = build_chunks(build_units({"blocks": blocks}, results, True), 300, 60)
    assert len(chunks) > 3
    assert all(c["char_count"] <= 300 for c in chunks)
    assert chunks[0]["overlap_chars"] == 0
    for prev, cur in zip(chunks, chunks[1:]):
        head = cur["text"][: cur["overlap_chars"]]
        assert 0 < len(head) <= 60 and prev["text"].endswith(head)  # 앞 청크 끝부분이 그대로 겹친다
    assert [c["chunk_index"] for c in chunks] == list(range(1, len(chunks) + 1))


def test_all_blocks_covered_in_order():
    """[규칙 1] 모든 블록이 order 순서대로 어느 청크엔가 들어간다 (빠지거나 순서가 바뀌지 않는다)."""
    blocks = [_block(i) for i in range(1, 31)]
    results = {b["block_id"]: _done(f"문단 {b['order']} " + "내용 " * 30) for b in blocks}
    chunks = build_chunks(build_units({"blocks": list(reversed(blocks))}, results, True), 400, 50)
    ids = [b for c in chunks for b in c["block_ids"]]
    assert ids == [b["block_id"] for b in blocks]


def test_title_attached_to_next_block():
    structure = {"blocks": [_block(1, "text"), _block(2, "title"), _block(3, "text")]}
    results = {"p1-b001": _done("앞 " * 200), "p1-b002": _done("2. 예산 계획"), "p1-b003": _done("예산은 100만원이다.")}
    chunks = build_chunks(build_units(structure, results, True), 420, 0)  # 앞 문단(399자) + 제목·본문은 420자를 넘는다
    assert len(chunks) == 2
    assert chunks[1]["text"].startswith("2. 예산 계획\n예산은 100만원이다.")  # 제목이 앞 청크 끝에 홀로 남지 않는다
    assert chunks[1]["block_ids"] == ["p1-b002", "p1-b003"]


def test_table_is_own_chunk_without_overlap():
    table = {"n_rows": 2, "n_cols": 2, "cells": [
        {"row": 0, "col": 0, "text": "항목", "status": "done"}, {"row": 0, "col": 1, "text": "값", "status": "done"},
        {"row": 1, "col": 0, "text": "사과", "status": "done"}, {"row": 1, "col": 1, "text": "100", "status": "done"},
    ]}
    structure = {"blocks": [_block(1), _block(2, "table"), _block(3)]}
    results = {"p1-b001": _done("표 앞 문단."), "p1-b002": {"status": "done", "table": table}, "p1-b003": _done("표 뒤 문단.")}
    chunks = build_chunks(build_units(structure, results, True), 1000, 150)
    assert [c["kind"] for c in chunks] == ["text", "table", "text"]
    assert chunks[1]["text"] == "항목\t값\n사과\t100" and chunks[1]["overlap_chars"] == 0
    assert chunks[2]["overlap_chars"] == 0  # 표 다음 본문은 표 글자를 겹쳐 넣지 않는다


def test_single_column_table_treated_as_text():
    """HWP 글상자(열 1개짜리 표) 안의 본문은 표가 아니라 본문 청크로 다룬다 (오버랩 적용)."""
    long = " ".join(f"답안 {i}번 문장입니다." for i in range(40))
    table = {"n_rows": 2, "n_cols": 1, "cells": [
        {"row": 0, "col": 0, "text": "1. 문항", "status": "done"}, {"row": 1, "col": 0, "text": long, "status": "done"},
    ]}
    structure = {"blocks": [_block(1, "table")]}
    chunks = build_chunks(build_units(structure, {"p1-b001": {"status": "done", "table": table}}, True), 300, 50)
    assert len(chunks) > 1 and {c["kind"] for c in chunks} == {"text"}
    assert all(c["overlap_chars"] > 0 for c in chunks[1:])


def test_long_block_split_by_sentence():
    text = " ".join(f"이것은 {i}번째 문장입니다." for i in range(60))
    parts = split_long(Unit(text, "text", [1], ["p1-b001"]), 200)
    assert len(parts) > 1 and all(len(p.text) <= 200 for p in parts)
    assert all(p.text.endswith("문장입니다.") for p in parts)  # 문장 중간에서 자르지 않는다
    assert all(p.block_ids == ["p1-b001"] for p in parts)


def test_sentence_longer_than_limit_hard_cut():
    parts = split_long(Unit("가" * 250, "text", [1], ["p1-b001"]), 100)
    assert [len(p.text) for p in parts] == [100, 100, 50]


def test_page_range_and_failed_marker_kept():
    """[규칙 2] 실패 블록도 [추출 실패: ...] 로 청크에 남는다. 그림만 있는 블록은 검색할 글자가 없어 뺀다."""
    structure = {"blocks": [_block(1, page=1), _block(2, "image", page=1), _block(1, page=2), _block(2, page=2)]}
    results = {"p1-b001": _done("1쪽 글."), "p1-b002": {"status": "done", "image_path": "p1-b002.png"},
               "p2-b001": {"status": "failed", "text": None}, "p2-b002": _done("2쪽 글.")}
    for b in structure["blocks"]:
        b["order"] = structure["blocks"].index(b) + 1
    chunks = build_chunks(build_units(structure, results, True), 1000, 0)
    assert len(chunks) == 1
    assert chunks[0]["page_start"] == 1 and chunks[0]["page_end"] == 2
    assert "[추출 실패: p2-b001]" in chunks[0]["text"] and "[이미지:" not in chunks[0]["text"]


def test_edited_text_used():
    structure = {"blocks": [_block(1)]}
    results = {"p1-b001": {**_done("틀린 글자"), "edited_text": "고친 글자"}}
    assert build_chunks(build_units(structure, results, True), 1000, 0)[0]["text"] == "고친 글자"


def test_overlap_tail_starts_at_word_boundary():
    tail = overlap_tail("하나 둘 셋 넷 다섯 여섯", 8)
    assert tail and not tail.startswith(" ") and "하나 둘 셋 넷 다섯 여섯".endswith(tail)
    assert overlap_tail("짧다", 0) == ""


def test_empty_document_has_no_chunks():
    assert build_chunks([], 1000, 150) == []


@pytest.mark.parametrize("size,overlap", [(0, 0), (100, 100), (100, -1)])
def test_invalid_chunk_settings_rejected(size, overlap):
    """[규칙 4] 잘못된 청크 설정은 서버 시작(설정 읽기) 때 바로 오류."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, CHUNK_SIZE_CHARS=size, CHUNK_OVERLAP_CHARS=overlap)


# ---------------- 저장 (파일 + DB) ----------------


def test_chunks_saved_to_file_and_db(tmp_path, run_doc, settings):
    _, file_id, st, _ = run_doc(samples.make_digital_pdf(tmp_path / "d.pdf", pages=2), "pdf", FakeEngines())
    saved = read_json(st.chunks_json)
    rows = db.get_text_chunks(settings.DB_PATH, file_id)
    assert saved["count"] == len(rows) > 0
    assert saved["chunk_size"] == settings.CHUNK_SIZE_CHARS and saved["chunk_overlap"] == settings.CHUNK_OVERLAP_CHARS
    assert [r["text"] for r in rows] == [c["text"] for c in saved["chunks"]]
    assert rows[0]["page_start"] == 1 and rows[-1]["page_end"] == 2
    assert isinstance(rows[0]["block_ids"], list)


# ---------------- API ----------------


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, "tests.fakes:make_fake_engines")) as c:
        _wait_ready(c)
        yield c


def test_chunks_api_and_update_after_edit(client, settings, tmp_path):
    doc = _upload(client, samples.make_fake_page(tmp_path / "p.png"))
    assert _wait(client, doc["doc_id"])["status"] == "done"
    data = client.get(f"/api/documents/{doc['doc_id']}/chunks").json()
    assert data["count"] == len(data["chunks"]) > 0
    assert data["chunk_size"] == settings.CHUNK_SIZE_CHARS
    assert any("첫 번째 문단입니다." in c["text"] for c in data["chunks"])

    # 블록을 고치면 청크도 다시 만들어진다
    r = client.patch(f"/api/documents/{doc['doc_id']}/blocks/p1-b002", json={"edited_text": "고친 문단입니다."})
    assert r.status_code == 200
    data = client.get(f"/api/documents/{doc['doc_id']}/chunks").json()
    texts = "\n".join(c["text"] for c in data["chunks"])
    assert "고친 문단입니다." in texts and "첫 번째 문단입니다." not in texts


def test_failed_file_has_no_chunks(client, settings, tmp_path):
    doc = _upload(client, samples.make_corrupted_pdf(tmp_path / "bad.pdf"), "application/pdf")
    assert _wait(client, doc["doc_id"])["status"] == "failed"
    assert client.get(f"/api/documents/{doc['doc_id']}/chunks").status_code == 409
    row = db.get_uploaded_file(settings.DB_PATH, doc["doc_id"])
    assert db.get_text_chunks(settings.DB_PATH, row["id"]) == []
