"""
[이 파일이 하는 일]
요약·교정 제안을 백그라운드 스레드에서 실행하고, 결과를 storage/{doc_id}/assist/*.json 에 저장한다.
사용자가 고른 교정 제안을 블록 결과에 적용하고 결과를 다시 합친다.

[왜 이렇게 나눴는지]
- LLM 답변은 수십 초~몇 분 걸린다. API 요청 안에서 기다리면 브라우저가 멈춘 것처럼 보이므로,
  "시작" 요청은 바로 돌려주고 화면이 상태(state)를 몇 초마다 확인하게 한다 (OCR 처리와 같은 방식).
- OCR 워커 프로세스가 아니라 API 서버 안의 스레드에서 돌린다. 실제 계산은 Ollama 프로그램이 하고,
  여기서는 HTTP 로 기다리기만 하므로 무거운 모델을 따로 불러올 필요가 없다. OCR 시간 제한과도 섞이지 않는다.
- 서버가 꺼지면 실행 중이던 작업은 사라진다. 그래서 "실행 중" 기록이 있어도 실제 스레드가 없으면 중단됨으로 본다.

assist/{kind}.json 모양
  {"state": "running"|"done"|"failed", "model", "started_at", "finished_at", "error", ...결과}
  summary     → "summary", "chunks"
  corrections → "suggestions", "rejected", "chunks"
"""
import threading

from app.assist import core, ollama
from app.logger import log_ctx, logger
from app.pipeline.merge import load_results
from app.pipeline.processors.common import now
from app.pipeline.storage import DocStorage, read_json, write_json
from app.pipeline.structure import load_structure

KINDS = ("summary", "corrections")

_running: set[tuple[str, str]] = set()  # 지금 실제로 돌고 있는 (doc_id, kind)
_running_lock = threading.Lock()


def read_job(storage: DocStorage, kind: str) -> dict | None:
    """저장된 결과. 실행 중 기록인데 실제로는 돌고 있지 않으면(서버 재시작 등) 실패로 바꿔 보여 준다."""
    data = read_json(storage.assist_json(kind))
    if data and data.get("state") == "running" and not is_running(storage.doc_id, kind):
        data = {**data, "state": "failed", "error": "작업이 중단되었습니다 (서버가 다시 시작됨). 다시 실행해 주세요."}
    return data


def is_running(doc_id: str, kind: str) -> bool:
    with _running_lock:
        return (doc_id, kind) in _running


def start(settings, storage: DocStorage, file_name: str, kind: str) -> bool:
    """작업을 시작한다. 같은 문서의 같은 작업이 이미 돌고 있으면 False."""
    key = (storage.doc_id, kind)
    with _running_lock:
        if key in _running:
            return False
        _running.add(key)
    write_json(storage.assist_json(kind), {"state": "running", "model": settings.OLLAMA_MODEL, "started_at": now(),
                                           "finished_at": None, "error": None})
    thread = threading.Thread(target=_run, args=(settings, storage, file_name, kind), daemon=True, name=f"assist-{kind}")
    thread.start()
    return True


def _run(settings, storage: DocStorage, file_name: str, kind: str) -> None:
    ctx = log_ctx(storage.doc_id, file_name, f"assist_{kind}")
    path = storage.assist_json(kind)
    base = read_json(path) or {}
    try:
        structure = load_structure(storage)
        results = load_results(storage, structure)

        def chat(system, user, json_mode=False):
            return ollama.chat(settings, system, user, json_mode)

        if kind == "summary":
            out = core.summarize(chat, core.document_text(structure, results), settings.LLM_CHUNK_CHARS)
        else:
            units = core.collect_units(structure, results)
            out = core.suggest_corrections(chat, units, settings.LLM_CHUNK_CHARS, settings.LLM_CORRECTION_MIN_SIMILARITY)
            out["units"] = len(units)
        write_json(path, {**base, **out, "state": "done", "finished_at": now()})
        logger.info(f"LLM {kind} 완료 ({settings.OLLAMA_MODEL}, 청크 {out.get('chunks')}개)", extra=ctx)
    except ollama.LLMError as e:
        write_json(path, {**base, "state": "failed", "finished_at": now(), "error": str(e)})
        logger.warning(f"LLM {kind} 실패: {e}", extra=ctx)
    except Exception as e:  # 예상하지 못한 오류도 "실행 중"으로 남지 않게 기록한다
        write_json(path, {**base, "state": "failed", "finished_at": now(), "error": "LLM 도우미 처리 중 오류가 났습니다."})
        logger.error(f"LLM {kind} 오류: {type(e).__name__}: {e}", exc_info=True, extra=ctx)
    finally:
        with _running_lock:
            _running.discard((storage.doc_id, kind))


def apply_corrections(storage: DocStorage, sids: list[int]) -> dict:
    """
    사용자가 고른 교정 제안(sid 목록)을 블록 결과에 적용한다. 다시 합치기는 부르는 쪽(runner.remerge)이 한다.
    돌려주는 값: {"applied": [sid...], "skipped": [sid...], "blocks": [바뀐 block_id...]}
    """
    path = storage.assist_json("corrections")
    data = read_json(path) or {}
    wanted = set(sids)
    applied, skipped, changed, touched = [], [], {}, set()
    for s in data.get("suggestions", []):
        if s["sid"] not in wanted or s.get("applied"):
            continue
        block_id = s["block_id"]
        if block_id not in changed:
            changed[block_id] = read_json(storage.block_result(block_id))
        result = changed[block_id]
        if result is not None and core.apply_to_result(result, s["id"], s["wrong"], s["right"]):
            s["applied"] = True
            applied.append(s["sid"])
            touched.add(block_id)
        else:
            skipped.append(s["sid"])
    for block_id in touched:
        changed[block_id]["edited_at"] = now()
        write_json(storage.block_result(block_id), changed[block_id])
    write_json(path, data)
    return {"applied": applied, "skipped": skipped, "blocks": sorted(touched)}
