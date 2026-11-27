"""
[2단계 — table 블록 처리]
표를 잘라서
  1) 표 구조 모델(SLANet_plus)로 행·열·병합 셀과 셀 위치를 알아내고
  2) 표 안의 글자 줄을 OCR(또는 PDF 텍스트 레이어)로 읽은 뒤
  3) 줄마다 "가장 많이 겹치는 셀"에 넣는다.
결과는 셀 목록(row, col, rowspan, colspan, text)으로 저장하고, Markdown/HTML 변환은 3단계에서 한다.

[왜 이렇게 나눴는지]
표를 통째로 OCR 하면 칸 구분이 사라져서 "항목 수량 Price 사과 10 ..." 처럼 한 줄로 뭉개진다.
구조(어디가 칸인지)와 내용(칸 안의 글자)을 따로 구한 뒤 합쳐야 표 모양이 유지된다.

[규칙 2] 셀 하나의 confidence 가 기준보다 낮으면 그 셀만 [추출 실패: p1-b003 r2c3] 로 표시한다.
표 전체를 실패로 만들면 잘 읽힌 다른 셀까지 잃고, 반대로 그냥 두면 틀린 글자가 결과에 섞이기 때문이다.
"""
import re

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
    sort_lines,
)

_SPAN_RE = re.compile(r'(colspan|rowspan)\s*=\s*"?(\d+)"?')


