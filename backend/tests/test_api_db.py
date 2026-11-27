"""
[API + DB 테스트] 업로드 검사, 각 조회 API, retry/수정, DB 두 테이블 저장을 확인한다.
"""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from tests import samples
from tests.test_runtime import _upload, _wait, _wait_ready


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, "tests.fakes:make_fake_engines")) as c:
        _wait_ready(c)
        yield c


def _rows(settings, table):
    with sqlite3.connect(settings.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]  # 테스트 전용: 고정된 테이블 이름


# ---------------- 업로드 검사 ----------------


def test_upload_adds_uploaded_files_row(client, settings, tmp_path):
    doc = _upload(client, samples.make_fake_page(tmp_path / "p.png"))
    rows = _rows(settings, "uploaded_files")
    assert len(rows) == 1
    assert rows[0]["doc_id"] == doc["doc_id"] and rows[0]["file_name"] == "p.png" and rows[0]["file_type"] == "png"
    assert rows[0]["created_at"] and rows[0]["file_path"].endswith("original.png")


def test_upload_too_big_413_message_from_settings(client, settings, tmp_path):
    settings.MAX_FILE_SIZE_MB = 1
    path = samples.make_big_file(tmp_path / "big.png", 1)
    with open(path, "rb") as f:
        r = client.post("/api/documents", files={"file": ("big.png", f, "image/png")})
    assert r.status_code == 413
    assert r.json()["detail"] == "파일 크기는 1MB 이하만 가능합니다."
    assert _rows(settings, "uploaded_files") == []  # 검사를 통과하지 못하면 DB 에 남기지 않는다


def test_upload_5mb_default_limit(client, settings, tmp_path):
    assert settings.MAX_FILE_SIZE_MB == 5
    path = samples.make_big_file(tmp_path / "big.png", 5)
    with open(path, "rb") as f:
        r = client.post("/api/documents", files={"file": ("big.png", f, "image/png")})
    assert r.status_code == 413 and "5MB" in r.json()["detail"]


def test_upload_two_files_rejected(client, tmp_path):
    p = samples.make_fake_page(tmp_path / "p.png")
    with open(p, "rb") as f1, open(p, "rb") as f2:
        r = client.post("/api/documents", files=[("file", ("a.png", f1, "image/png")), ("file", ("b.png", f2, "image/png"))])
    assert r.status_code == 400 and "1개" in r.json()["detail"]


