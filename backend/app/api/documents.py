"""
[이 파일이 하는 일]
문서 업로드·상태·결과 조회·블록 retry/수정 API.

  POST  /api/documents                              파일 1개 업로드 (+language) → doc_id. 처리는 백그라운드에서
  GET   /api/documents/{doc_id}/status              진행 상태
  GET   /api/documents/{doc_id}/structure           블록 구조 (structure.json)
  GET   /api/documents/{doc_id}/blocks              모든 블록 + 결과 (화면 그리기용)
  GET   /api/documents/{doc_id}/blocks/{block_id}   블록 하나 결과 + crop 이미지 URL
  GET   /api/documents/{doc_id}/blocks/{block_id}/image
  POST  /api/documents/{doc_id}/blocks/{block_id}/retry
  PATCH /api/documents/{doc_id}/blocks/{block_id}   사용자 수정(edited_text)
  GET   /api/documents/{doc_id}/pages/{page}/image  원본 페이지 이미지
  GET   /api/documents/{doc_id}/result?format=txt|md&page_separator=&debug=

[왜 이렇게 나눴는지]
API 함수는 "요청이 올바른지 검사하고, pipeline 의 함수를 부르고, 응답을 만드는 것"만 한다.
실제 처리는 app/pipeline 에 있어서, 나중에 작업 큐(Celery 등)로 바꿔도 이 파일은 거의 그대로 둘 수 있다.
"""
import io
import uuid
import zipfile

import pymupdf
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from app import db
from app.logger import log_ctx, logger, stage_log
from app.models.schemas import EditRequest, StatusResponse, UploadResponse
from app.pipeline import runner
from app.pipeline.engines import LANGUAGES
from app.pipeline.merge import load_results, merge_document, render_block
from app.pipeline.status import read_status, update_status
from app.pipeline.storage import DocStorage, is_valid_block_id, is_valid_doc_id, read_json
from app.pipeline.structure import find_block, load_structure, set_block_status

router = APIRouter()

# 지원 형식. 이 목록은 "코드가 처리할 줄 아는 형식"이라 설정값(.env)이 아니라 코드에 둔다.
# (.env 에 docx 를 추가해도 처리 코드가 없으면 동작하지 않기 때문)
EXTENSIONS = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "pdf": "pdf", "hwp": "hwp", "hwpx": "hwpx"}
# 브라우저가 보내는 MIME 종류. HWP 는 브라우저가 형식을 몰라 octet-stream(그냥 바이너리)이나 빈 값으로 보내는 경우가 많아 허용한다
MIME_TYPES = {
    "png": {"image/png"},
    "jpg": {"image/jpeg", "image/jpg", "image/pjpeg"},
    "pdf": {"application/pdf"},
    "hwp": {"application/x-hwp", "application/haansofthwp", "application/vnd.hancom.hwp", "application/octet-stream", ""},
    "hwpx": {"application/hwp+zip", "application/haansofthwpx", "application/vnd.hancom.hwpx", "application/x-hwpx", "application/zip", "application/octet-stream", ""},
}


