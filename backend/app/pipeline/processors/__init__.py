"""
2단계 블록 처리기 모음. 블록 종류 → 처리 함수.
새 블록 종류를 추가하려면 처리 함수를 만들고 여기 PROCESSORS 에 한 줄 추가하면 된다.
"""
from app.pipeline.processors.image import process_image_block
from app.pipeline.processors.table import process_table_block
from app.pipeline.processors.text import process_text_block

PROCESSORS = {
    "text": process_text_block,
    "title": process_text_block,
    "header": process_text_block,
    "footer": process_text_block,
    "caption": process_text_block,
    "table": process_table_block,
    "image": process_image_block,
}
