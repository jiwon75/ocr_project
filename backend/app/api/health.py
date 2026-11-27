"""
GET /api/health — 서버와 OCR 모델이 준비됐는지 알려 준다.
모델 로딩이 끝나기 전에는 503 + {"status": "not_ready"} 를 돌려준다.
(503 = "지금은 서비스할 수 없음". 상태 검사 도구들이 이 숫자만 보고도 준비 여부를 판단할 수 있게 하기 위해서다)
"""
from fastapi import APIRouter, Request, Response

from app.models.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(request: Request, response: Response):
    pool = request.app.state.pool
    ready = pool.ready_count > 0
    if not ready:
        response.status_code = 503
    return {
        "status": "ready" if ready else "not_ready",
        "workers": {"ready": pool.ready_count, "total": len(pool.slots), "states": pool.states},
    }