def parse_table_tokens(tokens: list[str]) -> tuple[list[dict], int, int]:
    """
    SLANet 의 HTML 조각 목록 → 셀 목록.
      ["<tr>", "<td></td>", "<td", ' colspan="2"', ">", "</td>", "</tr>", ...]
    돌려주는 값: (셀 목록(<td> 순서), 행 수, 열 수). 각 셀 = {"row", "col", "rowspan", "colspan"}

    병합 셀 처리: 위 행의 rowspan 셀이 아래 행의 칸을 차지하고 있으므로, "이미 차지된 칸"을 기록해 두고
    다음 빈칸부터 배치한다. 이렇게 해야 병합이 있어도 각 셀의 실제 열 번호가 맞는다.
    """
    rows: list[list[dict]] = []
    current = None
    for tok in tokens:
        t = tok.strip()
        if t == "<tr>":
            rows.append([])
        elif t.startswith("<td"):
            if not rows:
                rows.append([])
            current = {"rowspan": 1, "colspan": 1}
            rows[-1].append(current)
            for name, value in _SPAN_RE.findall(t):
                current[name] = max(1, int(value))
        elif current is not None and ("colspan" in t or "rowspan" in t):
            for name, value in _SPAN_RE.findall(t):
                current[name] = max(1, int(value))

    occupied: set[tuple[int, int]] = set()
    cells = []
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while (r, c) in occupied:
                c += 1
            cell["row"], cell["col"] = r, c
            for dr in range(cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    occupied.add((r + dr, c + dc))
            cells.append(cell)
            c += cell["colspan"]
    n_rows = max((c["row"] + c["rowspan"] for c in cells), default=0)
    n_cols = max((c["col"] + c["colspan"] for c in cells), default=0)
    return cells, n_rows, n_cols


def _overlap_ratio(line_box, cell_box) -> float:
    """줄 상자 면적 중 셀과 겹치는 비율 (0~1)."""
    x1, y1 = max(line_box[0], cell_box[0]), max(line_box[1], cell_box[1])
    x2, y2 = min(line_box[2], cell_box[2]), min(line_box[3], cell_box[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area = max(1e-6, (line_box[2] - line_box[0]) * (line_box[3] - line_box[1]))
    return inter / area


def assign_lines_to_cells(lines: list[dict], cell_boxes: list[list[float]], min_ratio: float = 0.5):
    """
    줄마다 가장 많이 겹치는 셀을 찾는다. 줄 면적의 min_ratio(절반)를 "넘게" 겹치는 셀이 없으면 어느 셀에도 넣지 않는다.
    (딱 절반씩 걸친 줄은 어느 칸인지 정할 근거가 없다)
    (애매한 줄을 가까운 셀에 억지로 넣는 것은 위치를 추측하는 것이라 하지 않는다. 결과 JSON 의 unassigned_lines 에 남긴다)
    돌려주는 값: (셀 번호 → 줄 목록, 배정 못 한 줄 목록)
    """
    per_cell: dict[int, list[dict]] = {}
    unassigned = []
    for line in lines:
        best, best_ratio = None, 0.0
        for i, cb in enumerate(cell_boxes):
            ratio = _overlap_ratio(line["box"], cb)
            if ratio > best_ratio:
                best, best_ratio = i, ratio
        if best is not None and best_ratio > min_ratio:
            per_cell.setdefault(best, []).append(line)
        else:
            unassigned.append(line)
    return per_cell, unassigned


def process_table_block(ctx: ProcessContext, block: dict) -> dict:
    """
    받는 값: 처리 상자, 표 블록
    돌려주는 값: 블록 결과 dict. result["table"] = {"n_rows", "n_cols", "cells": [...]}
    """
    result = base_result(block)
    threshold = ctx.settings.OCR_CONFIDENCE_THRESHOLD

    if block.get("source") == "hwp":
        # HWP 는 셀 구조와 글자가 파일에 정확히 들어 있다. 모델이 필요 없다
        table = read_source(ctx, block).get("table") or {"n_rows": 0, "n_cols": 0, "cells": []}
        for cell in table["cells"]:
            cell.update(confidence=None, status="done" if cell.get("text") else "empty", raw_text=cell.get("text", ""))
        result.update(source="hwp", status="done", table=table, raw_text=_plain(table), processed_at=now())
        return result

    crop, offset = crop_block(ctx, block)
    structure = ctx.engines.table_structure(crop)
    lines, source = read_lines(ctx, block, crop, offset)
    lines = sort_lines(lines)
    result.update(source=source, lines=lines, raw_text=join_lines(lines), structure_score=structure["score"])

    cells, n_rows, n_cols = parse_table_tokens(structure["tokens"])
    cell_boxes = structure["cells"][: len(cells)]
    if not cells or len(cell_boxes) < len(cells) or structure["score"] < ctx.settings.TABLE_STRUCTURE_THRESHOLD:
        # 구조를 못 알아내면 표 전체를 실패로 둔다. 글자만 한 줄로 이어 붙이면 표의 의미가 망가지기 때문
        result.update(
            status="failed",
            failure_reason="table_structure_failed",
            failure_detail=f"표 구조 인식 실패 (cells={len(cells)}, boxes={len(structure['cells'])}, score={structure['score']:.3f})",
            processed_at=now(),
        )
        return result

    per_cell, unassigned = assign_lines_to_cells(lines, cell_boxes)
    failed_cells = 0
    for i, cell in enumerate(cells):
        cell_lines = sort_lines(per_cell.get(i, []))
        text = join_lines(cell_lines)
        confidence = aggregate_confidence(cell_lines, ctx.settings.OCR_CONFIDENCE_AGG)
        cell["box"] = cell_boxes[i]
        cell["raw_text"] = text  # [규칙 2] 원본 OCR 결과는 항상 남긴다
        cell["confidence"] = confidence
        if not cell_lines:
            cell.update(text="", status="empty")  # 원래 비어 있는 칸. 실패가 아니다
            continue
        ok, reason = judge_text(text, confidence, threshold)
        cell.update(text=text if ok else None, status="done" if ok else "failed", failure_reason=reason)
        failed_cells += 0 if ok else 1

    result.update(
        status="done",
        table={"n_rows": n_rows, "n_cols": n_cols, "cells": cells},
        failed_cells=failed_cells,
        unassigned_lines=unassigned,
        confidence=aggregate_confidence(lines, ctx.settings.OCR_CONFIDENCE_AGG),
        processed_at=now(),
    )
    return result


def _plain(table: dict) -> str:
    return "\n".join(c.get("text", "") for c in table["cells"] if c.get("text"))
