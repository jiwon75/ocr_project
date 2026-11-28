"""
[이 파일이 하는 일]
문서 하나를 전처리 → 1단계 → 2단계 → 3단계 → DB 저장 순서로 실행하고,
단계마다 로그와 진행 상태(status.json)를 남긴다. 실패하면 어느 단계에서 왜 실패했는지 기록한다.
블록 retry(다시 처리)와 사용자 수정 반영도 여기 있다.

[왜 이렇게 나눴는지]
- 각 단계 파일(preprocess/layout/process/merge)은 "자기 일"만 하고, 순서·로그·실패 처리는 이 파일이 맡는다.
  그래서 각 단계 코드를 읽을 때 로그나 상태 코드에 방해받지 않는다.
- 이 파일의 run_document / run_block_retry 는 "작업 하나"를 받아 끝까지 처리하는 순수한 함수다.
  지금은 worker.py 의 프로세스가 부르지만, 나중에 Celery/RQ 의 작업(task) 함수 안에서 그대로 부르면 된다.
"""
from pathlib import Path

from app import db
from app.config import Settings
from app.logger import log_ctx, logger, stage_log
from app.pipeline.errors import PipelineError, user_message
from app.pipeline.layout import run_layout
from app.pipeline.merge import merge_outputs, save_to_db
from app.pipeline.preprocess import preprocess
from app.pipeline.process import process_block, run_processing
from app.pipeline.processors.common import ProcessContext, now
from app.pipeline.status import mark_failed, read_status, update_status
from app.pipeline.storage import DocStorage, read_json, write_json
from app.pipeline.structure import find_block, load_structure, set_block_status


def _context(settings, engines, storage, structure, file_type) -> ProcessContext:
    pdf_path = storage.original("pdf") if file_type == "pdf" else None
    return ProcessContext(settings=settings, engines=engines, storage=storage, structure=structure, pdf_path=pdf_path)


def run_document(job: dict, engines, settings: Settings) -> None:
    """
    문서 하나 전체 처리. (워커 프로세스 안에서 실행된다)
    job = {"doc_id", "file_id", "file_name", "file_type", "file_path", "language"}
    실패해도 예외를 밖으로 던지지 않고 status.json 에 실패를 기록한다. 그래야 워커가 다음 파일을 계속 처리할 수 있다.
    """
    doc_id, name = job["doc_id"], job["file_name"]
    storage = DocStorage(settings.STORAGE_DIR, doc_id)
    stage = "preprocess"
    try:
        update_status(storage, state="analyzing", stage="preprocess", progress={"done": 0, "total": 0}, error=None)
        with stage_log("preprocess", doc_id, name):
            prepared = preprocess(Path(job["file_path"]), job["file_type"], storage, settings)

        stage = "layout"
        update_status(storage, stage="layout")
        with stage_log("layout", doc_id, name) as _:
            structure = run_layout(prepared, storage, engines, {"doc_id": doc_id, "file_name": name, "language": job["language"]},
                                   settings.LAYOUT_SWEEP_UNCOVERED_TEXT, settings.LAYOUT_SWEEP_MIN_HEIGHT_RATIO)
        total = len(structure["blocks"])
        logger.info(f"블록 {total}개 검출", extra=log_ctx(doc_id, name, "layout"))

        stage = "process"
        update_status(storage, state="processing", stage="process", progress={"done": 0, "total": total})
        ctx = _context(settings, engines, storage, structure, job["file_type"])
        with stage_log("process", doc_id, name):
            results = run_processing(ctx, lambda d, t: update_status(storage, progress={"done": d, "total": t}))
        failed = sum(1 for r in results if r["status"] == "failed")
        logger.info(f"블록 처리 완료: 성공 {total - failed} / 실패 {failed}", extra=log_ctx(doc_id, name, "process"))

        stage = "merge"
        update_status(storage, state="merging", stage="merge")
        with stage_log("merge", doc_id, name):
            text = merge_outputs(storage, settings)

        stage = "db"
        with stage_log("db", doc_id, name):
            save_to_db(settings, job["file_id"], name, text)

        update_status(storage, state="done", stage="done", failed_blocks=failed)
    except PipelineError as e:
        # 예상한 실패(손상된 파일, 페이지 수 초과 등). 단계 로그(stage_log)에 traceback 이 이미 남았다
        _fail_document(settings, storage, job, e.stage, e.reason, e.message)
    except Exception as e:
        # 예상하지 못한 오류. 사용자에게는 일반 문장, 로그에는 traceback 을 남긴다
        logger.error(f"예상하지 못한 오류: {type(e).__name__}: {e}", exc_info=True, extra=log_ctx(doc_id, name, stage))
        _fail_document(settings, storage, job, stage, f"internal_error ({type(e).__name__})", user_message("internal_error"))


def _fail_document(settings, storage: DocStorage, job: dict, stage: str, reason: str, message: str) -> None:
    """
    파일 전체 실패 처리.
    - status.json 에 실패 단계·사유 기록 → 프론트에 보여 준다
    - extracted_texts 에 이 파일 행이 혹시 있으면 지운다 (실패한 파일은 DB 결과 테이블에 없어야 하므로)
    """
    mark_failed(storage, stage, reason, message)
    db.delete_extracted_texts(settings.DB_PATH, job["file_id"])
    logger.error(f"파일 처리 실패: {reason} — {message}", extra=log_ctx(job["doc_id"], job["file_name"], stage))


