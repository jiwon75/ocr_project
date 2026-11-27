from typing import Literal

from pydantic import BaseModel


class OcrPage(BaseModel):
    page: int
    method: Literal["ocr", "text-layer"]  # text-layer: PDF 에 내장된 텍스트를 그대로 사용
    text: str


class OcrResponse(BaseModel):
    filename: str
    page_count: int
    pages: list[OcrPage]
    text: str
