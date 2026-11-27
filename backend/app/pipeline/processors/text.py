"""
[2단계 — text / title / header / footer / caption 블록 처리]
블록을 잘라 저장하고, 글자를 뽑고, confidence 로 "추출 실패" 여부를 판정한다.

[왜 이렇게 나눴는지]
제목·본문·머리말·캡션은 화면에서 보이는 모양만 다를 뿐, 글자를 뽑는 방법은 같다.
종류별 차이(제목은 # 붙이기 등)는 3단계(merge.py)에서 처리하고, 여기서는 "글자 뽑기"에만 집중한다.
"""
from app.pipeline.processors.common import (
    ProcessContext,
    aggregate_confidence,
    base_result,
    crop_block,
    join_lines,
    judge_text,
    now,
    read_lines,
    read_source,
)


def process_text_block(ctx: ProcessContext, block: dict) -> dict:
    """
    받는 값: 처리 상자, structure 의 블록 하나
    돌려주는 값: 블록 결과 dict (results/{block_id}.json 에 저장될 내용)
    """
    result = base_result(block)

    if block.get("source") == "hwp":
        # HWP 는 파일에 저장된 글자를 그대로 쓴다. 자를 이미지가 없다 (bbox null)
        text = read_source(ctx, block).get("text", "")
        lines = [{"text": text, "confidence": None}]
        source = "hwp"
    else:
        crop, offset = crop_block(ctx, block)
        lines, source = read_lines(ctx, block, crop, offset)
        text = join_lines(lines)

    confidence = aggregate_confidence(lines, ctx.settings.OCR_CONFIDENCE_AGG)
    ok, reason = judge_text(text, confidence, ctx.settings.OCR_CONFIDENCE_THRESHOLD)

    result.update(
        source=source,
        raw_text=text,  # [규칙 2] 통과하든 실패하든 원본 그대로 남긴다 (맞춤법 교정·보정 없음)
        confidence=confidence,
        lines=lines,
        # [규칙 2] 실패면 text 를 비워 두고, 3단계에서 [추출 실패: p1-b003] 로 표시한다
        text=text if ok else None,
        status="done" if ok else "failed",
        failure_reason=reason,
        failure_detail=(
            f"confidence {confidence} < 기준값 {ctx.settings.OCR_CONFIDENCE_THRESHOLD}" if reason == "low_confidence" else
            "인식된 글자가 없습니다" if reason == "empty" else None
        ),
        processed_at=now(),
    )
    return result