def sniff_type(data: bytes) -> str | None:
    """
    파일 앞부분의 "매직 넘버"(형식마다 정해진 시작 바이트)로 실제 형식을 알아낸다.
    확장자나 브라우저가 보낸 MIME 은 이름만 바꾸면 속일 수 있어서, 실제 내용으로 한 번 더 확인하기 위해서다.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):  # OLE 복합 문서 (HWP 5.x)
        return "hwp"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if "mimetype" in names and b"hwp" in zf.read("mimetype").lower():
                    return "hwpx"
                if any(n.startswith("Contents/section") for n in names):
                    return "hwpx"
        except zipfile.BadZipFile:
            return None
    return None


def _storage(request: Request, doc_id: str) -> DocStorage:
    if not is_valid_doc_id(doc_id):
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    storage = DocStorage(request.app.state.settings.STORAGE_DIR, doc_id)
    if not storage.root.exists():
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return storage


def _file_row(request: Request, doc_id: str) -> dict:
    row = db.get_uploaded_file(request.app.state.settings.DB_PATH, doc_id)
    if not row:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return row


def _job(row: dict, **extra) -> dict:
    return {"doc_id": row["doc_id"], "file_id": row["id"], "file_name": row["file_name"], "file_type": row["file_type"], "file_path": row["file_path"], **extra}


# ---------------------------------------------------------------------
# 업로드
# ---------------------------------------------------------------------


@router.post("", response_model=UploadResponse, status_code=202)
async def upload_document(
    request: Request,
    file: list[UploadFile] = File(..., description="업로드할 파일 1개"),
    language: str = Form("auto", description="auto | ko | en"),
):
    """
    파일 1개를 받아 검사 → 저장 → uploaded_files 에 기록 → 백그라운드 처리 시작.
    202(Accepted) 를 돌려주는 이유: 처리가 아직 끝난 게 아니라 "접수만" 됐다는 뜻이기 때문.
    """
    settings = request.app.state.settings

    # 1) 한 번에 1개만. 여러 개를 받으면 어떤 파일 결과인지 헷갈리고 시간 제한 계산도 꼬인다
    if len(file) != 1:
        raise HTTPException(400, "한 번에 파일 1개만 업로드할 수 있습니다.")
    upload = file[0]
    name = upload.filename or "upload"

    if language not in LANGUAGES:
        raise HTTPException(400, "language 는 auto, ko, en 중 하나여야 합니다.")

    # 2) 확장자 검사
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    file_type = EXTENSIONS.get(ext)
    if not file_type:
        raise HTTPException(400, f"지원하지 않는 파일 형식입니다: .{ext or '(없음)'} (png, jpg, pdf, hwp, hwpx 만 가능)")

    # 3) 크기 검사. 제한보다 1바이트 더 읽어 보고 넘치면 거절한다 (파일 전체를 다 읽지 않아도 판단할 수 있다)
    data = await upload.read(settings.max_file_size_bytes + 1)
    if len(data) > settings.max_file_size_bytes:
        # [규칙 4] 메시지 속 숫자도 설정값에서 가져온다
        raise HTTPException(413, f"파일 크기는 {settings.MAX_FILE_SIZE_MB}MB 이하만 가능합니다.")
    if not data:
        raise HTTPException(400, "빈 파일입니다.")

    # 4) MIME 검사: 브라우저가 알려 준 형식 + 파일 내용(매직 넘버) 둘 다 확인
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    if declared not in MIME_TYPES[file_type]:
        raise HTTPException(400, f"파일 형식(MIME: {declared or '없음'})이 확장자 .{ext} 와 맞지 않습니다.")
    actual = sniff_type(data)
    if actual != file_type:
        raise HTTPException(400, f"파일 내용이 .{ext} 형식이 아닙니다. 확장자만 바꾼 파일인지 확인해 주세요.")

    # 5) 최대 페이지 수 (PDF). 열리지 않는 PDF 는 여기서 거절하지 않고 처리 단계에서 "손상된 파일"로 실패를 기록한다
    if file_type == "pdf":
        try:
            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                pages = pdf.page_count
        except Exception:
            pages = None
        if pages is not None and pages > settings.MAX_PAGES:
            raise HTTPException(400, f"페이지 수가 너무 많습니다. 최대 {settings.MAX_PAGES}페이지까지 가능합니다. (현재 {pages}페이지)")

    # 6) 저장 + DB 기록 + 작업 등록
    doc_id = str(uuid.uuid4())
    storage = DocStorage(settings.STORAGE_DIR, doc_id)
    with stage_log("upload", doc_id, name, detail=f"{len(data)} bytes, {file_type}, lang={language}"):
        storage.create_dirs()
        path = storage.original(file_type)
        path.write_bytes(data)
        file_id = db.insert_uploaded_file(settings.DB_PATH, doc_id, name, str(path), file_type)
        update_status(storage, doc_id=doc_id, file_name=name, state="queued", stage="queued", progress={"done": 0, "total": 0}, error=None)

    request.app.state.pool.submit(
        "document",
        {"doc_id": doc_id, "file_id": file_id, "file_name": name, "file_type": file_type, "file_path": str(path), "language": language},
    )
    return {"doc_id": doc_id, "file_id": file_id, "file_name": name, "status": "queued"}


# ---------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------


@router.get("/{doc_id}/status", response_model=StatusResponse)
def get_status(request: Request, doc_id: str):
    storage = _storage(request, doc_id)
    status = read_status(storage) or {}
    progress = status.get("progress") or {"done": 0, "total": 0}
    total = progress.get("total") or 0
    progress["percent"] = int(progress.get("done", 0) * 100 / total) if total else (100 if status.get("state") == "done" else 0)
    return {
        "doc_id": doc_id,
        "file_name": status.get("file_name", ""),
        "status": status.get("state", "queued"),
        "stage": status.get("stage"),
        "progress": progress,
        "failed_blocks": status.get("failed_blocks"),
        "error": status.get("error"),
        "updated_at": status.get("updated_at"),
    }


@router.get("/{doc_id}/structure")
def get_structure(request: Request, doc_id: str):
    structure = load_structure(_storage(request, doc_id))
    if structure is None:
        raise HTTPException(404, "아직 구조 분석이 끝나지 않았습니다.")
    return structure


def _block_view(request: Request, doc_id: str, storage: DocStorage, block: dict, result: dict | None) -> dict:
    """화면에 필요한 블록 정보: 구조 + 결과 + 이미지 주소 + 최종 결과에 들어갈 글자(rendered)."""
    image = storage.block_image(block["block_id"])
    return {
        **block,
        "result": result,
        "rendered": render_block(block, result, "txt"),
        "image_url": str(request.url_for("get_block_image", doc_id=doc_id, block_id=block["block_id"])) if image.exists() else None,
    }


@router.get("/{doc_id}/blocks")
def list_blocks(request: Request, doc_id: str):
    storage = _storage(request, doc_id)
    structure = load_structure(storage)
    if structure is None:
        raise HTTPException(404, "아직 구조 분석이 끝나지 않았습니다.")
    results = load_results(storage, structure)
    blocks = [_block_view(request, doc_id, storage, b, results.get(b["block_id"])) for b in sorted(structure["blocks"], key=lambda b: b["order"])]
    return {"doc_id": doc_id, "pages": structure["pages"], "kind": structure.get("kind"), "blocks": blocks}


def _get_block(request: Request, doc_id: str, block_id: str):
    storage = _storage(request, doc_id)
    if not is_valid_block_id(block_id):
        raise HTTPException(404, "블록을 찾을 수 없습니다.")
    structure = load_structure(storage)
    block = find_block(structure, block_id) if structure else None
    if block is None:
        raise HTTPException(404, "블록을 찾을 수 없습니다.")
    return storage, structure, block


@router.get("/{doc_id}/blocks/{block_id}")
def get_block(request: Request, doc_id: str, block_id: str):
    storage, _, block = _get_block(request, doc_id, block_id)
    return _block_view(request, doc_id, storage, block, read_json(storage.block_result(block_id)))


@router.get("/{doc_id}/blocks/{block_id}/image", name="get_block_image")
def get_block_image(request: Request, doc_id: str, block_id: str):
    storage, _, _ = _get_block(request, doc_id, block_id)
    path = storage.block_image(block_id)
    if not path.exists():
        raise HTTPException(404, "이 블록에는 이미지가 없습니다.")
    return FileResponse(path, media_type="image/png")


@router.get("/{doc_id}/pages/{page}/image")
def get_page_image(request: Request, doc_id: str, page: int):
    storage = _storage(request, doc_id)
    path = storage.page_image(page)
    if not path.exists():
        raise HTTPException(404, "페이지 이미지가 없습니다. (HWP 문서는 페이지 이미지가 없습니다)")
    return FileResponse(path, media_type="image/png")


@router.get("/{doc_id}/result", response_class=PlainTextResponse)
def get_result(request: Request, doc_id: str, format: str = "txt", page_separator: bool | None = None, debug: bool = False):
    """최종 결과. page_separator 를 주지 않으면 .env 의 MERGE_PAGE_SEPARATOR 를 따른다."""
    settings = request.app.state.settings
    storage = _storage(request, doc_id)
    if format not in ("txt", "md"):
        raise HTTPException(400, "format 은 txt 또는 md 여야 합니다.")
    status = read_status(storage) or {}
    if status.get("state") != "done":
        raise HTTPException(409, "아직 처리가 끝나지 않았거나 실패한 문서입니다.")
    structure = load_structure(storage)
    sep = settings.MERGE_PAGE_SEPARATOR if page_separator is None else page_separator
    text = merge_document(structure, load_results(storage, structure), format, sep, debug)
    media = "text/markdown; charset=utf-8" if format == "md" else "text/plain; charset=utf-8"
    return PlainTextResponse(text, media_type=media)


# ---------------------------------------------------------------------
# retry / 수정
# ---------------------------------------------------------------------


def _ensure_editable(request: Request, storage: DocStorage, doc_id: str) -> None:
    """처리 중인 문서는 고치거나 다시 처리할 수 없다. 워커가 쓰는 파일을 동시에 고치면 결과가 서로 덮어써지기 때문."""
    if request.app.state.pool.is_busy(doc_id):
        raise HTTPException(409, "이 문서는 지금 처리 중입니다. 잠시 후 다시 시도해 주세요.")
    if (read_status(storage) or {}).get("state") != "done":
        raise HTTPException(409, "처리가 끝난 문서만 수정하거나 다시 처리할 수 있습니다.")


@router.post("/{doc_id}/blocks/{block_id}/retry", status_code=202)
def retry_block(request: Request, doc_id: str, block_id: str):
    """
    블록 하나를 다시 처리한다. [규칙 1] block_id 와 order 는 그대로 유지된다 (structure 의 블록을 그대로 다시 처리).
    """
    storage, _, block = _get_block(request, doc_id, block_id)
    _ensure_editable(request, storage, doc_id)
    row = _file_row(request, doc_id)
    set_block_status(storage, {block_id: "pending"})  # 화면에 "처리 중"으로 보이게 한다 (status 만 바뀜)
    request.app.state.pool.submit("retry", _job(row, block_id=block_id))
    logger.info(f"retry 요청 {block_id}", extra=log_ctx(doc_id, row["file_name"], "retry"))
    return {"doc_id": doc_id, "block_id": block_id, "order": block["order"], "status": "pending"}


@router.patch("/{doc_id}/blocks/{block_id}")
def edit_block(request: Request, doc_id: str, block_id: str, body: EditRequest):
    """
    사용자가 고친 텍스트를 edited_text 에 저장한다. [규칙 2] 원본(raw_text, text)은 지우지 않는다.
    저장 후 결과를 다시 합치고 DB 도 갱신한다.
    """
    storage, _, block = _get_block(request, doc_id, block_id)
    _ensure_editable(request, storage, doc_id)
    if read_json(storage.block_result(block_id)) is None:
        raise HTTPException(409, "아직 처리되지 않은 블록입니다.")
    row = _file_row(request, doc_id)
    result = runner.apply_edit(request.app.state.settings, _job(row, block_id=block_id), body.edited_text)
    return _block_view(request, doc_id, storage, block, result)
