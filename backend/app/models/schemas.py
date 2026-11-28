"""
[이 파일이 하는 일]
API 가 주고받는 데이터의 모양을 정의한다.

[왜 필요한지]
FastAPI 는 이 정의를 보고 (1) 잘못된 요청을 자동으로 거절하고 (2) /docs 화면에 API 설명서를 자동으로 만든다.
프론트 개발자는 /docs 만 보면 어떤 값이 오가는지 알 수 있다.
"""
from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    workers: dict


class UploadResponse(BaseModel):
    doc_id: str
    file_id: int
    file_name: str
    status: str


class ErrorInfo(BaseModel):
    stage: str
    reason: str
    message: str  # 사용자에게 보여 줄 문장


class Progress(BaseModel):
    done: int = 0
    total: int = 0
    percent: int = 0


class StatusResponse(BaseModel):
    doc_id: str
    file_name: str
    status: Literal["queued", "analyzing", "processing", "merging", "done", "failed"]
    stage: str | None = None
    progress: Progress
    failed_blocks: int | None = None
    error: ErrorInfo | None = None
    updated_at: str | None = None


class EditRequest(BaseModel):
    # None 을 보내면 수정 취소 (원래 OCR 결과로 되돌림)
    edited_text: str | None = Field(default=None, description="사용자가 고친 텍스트. null 이면 수정 취소")


class ApplyCorrectionsRequest(BaseModel):
    # 적용할 LLM 교정 제안 번호(sid) 목록. 제안 목록은 GET /api/documents/{doc_id}/assist 의 corrections.suggestions
    sids: list[int] = Field(..., min_length=1, description="적용할 제안 번호")
