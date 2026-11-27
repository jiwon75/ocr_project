from fastapi import APIRouter

from app.api.routes import health, items, ocr

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(items.router, prefix="/items", tags=["items"])
api_router.include_router(ocr.router, prefix="/ocr", tags=["ocr"])