def test_upload_bad_extension(client, tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello")
    with open(p, "rb") as f:
        r = client.post("/api/documents", files={"file": ("a.txt", f, "text/plain")})
    assert r.status_code == 400


def test_upload_mime_mismatch(client, tmp_path):
    """확장자는 png 인데 내용은 PDF → 거절."""
    p = samples.make_digital_pdf(tmp_path / "d.pdf")
    with open(p, "rb") as f:
        r = client.post("/api/documents", files={"file": ("fake.png", f, "image/png")})
    assert r.status_code == 400 and "형식이 아닙니다" in r.json()["detail"]
    with open(p, "rb") as f:
        r = client.post("/api/documents", files={"file": ("d.pdf", f, "image/png")})
    assert r.status_code == 400 and "MIME" in r.json()["detail"]


def test_upload_too_many_pages(client, settings, tmp_path):
    settings.MAX_PAGES = 2
    p = samples.make_digital_pdf(tmp_path / "many.pdf", pages=3)
    with open(p, "rb") as f:
        r = client.post("/api/documents", files={"file": ("many.pdf", f, "application/pdf")})
    assert r.status_code == 400 and "2페이지" in r.json()["detail"]


def test_upload_bad_language(client, tmp_path):
    p = samples.make_fake_page(tmp_path / "p.png")
    with open(p, "rb") as f:
        r = client.post("/api/documents", files={"file": ("p.png", f, "image/png")}, data={"language": "jp"})
    assert r.status_code == 400


# ---------------- 처리 결과 + DB ----------------


def test_full_flow_and_extracted_texts_row(client, settings, tmp_path):
    doc = _upload(client, samples.make_fake_page(tmp_path / "p.png"), language="ko")
    status = _wait(client, doc["doc_id"])
    assert status["status"] == "done" and status["progress"]["percent"] == 100

    structure = client.get(f"/api/documents/{doc['doc_id']}/structure").json()
    assert structure["language"] == "ko" and len(structure["blocks"]) == 6

    block = client.get(f"/api/documents/{doc['doc_id']}/blocks/p1-b002").json()
    assert block["result"]["text"] == "첫 번째 문단입니다."
    assert client.get(block["image_url"]).status_code == 200

    txt = client.get(f"/api/documents/{doc['doc_id']}/result", params={"format": "txt"}).text
    md = client.get(f"/api/documents/{doc['doc_id']}/result", params={"format": "md", "debug": True}).text
    assert "[추출 실패: p1-b003]" in txt and "<!-- p1-b001 -->" in md

    rows = _rows(settings, "extracted_texts")
    file_row = db.get_uploaded_file(settings.DB_PATH, doc["doc_id"])
    assert len(rows) == 1 and rows[0]["file_id"] == file_row["id"]  # file_id 로 연결
    assert rows[0]["extracted_text"] == txt


def test_same_file_name_twice_gets_separate_rows(client, settings, tmp_path):
    p = samples.make_fake_page(tmp_path / "same.png")
    a, b = _upload(client, p), _upload(client, p)
    _wait(client, a["doc_id"]), _wait(client, b["doc_id"])
    rows = _rows(settings, "extracted_texts")
    assert len(rows) == 2 and rows[0]["file_id"] != rows[1]["file_id"]


def test_failed_file_not_in_extracted_texts(client, settings, tmp_path):
    doc = _upload(client, samples.make_corrupted_pdf(tmp_path / "bad.pdf"), "application/pdf")
    assert _wait(client, doc["doc_id"])["status"] == "failed"
    assert len(_rows(settings, "uploaded_files")) == 1  # 업로드 기록은 남는다
    assert _rows(settings, "extracted_texts") == []  # 결과는 저장하지 않는다
    assert client.get(f"/api/documents/{doc['doc_id']}/result").status_code == 409


def test_foreign_key_enforced(settings):
    with pytest.raises(sqlite3.IntegrityError):
        db.save_extracted_text(settings.DB_PATH, 9999, "x", "y")  # 없는 file_id


def test_wal_mode_enabled(client, settings):
    with sqlite3.connect(settings.DB_PATH) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_retry_updates_same_db_row(client, settings, tmp_path):
    doc = _upload(client, samples.make_fake_page(tmp_path / "p.png"))
    _wait(client, doc["doc_id"])
    before_rows = _rows(settings, "extracted_texts")
    before_ids = [b["block_id"] for b in client.get(f"/api/documents/{doc['doc_id']}/structure").json()["blocks"]]

    r = client.post(f"/api/documents/{doc['doc_id']}/blocks/p1-b003/retry")
    assert r.status_code == 202 and r.json()["order"] == 3
    for _ in range(100):
        block = client.get(f"/api/documents/{doc['doc_id']}/blocks/p1-b003").json()
        if block["status"] != "pending":
            break
        time.sleep(0.1)
    assert block["result"]["attempts"] == 2

    after_rows = _rows(settings, "extracted_texts")
    assert len(after_rows) == 1 and after_rows[0]["id"] == before_rows[0]["id"]  # 새 행이 아니라 같은 행 갱신
    after_ids = [b["block_id"] for b in client.get(f"/api/documents/{doc['doc_id']}/structure").json()["blocks"]]
    assert after_ids == before_ids


def test_edit_block_updates_db_and_keeps_original(client, settings, tmp_path):
    doc = _upload(client, samples.make_fake_page(tmp_path / "p.png"))
    _wait(client, doc["doc_id"])
    r = client.patch(f"/api/documents/{doc['doc_id']}/blocks/p1-b003", json={"edited_text": "고친 문장"})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["edited_text"] == "고친 문장" and res["raw_text"] == "흐릿한 글자"
    assert "고친 문장" in _rows(settings, "extracted_texts")[0]["extracted_text"]


def test_invalid_ids_404(client):
    assert client.get("/api/documents/not-a-uuid/status").status_code == 404
    assert client.get("/api/documents/00000000-0000-0000-0000-000000000000/status").status_code == 404


def test_hwpx_upload_flow(client, settings, tmp_path):
    doc = _upload(client, samples.make_hwpx(tmp_path / "d.hwpx"), "application/octet-stream")
    assert _wait(client, doc["doc_id"])["status"] == "done"
    data = client.get(f"/api/documents/{doc['doc_id']}/blocks").json()
    assert all(b["bbox"] is None for b in data["blocks"])
    md = client.get(f"/api/documents/{doc['doc_id']}/result", params={"format": "md"}).text
    assert '<td colspan="2">구분</td>' in md and '<td rowspan="2">1분기</td>' in md