def fail_document_timeout(settings: Settings, job: dict) -> None:
    """
    시간 초과로 워커를 강제 종료한 뒤 메인 프로세스가 부른다.
    실패 단계는 status.json 에 마지막으로 기록된 단계(워커가 어디까지 했는지)를 쓴다.
    """
    storage = DocStorage(settings.STORAGE_DIR, job["doc_id"])
    stage = (read_status(storage) or {}).get("stage") or "preprocess"
    reason = f"timeout ({settings.PROCESS_TIMEOUT_SEC}s)"
    _fail_document(settings, storage, job, stage, reason, user_message("timeout", timeout=settings.PROCESS_TIMEOUT_SEC))


def fail_document_crash(settings: Settings, job: dict, reason_code: str = "worker_crashed") -> None:
    """워커가 비정상 종료(메모리 부족 등)했거나 모델을 못 불러왔을 때 메인 프로세스가 부른다."""
    storage = DocStorage(settings.STORAGE_DIR, job["doc_id"])
    stage = (read_status(storage) or {}).get("stage") or "preprocess"
    _fail_document(settings, storage, job, stage, reason_code, user_message(reason_code))


# ---------------------------------------------------------------------
# 블록 retry / 사용자 수정
# ---------------------------------------------------------------------


def run_block_retry(job: dict, engines, settings: Settings) -> None:
    """
    블록 하나만 다시 처리하고, 3단계(합치기 + DB 갱신)를 다시 한다. (워커 프로세스 안에서 실행)
    job = {"doc_id", "file_id", "file_name", "file_type", "block_id"}
    [규칙 1] structure 의 블록을 그대로 넘기므로 block_id 와 order 는 바뀌지 않는다.
    """
    doc_id, name, block_id = job["doc_id"], job["file_name"], job["block_id"]
    storage = DocStorage(settings.STORAGE_DIR, doc_id)
    structure = load_structure(storage)
    block = find_block(structure, block_id)
    ctx = _context(settings, engines, storage, structure, job["file_type"])
    with stage_log("retry", doc_id, name, detail=block_id):
        result = process_block(ctx, block)
    remerge(settings, storage, job)
    logger.info(f"retry 결과 {block_id}: {result['status']}", extra=log_ctx(doc_id, name, "retry"))


def fail_retry(settings: Settings, job: dict, reason: str, detail: str) -> None:
    """
    retry 가 시간 초과·워커 종료·워커 없음으로 끝나지 못했을 때 (메인 프로세스).
    그 블록만 실패로 표시하고 다시 합친다. 문서 전체는 실패로 만들지 않는다.
    (이걸 안 하면 블록이 영원히 "처리 중(pending)"으로 남아 화면이 계속 기다리게 된다)
    """
    storage = DocStorage(settings.STORAGE_DIR, job["doc_id"])
    path = storage.block_result(job["block_id"])
    result = read_json(path) or {}
    result.update(status="failed", failure_reason=reason, failure_detail=detail, text=None, processed_at=now())
    write_json(path, result)
    set_block_status(storage, {job["block_id"]: "failed"})
    remerge(settings, storage, job)
    logger.error(f"retry 실패 {job['block_id']}: {detail}", extra=log_ctx(job["doc_id"], job["file_name"], "retry"))


def fail_retry_timeout(settings: Settings, job: dict) -> None:
    fail_retry(settings, job, "timeout", f"retry timeout ({settings.PROCESS_TIMEOUT_SEC}s)")


def apply_edit(settings: Settings, job: dict, edited_text: str | None) -> dict:
    """
    사용자가 화면에서 고친 내용을 저장한다. (메인 프로세스, 모델 불필요)
    [규칙 2] edited_text 칸에만 저장하고 원본 raw_text/text 는 그대로 둔다. None 을 주면 수정 취소.
    [규칙 1] 번호는 건드리지 않는다.
    """
    storage = DocStorage(settings.STORAGE_DIR, job["doc_id"])
    path = storage.block_result(job["block_id"])
    result = read_json(path)
    result["edited_text"] = edited_text
    result["edited_at"] = now() if edited_text is not None else None
    if edited_text is None:
        # "원래대로" 는 표 칸 단위 수정(LLM 교정 제안 수락)도 함께 되돌린다
        for cell in (result.get("table") or {}).get("cells", []):
            cell.pop("edited_text", None)
    write_json(path, result)
    with stage_log("edit", job["doc_id"], job["file_name"], detail=job["block_id"]):
        remerge(settings, storage, job)
    return result


def apply_corrections(settings: Settings, job: dict, sids: list[int]) -> dict:
    """
    사용자가 고른 LLM 교정 제안을 적용하고 다시 합친다. (메인 프로세스, 모델 불필요)
    [규칙 2] 제안은 edited_text(블록 또는 표 칸)에만 저장된다. [규칙 1] 번호는 건드리지 않는다.
    """
    from app.assist.jobs import apply_corrections as apply_to_storage  # assist 는 선택 기능이라 쓸 때만 불러온다

    storage = DocStorage(settings.STORAGE_DIR, job["doc_id"])
    out = apply_to_storage(storage, sids)
    if out["applied"]:
        with stage_log("edit", job["doc_id"], job["file_name"], detail=f"LLM 교정 {len(out['applied'])}건"):
            remerge(settings, storage, job)
    return out


def remerge(settings: Settings, storage: DocStorage, job: dict) -> None:
    """3단계를 다시 실행하고, 같은 file_id 의 DB 행을 갱신한다 (retry·수정 후)."""
    with stage_log("merge", job["doc_id"], job["file_name"]):
        text = merge_outputs(storage, settings)
    with stage_log("db", job["doc_id"], job["file_name"]):
        save_to_db(settings, job["file_id"], job["file_name"], text)
