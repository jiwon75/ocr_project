"""
[이 파일이 하는 일]
문서 처리 진행 상태(status.json)를 읽고 쓴다. 프론트는 status API 로 이 내용을 1초마다 가져가 진행률을 그린다.

  {"state": "processing", "stage": "process", "progress": {"done": 3, "total": 10}, "error": null, ...}

state 값
  queued     : 워커가 비기를 기다리는 중
  analyzing  : 전처리 + 1단계(구조 분석)
  processing : 2단계(블록 처리) — progress.done / progress.total
  merging    : 3단계(합치기 + DB 저장)
  done       : 완료
  failed     : 실패 — error.stage / error.reason / error.message

[왜 DB 가 아니라 파일에 두는지]
상태는 1초에도 여러 번 바뀌는 임시 정보라서, 요구사항의 두 테이블(업로드 정보, 최종 텍스트)과 성격이 다르다.
문서 폴더 안의 status.json 에 두면 문서별로 독립적이고, 워커와 API 가 같은 파일만 보면 된다.
"""
from datetime import datetime

from app.pipeline.storage import DocStorage, read_json, write_json

STATES = ("queued", "analyzing", "processing", "merging", "done", "failed")


def read_status(storage: DocStorage) -> dict | None:
    return read_json(storage.status_json)


def update_status(storage: DocStorage, **fields) -> dict:
    """
    status.json 의 일부 항목만 바꾼다 (나머지는 그대로 둔다).
    예) update_status(st, state="processing", progress={"done": 1, "total": 5})
    """
    status = read_json(storage.status_json) or {"doc_id": storage.doc_id, "error": None}
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(fields["state"])
    status.update(fields)
    status["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(storage.status_json, status)
    return status


def mark_failed(storage: DocStorage, stage: str, reason: str, message: str) -> dict:
    """
    파일 전체 실패를 기록한다.
    reason 은 "timeout (30s)" 처럼 짧은 코드, message 는 사용자에게 보여 줄 문장.
    """
    return update_status(storage, state="failed", error={"stage": stage, "reason": reason, "message": message})
