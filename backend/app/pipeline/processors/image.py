"""
[2단계 — image 블록 처리]
그림 영역을 이미지 파일로 저장하고, 그림 안에 글자가 있으면 OCR 결과도 함께 저장한다.
최종 결과(3단계)에는 그림 대신 [이미지: p1-b005] 자리표시자가 들어간다.

[왜 그림 속 글자를 최종 텍스트에 넣지 않는지]
그림 속 글자(그래프 눈금, 사진 속 간판 등)는 본문 흐름과 상관없는 경우가 많아, 본문 중간에 끼면 읽기 어렵다.
대신 블록 결과 JSON(ocr_text)에 남겨 두어 화면에서 확인할 수 있게 한다.
"""
from PIL import Image

from app.pipeline.processors.common import (
    ProcessContext,
    aggregate_confidence,
    base_result,
    crop_block,
    join_lines,
    now,
    ocr_lines,
    read_source,
)
import numpy as np


def process_image_block(ctx: ProcessContext, block: dict) -> dict:
    """
    받는 값: 처리 상자, 이미지 블록
    돌려주는 값: 블록 결과 dict (image_path, ocr_text, ocr_confidence 포함)
    """
    result = base_result(block)
    crop = None

    if block.get("source") == "hwp":
        # HWP 안에 들어 있던 그림 파일을 PNG 로 바꿔 저장한다 (화면에서 형식 구분 없이 보여 주기 위해)
        src = read_source(ctx, block)
        name = src.get("image_file")
        if name:
            with Image.open(ctx.storage.source_dir / name) as im:
                im = im.convert("RGB")
                im.save(ctx.storage.block_image(block["block_id"]))
                crop = np.asarray(im)
    else:
        crop, _ = crop_block(ctx, block)

    image_path = ctx.storage.block_image(block["block_id"])
    lines, ocr_error = [], None
    if crop is not None:
        try:
            lines = ocr_lines(ctx, crop)
        except Exception as e:  # 그림 속 글자는 덤이므로, OCR 이 실패해도 이미지 저장 자체는 성공으로 둔다
            ocr_error = f"{type(e).__name__}: {e}"

    result.update(
        status="done",
        image_path=str(image_path.name) if image_path.exists() else None,
        ocr_text=join_lines(lines),
        ocr_confidence=aggregate_confidence(lines, ctx.settings.OCR_CONFIDENCE_AGG),
        lines=lines,
        ocr_error=ocr_error,
        failure_detail=None if crop is not None else "문서에서 그림 데이터를 찾지 못했습니다 (자리표시자만 남김)",
        processed_at=now(),
    )
    return result
