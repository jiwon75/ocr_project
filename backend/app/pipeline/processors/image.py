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
import re

import numpy as np

_NUMBER_RE = re.compile(r"^[+-]?\d[\d,]*(\.\d+)?%?$")


def _is_number(text: str) -> bool:
    return bool(_NUMBER_RE.match(text.replace(" ", "")))


def _cx(line: dict) -> float:
    return (line["box"][0] + line["box"][2]) / 2


def _cy(line: dict) -> float:
    return (line["box"][1] + line["box"][3]) / 2


def chart_to_table(lines: list[dict], threshold: float) -> dict | None:
    """
    막대 차트의 글자(막대 위 값, 아래 항목 이름, 범례)를 표로 정리한다. 모양이 안 맞으면 None (그림 속 글자만 남는다).
      예) 항목 25만원/50만원…, 범례 1번/2번/3번, 막대 위 값 23/17/12… →
          | 구분 | 1번 | 2번 | 3번 |  /  | 25만원 | 23 | 17 | 12 | ...
    방법
      1) 숫자가 아닌 글자 중 모든 숫자보다 아래에 있는 첫 줄(글자 2개 이상) = 항목 이름, 그다음 줄 = 범례
      2) 왼쪽 세로축 눈금(첫 항목보다 왼쪽에 있는 숫자)은 뺀다
      3) 남은 숫자(막대 위 값)를 가로로 가장 가까운 항목에 넣고, 항목 안에서는 왼쪽부터 범례 순서로 본다
    [규칙 3] 값의 위치를 짐작해 채우지 않는다: 항목마다 값 개수가 다르면 표를 만들지 않는다.
    """
    # 신뢰도 기준 미달 숫자는 뺀다 (실제 사례: 막대 위 마우스 포인터의 빨간 점이 "0"(0.52)으로 읽혀 값 개수가 어긋났다)
    nums = [l for l in lines if _is_number(l["text"]) and (l.get("confidence") is None or l["confidence"] >= threshold)]
    words = [l for l in lines if l["text"].strip() and not _is_number(l["text"])]
    if len(nums) < 2 or not words:
        return None
    lowest_num = max(_cy(l) for l in nums)
    below: dict[int, list[dict]] = {}
    for l in words:
        if _cy(l) > lowest_num:
            below.setdefault(l["row"], []).append(l)
    rows = [sorted(below[r], key=_cx) for r in sorted(below)]
    cat_idx = next((i for i, r in enumerate(rows) if len(r) >= 2), None)
    if cat_idx is None:
        return None
    cats = rows[cat_idx]
    legend = rows[cat_idx + 1] if cat_idx + 1 < len(rows) else []

    centers = [_cx(c) for c in cats]
    spacing = min(b - a for a, b in zip(centers, centers[1:])) if len(centers) > 1 else 0
    left_edge = centers[0] - spacing / 2
    values = [l for l in nums if _cx(l) >= left_edge and _cy(l) < min(_cy(c) for c in cats)]
    per_cat: dict[int, list[dict]] = {}
    for v in values:
        per_cat.setdefault(min(range(len(cats)), key=lambda i: abs(centers[i] - _cx(v))), []).append(v)
    counts = {len(per_cat.get(i, [])) for i in range(len(cats))}
    if len(counts) != 1 or 0 in counts:
        return None
    n_series = counts.pop()

    names = [t for l in legend for t in l["text"].split()]
    if len(names) != n_series and len(legend) == 1:
        # 범례가 한 줄로 붙어 읽힌 경우("1번2번3번"): 숫자로 시작하는 곳에서 나눠 본다
        names = [t for t in re.split(r"(?<=[^\d\s.])(?=\d)", legend[0]["text"].replace(" ", "")) if t]
    if len(names) != n_series:
        names = ["값"] if n_series == 1 else [f"계열{i + 1}" for i in range(n_series)]

    def cell(row, col, text, conf=None):
        ok = conf is None or conf >= threshold
        c = {"row": row, "col": col, "rowspan": 1, "colspan": 1, "text": text if ok else None,
             "raw_text": text, "confidence": conf, "status": "done" if ok else "failed"}
        if not ok:
            c["uncertain_text"] = text
        return c

    cells = [cell(0, 0, "구분")] + [cell(0, i + 1, n) for i, n in enumerate(names)]
    for r, cat in enumerate(cats, start=1):
        cells.append(cell(r, 0, cat["text"], cat.get("confidence")))
        for s, v in enumerate(sorted(per_cat[r - 1], key=_cx), start=1):
            cells.append(cell(r, s, v["text"], v.get("confidence")))
    return {"n_rows": len(cats) + 1, "n_cols": n_series + 1, "cells": cells}


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

    chart = None
    if block.get("label") == "chart" and lines:
        chart = chart_to_table(lines, ctx.settings.OCR_CONFIDENCE_THRESHOLD)

    result.update(
        status="done",
        chart_table=chart,
        image_path=str(image_path.name) if image_path.exists() else None,
        ocr_text=join_lines(lines),
        ocr_confidence=aggregate_confidence(lines, ctx.settings.OCR_CONFIDENCE_AGG),
        lines=lines,
        ocr_error=ocr_error,
        failure_detail=None if crop is not None else "문서에서 그림 데이터를 찾지 못했습니다 (자리표시자만 남김)",
        processed_at=now(),
    )
    return result
