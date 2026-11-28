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
    ocr_lines,
    read_lines,
    read_source,
)

# 글자 높이(픽셀)가 이보다 작으면 위아래를 좁게 다시 잘라 2배로 키워 다시 읽는다. 화면 캡처의 작은 주석 글자(높이 약 12px)는
# 검출 모델이 단어 몇 개만 잡아 "냉각수 입니다. 정비저" 처럼 대부분 빠졌다 (그랑 콜레오스 오일 제원 아래 주석).
# 위아래 여백(CROP_PADDING_PX=12)이 줄 높이만큼 커서 윗줄·아랫줄까지 섞여 들어온 것도 원인이라, 위아래 여백은 4px 만 둔다
SMALL_TEXT_PX = 16
SMALL_TEXT_SCALE = 2
SMALL_TEXT_VPAD = 4


def reread_small_text(ctx: ProcessContext, block: dict, offset: tuple[float, float], lines: list[dict]) -> tuple[list[dict], bool]:
    """작은 글자 블록이면 좁게 잘라 키워서 다시 읽고, 더 많이 읽혔으면 그 결과를 쓴다. 돌려주는 값: (줄 목록, 다시 읽었는지)"""
    x1, y1, x2, y2 = block["bbox"]
    heights = sorted(l["box"][3] - l["box"][1] for l in lines)
    small = (heights and heights[len(heights) // 2] < SMALL_TEXT_PX) or (y2 - y1) < SMALL_TEXT_PX * 1.5
    if not small:
        return lines, False
    import numpy as np
    from PIL import Image

    page = ctx.page_rgb(block["page"])
    h, w = page.shape[:2]
    pad = ctx.settings.CROP_PADDING_PX
    tx1, ty1 = max(0, int(x1) - pad), max(0, int(y1) - SMALL_TEXT_VPAD)
    tx2, ty2 = min(w, int(round(x2)) + pad), min(h, int(round(y2)) + SMALL_TEXT_VPAD)
    tight = page[ty1:ty2, tx1:tx2]
    if tight.size == 0:
        return lines, False
    k = SMALL_TEXT_SCALE
    big = np.asarray(Image.fromarray(tight).resize((tight.shape[1] * k, tight.shape[0] * k), Image.LANCZOS))
    bigger = ocr_lines(ctx, big)
    ox, oy = offset
    for l in bigger:  # 키운 좌표 → 블록 crop 좌표 (다른 줄들과 같은 기준)
        bx1, by1, bx2, by2 = l["box"]
        l["box"] = [round(bx1 / k + tx1 - ox, 1), round(by1 / k + ty1 - oy, 1), round(bx2 / k + tx1 - ox, 1), round(by2 / k + ty1 - oy, 1)]

    def chars(ls):
        return sum(len(l["text"].strip()) for l in ls)

    if chars(bigger) > chars(lines):
        return bigger, True
    return lines, False


def drop_lines_of_other_blocks(ctx: ProcessContext, block: dict, lines: list[dict], offset: tuple[float, float]):
    """
    자를 때 둔 여백에 걸린 "다른 블록의 줄"을 뺀다 (가운데 점이 이 블록 밖이면서 다른 블록 안에 있는 줄).
    촘촘한 주석 줄은 위·아래 블록의 줄이 여백에 걸려 같은 문장이 두 블록에 나왔다.
    어느 블록에도 속하지 않는 여백의 글자(그림 속 글자 등)는 그대로 둔다 — 빼면 결과에서 아예 사라진다.
    돌려주는 값: (남길 줄, 뺀 줄)
    """
    ox, oy = offset
    bx1, by1, bx2, by2 = block["bbox"]
    others = [b["bbox"] for b in ctx.structure["blocks"]
              if b["page"] == block["page"] and b["block_id"] != block["block_id"] and b.get("bbox")]
    keep, dropped = [], []
    for l in lines:
        cx, cy = (l["box"][0] + l["box"][2]) / 2 + ox, (l["box"][1] + l["box"][3]) / 2 + oy
        outside = not (bx1 <= cx <= bx2 and by1 <= cy <= by2)
        elsewhere = any(o[0] <= cx <= o[2] and o[1] <= cy <= o[3] for o in others)
        (dropped if outside and elsewhere else keep).append(l)
    return keep, dropped


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
        if source == "ocr":
            lines, upscaled = reread_small_text(ctx, block, offset, lines)
            if upscaled:
                result["upscaled"] = SMALL_TEXT_SCALE
            lines, dropped = drop_lines_of_other_blocks(ctx, block, lines, offset)
            dropped = [{**l, "drop_reason": "outside_block"} for l in dropped]
            if block.get("label") == "uncovered_text":
                # 레이아웃 모델이 아니라 우리가 페이지에서 다시 찾은 글자 블록은 반투명 워터마크("oiltop")만 잡힌 경우가 있다.
                # 표와 같은 기준(획 진하기)으로 워터마크 줄을 뺀다. 다 빠지면 빈 블록 → 결과 파일에서 빠진다 (merge.py)
                from app.pipeline.processors.table import WATERMARK_INK_RATIO, _ink_ratio

                faint = [l for l in lines if _ink_ratio(crop, l["box"]) < WATERMARK_INK_RATIO]
                lines = [l for l in lines if l not in faint]
                dropped += [{**l, "drop_reason": "faint_watermark"} for l in faint]
            if dropped:
                result["dropped_lines"] = dropped  # [규칙 2] 버린 줄도 결과 JSON 에는 남긴다
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
