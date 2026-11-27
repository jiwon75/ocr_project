"""
[운영 안정성 테스트] 모델 로딩 / 시간 제한 / 실패 격리 / 로그
실제 워커 프로세스를 띄워서 확인한다 (엔진만 가짜). 시간 제한은 테스트 시간을 줄이려고 설정으로 짧게 바꾼다.
→ "30초가 넘는 파일"은 "PROCESS_TIMEOUT_SEC 보다 오래 걸리는 파일"로 확인한다. 30 이라는 값 자체도 설정이므로 같은 코드가 동작한다.
"""
import time

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from tests import samples


def _client(settings, factory="tests.fakes:make_fake_engines"):
    return TestClient(create_app(settings, factory))


def _wait(client, doc_id, until=("done", "failed"), timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = client.get(f"/api/documents/{doc_id}/status").json()
        if s["status"] in until:
            return s
        time.sleep(0.2)
    raise AssertionError(f"시간 안에 끝나지 않음: {s}")


def _wait_ready(client, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = client.get("/api/health")
        if r.status_code == 200:
            return r.json()
        time.sleep(0.2)
    raise AssertionError("워커가 준비되지 않음")


def _upload(client, path, mime="image/png", language="auto"):
    with open(path, "rb") as f:
        r = client.post("/api/documents", files={"file": (path.name, f, mime)}, data={"language": language})
    assert r.status_code == 202, r.text
    return r.json()


def test_health_not_ready_until_models_loaded(settings):
    with _client(settings, "tests.fakes:make_slow_loading_engines") as client:
        first = client.get("/api/health")
        assert first.status_code == 503 and first.json()["status"] == "not_ready"
        assert _wait_ready(client)["status"] == "ready"


def test_models_loaded_once_per_worker(settings, tmp_path):
    """파일 두 개를 처리해도 모델 로딩 로그는 한 번만 나와야 한다."""
    with _client(settings) as client:
        _wait_ready(client)
        for i in range(2):
            doc = _upload(client, samples.make_fake_page(tmp_path / f"p{i}.png"))
            assert _wait(client, doc["doc_id"])["status"] == "done"
    log = (settings.LOG_DIR / "app.log").read_text(encoding="utf-8")
    assert log.count("모델 로딩 완료") == 1


def test_timeout_kills_job_and_next_file_still_works(settings, tmp_path):
    settings.PROCESS_TIMEOUT_SEC = 3
    with _client(settings) as client:
        _wait_ready(client)
        slow = _upload(client, samples.make_fake_page(tmp_path / "slow.png", marker=samples_red()))
        start = time.monotonic()
        status = _wait(client, slow["doc_id"], timeout=30)
        elapsed = time.monotonic() - start
        assert status["status"] == "failed"
        assert status["error"]["reason"] == "timeout (3s)"
        assert status["error"]["stage"] == "layout"  # 어느 단계에서 멈췄는지
        assert "3초" in status["error"]["message"]  # 사용자용 문장
        assert elapsed < 10  # 가짜 엔진은 120초를 자려고 했지만 실제로 멈췄다

        # 실패 격리: 서버는 살아 있고 다음 파일은 정상 처리된다 (새 워커가 모델을 다시 불러옴)
        ok = _upload(client, samples.make_fake_page(tmp_path / "ok.png"))
        assert _wait(client, ok["doc_id"])["status"] == "done"
        # 실패한 파일은 extracted_texts 에 없다
        row = db.get_uploaded_file(settings.DB_PATH, slow["doc_id"])
        assert db.get_extracted_texts(settings.DB_PATH, row["id"]) == []
    log = (settings.LOG_DIR / "app.log").read_text(encoding="utf-8")
    assert "시간 제한 3초 초과 → 워커 강제 종료" in log


def test_worker_crash_is_isolated(settings, tmp_path):
    with _client(settings) as client:
        _wait_ready(client)
        crash = _upload(client, samples.make_fake_page(tmp_path / "crash.png", marker=(0, 0, 255)))
        status = _wait(client, crash["doc_id"], timeout=30)
        assert status["status"] == "failed" and status["error"]["reason"] == "worker_crashed"
        ok = _upload(client, samples.make_fake_page(tmp_path / "ok.png"))
        assert _wait(client, ok["doc_id"])["status"] == "done"


def test_corrupted_file_fails_but_server_continues(settings, tmp_path):
    with _client(settings) as client:
        _wait_ready(client)
        bad = _upload(client, samples.make_corrupted_pdf(tmp_path / "bad.pdf"), "application/pdf")
        status = _wait(client, bad["doc_id"])
        assert status["status"] == "failed"
        assert status["error"] == {"stage": "preprocess", "reason": "corrupted_file", "message": "파일이 손상되었거나 열 수 없는 형식입니다."}
        ok = _upload(client, samples.make_fake_page(tmp_path / "ok.png"))
        assert _wait(client, ok["doc_id"])["status"] == "done"


def test_model_load_failure_reports_not_ready(settings):
    settings.WORKER_START_TIMEOUT_SEC = 20
    with _client(settings, "tests.fakes:make_broken_engines") as client:
        time.sleep(3)
        r = client.get("/api/health")
        assert r.status_code == 503
        assert "load_failed" in r.json()["workers"]["states"] or "starting" in r.json()["workers"]["states"]
    log = (settings.LOG_DIR / "app.log").read_text(encoding="utf-8")
    assert "모델 로딩 실패" in log and "Traceback" in log


def test_log_format_has_all_fields(settings, tmp_path):
    with _client(settings) as client:
        _wait_ready(client)
        doc = _upload(client, samples.make_corrupted_pdf(tmp_path / "bad.pdf"), "application/pdf")
        _wait(client, doc["doc_id"])
    log = (settings.LOG_DIR / "app.log").read_text(encoding="utf-8")
    short = doc["doc_id"][:8]
    # 시각 | 레벨 | doc | file | stage | 걸린 시간 | 메시지
    import re

    upload_line = re.search(rf"^\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d \| INFO \| doc={short} \| file=bad\.pdf \| stage=upload \| \d+ms \| 성공", log, re.M)
    assert upload_line, log
    error_line = re.search(rf"\| ERROR \| doc={short} \| file=bad\.pdf \| stage=preprocess \| \d+ms \| 실패 PipelineError", log)
    assert error_line, log
    assert "Traceback (most recent call last)" in log  # 오류 traceback 까지 남는다


def samples_red():
    return (255, 0, 0)
