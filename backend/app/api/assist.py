"""
[이 파일이 하는 일]
LLM 도우미(Ollama) API. 선택 기능이라 Ollama 가 꺼져 있으면 409 와 안내 문장을 돌려줄 뿐, 다른 API 에는 영향이 없다.

  GET  /api/assist/status                                Ollama 연결·모델 설치 여부
  GET  /api/documents/{doc_id}/assist                    요약·교정 제안 결과와 진행 상태
  POST /api/documents/{doc_id}/assist/{kind}             요약(summary) / 교정 제안(corrections) 시작 → 202
  POST /api/documents/{doc_id}/assist/corrections/apply  고른 교정 제안 적용 {"sids": [1, 3]}
"""
from fastapi import APIRouter, HTTPException, Request

from app.api.documents import _ensure_editable, _file_row, _job, _storage
from app.assist import jobs, ollama
from app.models.schemas import ApplyCorrectionsRequest
from app.pipeline import runner

router = APIRouter()


@router.get("/assist/status")
def assist_status(request: Request):
    return ollama.status(request.app.state.settings)


@router.get("/documents/{doc_id}/assist")
def get_assist(request: Request, doc_id: str):
    storage = _storage(request, doc_id)
    return {kind: jobs.read_job(storage, kind) for kind in jobs.KINDS}


@router.post("/documents/{doc_id}/assist/corrections/apply")
def apply_corrections(request: Request, doc_id: str, body: ApplyCorrectionsRequest):
    storage = _storage(request, doc_id)
    _ensure_editable(request, storage, doc_id)
    if jobs.is_running(doc_id, "corrections"):
        raise HTTPException(409, "교정 제안을 아직 만드는 중입니다.")
    out = runner.apply_corrections(request.app.state.settings, _job(_file_row(request, doc_id)), body.sids)
    return {**out, "corrections": jobs.read_job(storage, "corrections")}


@router.post("/documents/{doc_id}/assist/{kind}", status_code=202)
def start_assist(request: Request, doc_id: str, kind: str):
    settings = request.app.state.settings
    if kind not in jobs.KINDS:
        raise HTTPException(404, "summary 또는 corrections 만 가능합니다.")
    storage = _storage(request, doc_id)
    _ensure_editable(request, storage, doc_id)
    info = ollama.status(settings)
    if not (info["reachable"] and info["model_available"]):
        raise HTTPException(409, info["message"])
    row = _file_row(request, doc_id)
    if not jobs.start(settings, storage, row["file_name"], kind):
        raise HTTPException(409, "이미 실행 중입니다. 잠시 기다려 주세요.")
    return jobs.read_job(storage, kind)
