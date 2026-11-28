"""
API 라우터 모음. 라우터는 "요청 검사 → pipeline 함수 호출 → 응답"만 하고, 처리 로직은 app/pipeline 에 둔다.
"""
from fastapi import APIRouter

from app.api import assist, documents, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(assist.router, tags=["assist"])
