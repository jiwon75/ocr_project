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
    for i, cell in enumerate(cells):
        cell["box"] = cell_boxes[i]
        cell["_lines"] = per_cell.get(i, [])

    cells, n_rows, n_cols, caption_cell = tidy_table(cells, n_rows, n_cols)
    if source == "ocr":
        reread = reread_empty_cells(ctx, crop, cells, threshold)
        if reread:
            lines = sort_lines(lines + reread)
            result.update(lines=lines, raw_text=join_lines(lines))

    agg = ctx.settings.OCR_CONFIDENCE_AGG
    failed_cells = 0
    for cell in cells:
        cell_lines = sort_lines(cell.pop("_lines"))
        text = join_lines(cell_lines)
        confidence = aggregate_confidence(cell_lines, agg)
        cell["raw_text"] = text  # [규칙 2] 원본 OCR 결과는 항상 남긴다
        cell["confidence"] = confidence
        if not cell_lines:
            cell.update(text="", status="empty")  # 원래 비어 있는 칸. 실패가 아니다
            continue
        ok, reason = judge_text(text, confidence, threshold)
        cell.update(text=text if ok else None, status="done" if ok else "failed", failure_reason=reason)
        failed_cells += 0 if ok else 1

    table = {"n_rows": n_rows, "n_cols": n_cols, "cells": cells}
    if caption_cell is not None:
        table["caption"] = _judged_piece(caption_cell.pop("_lines"), threshold, agg)

    # 표 영역 안이지만 어느 칸에도 속하지 않는 글자(예: 영상 자막, 표 아래 주석).
    # 버리면 글자가 소리 없이 사라지므로, 3단계에서 표 아래에 이어 쓴다
    outside = [_judged_piece([line], threshold, agg) for line in sort_lines(unassigned)]

    result.update(
        status="done",
        table=table,
        failed_cells=failed_cells,
        unassigned_lines=unassigned,
        outside_text=outside,
        confidence=aggregate_confidence(lines, agg),
        processed_at=now(),
    )
    return result


def _judged_piece(lines: list[dict], threshold: float, agg: str) -> dict:
    """줄 묶음 → {"text", "raw_text", "confidence", "status"} ([규칙 2] 기준 미달이면 text=None, status=failed)."""
    lines = sort_lines(lines)
    text = join_lines(lines)
    confidence = aggregate_confidence(lines, agg)
    ok, reason = judge_text(text, confidence, threshold)
    return {"text": text if ok else None, "raw_text": text, "confidence": confidence,
            "status": "done" if ok else "failed", "failure_reason": reason}


# ---------------------------------------------------------------------
# 표 구조 다듬기
# 표 구조 모델(SLANet_plus)은 선이 없거나 점선뿐인 표에서 열을 하나 더 만들어
# 같은 열의 글자가 두 열에 지그재그로 흩어지게 하거나, 표 제목을 "전체 병합 행"으로, 표 아래 여백을 빈 행으로 잡는다.
# 셀에 실제로 들어간 글자 줄을 보고 이런 경우만 골라 고친다.
# ---------------------------------------------------------------------


def _shift(cells: list[dict], key: str, after: int):
    for c in cells:
        if c[key] > after:
            c[key] -= 1


def tidy_table(cells: list[dict], n_rows: int, n_cols: int):
    """
    받는 값: 셀 목록(각 셀에 "box", "_lines"), 행 수, 열 수
    돌려주는 값: (셀 목록, 행 수, 열 수, 표 제목 셀 또는 None)
      1) 맨 윗행이 "전체 열을 덮는 칸 하나"뿐이고 글자가 있으면 표 제목으로 뺀다 (Markdown 표에는 병합 칸이 없기 때문)
      2) 글자가 하나도 없는 행을 지운다 (병합에 걸친 행은 건드리지 않는다)
      3) 이웃한 두 열이 "어느 행에서도 동시에 글자가 있지 않고" 칸 위치가 겹치면 같은 열로 보고 합친다
    """
    caption = None
    top = [c for c in cells if c["row"] == 0]
    if n_rows > 1 and len(top) == 1 and top[0]["colspan"] >= n_cols and top[0]["rowspan"] == 1 and top[0]["_lines"]:
        caption = top[0]
        cells = [c for c in cells if c is not caption]
        _shift(cells, "row", -1)
        n_rows -= 1

    for r in reversed(range(n_rows)):
        touching = [c for c in cells if c["row"] <= r < c["row"] + c["rowspan"]]
        if all(c["row"] == r and c["rowspan"] == 1 and not c["_lines"] for c in touching):
            cells = [c for c in cells if c not in touching]
            _shift(cells, "row", r)
            n_rows -= 1

    merged = True
    while merged and n_cols > 1:
        merged = False
        for col in range(n_cols - 1):
            if _can_merge_columns(cells, col):
                cells = _merge_columns(cells, col)
                n_cols -= 1
                merged = True
                break
    return cells, n_rows, n_cols, caption


def _col_cells(cells: list[dict], col: int) -> list[dict]:
    return [c for c in cells if c["col"] <= col < c["col"] + c["colspan"]]


def _can_merge_columns(cells: list[dict], col: int, min_overlap: float = 0.2) -> bool:
    left, right = _col_cells(cells, col), _col_cells(cells, col + 1)
    if any(c["colspan"] != 1 or c["rowspan"] != 1 for c in left + right):
        return False  # 병합 칸이 걸친 열은 의도된 구조일 수 있어 건드리지 않는다
    left_by_row = {c["row"]: c for c in left}
    if any(c["_lines"] and left_by_row.get(c["row"], {}).get("_lines") for c in right):
        return False  # 같은 행에 둘 다 글자가 있으면 진짜 다른 열이다
    if not any(c["_lines"] for c in left) or not any(c["_lines"] for c in right):
        return True  # 한쪽 열이 통째로 비어 있으면 구조 모델이 만든 빈 열이다
    # 둘 다 글자가 있는데 서로 다른 행에만 있다면, 칸 위치가 실제로 겹치는지(같은 세로 줄인지) 확인한다.
    # 진짜 다른 열은 칸이 나란히 붙어 있을 뿐 거의 겹치지 않는다
    ratios = []
    for c in right:
        lc = left_by_row.get(c["row"])
        if lc is None:
            continue
        inter = min(lc["box"][2], c["box"][2]) - max(lc["box"][0], c["box"][0])
        narrow = max(1e-6, min(lc["box"][2] - lc["box"][0], c["box"][2] - c["box"][0]))
        ratios.append(max(0.0, inter) / narrow)
    if not ratios:
        return False
    ratios.sort()
    return ratios[len(ratios) // 2] >= min_overlap


def _merge_columns(cells: list[dict], col: int) -> list[dict]:
    """col+1 열의 칸을 col 열의 같은 행 칸에 합친다 (글자 줄을 옮기고 칸 상자는 두 상자를 감싸게 넓힌다)."""
    left_by_row = {c["row"]: c for c in _col_cells(cells, col)}
    keep = []
    for c in cells:
        if c["col"] == col + 1 and c["row"] in left_by_row:
            lc = left_by_row[c["row"]]
            lc["_lines"] = lc["_lines"] + c["_lines"]
            lc["box"] = [min(lc["box"][0], c["box"][0]), min(lc["box"][1], c["box"][1]),
                         max(lc["box"][2], c["box"][2]), max(lc["box"][3], c["box"][3])]
            continue
        if c["col"] == col + 1:
            c["col"] = col
        keep.append(c)
    _shift(keep, "col", col)
    return keep


_MEANINGFUL_RE = re.compile(r"[0-9A-Za-z가-힣]")


def reread_empty_cells(ctx: ProcessContext, crop, cells: list[dict], threshold: float) -> list[dict]:
    """
    글자 검출 모델은 반투명 배경 위의 한 자리 숫자("1", "4")처럼 작고 가는 글자를 놓친다.
    같은 행 다른 칸에는 글자가 있는데 비어 있는 칸만, 그 열의 글자 가로 범위 × 그 행의 글자 세로 범위를 잘라
    인식 모델에 직접 넣어 본다. 점수가 기준 이상이고 글자·숫자가 들어 있을 때만 채운다 (진짜 빈칸에 엉터리 글자가 들어가지 않게).
    돌려주는 값: 새로 읽은 줄 목록 (셀의 "_lines" 에도 넣는다)
    """
    h, w = crop.shape[:2]
    targets, images = [], []
    for cell in cells:
        if cell["_lines"] or cell["rowspan"] != 1 or cell["colspan"] != 1:
            continue
        row_lines = [l for c in cells if c["row"] == cell["row"] and c is not cell for l in c["_lines"]]
        col_cells = [c for c in cells if c["col"] == cell["col"] and c["colspan"] == 1 and c["_lines"]]
        if not row_lines or len(col_cells) < 2:
            continue
        col_lines = [l for c in col_cells for l in c["_lines"]]
        x1 = max(0, int(min(l["box"][0] for l in col_lines)) - 4)
        x2 = min(w, int(max(l["box"][2] for l in col_lines)) + 4)
        y1 = max(0, int(min(l["box"][1] for l in row_lines)) - 2)
        y2 = min(h, int(max(l["box"][3] for l in row_lines)) + 2)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        targets.append((cell, [x1, y1, x2, y2]))
        images.append(crop[y1:y2, x1:x2])
    if not images:
        return []
    found = []
    for (cell, box), rec in zip(targets, ctx.engines.recognize(images, ctx.language)):
        if rec["confidence"] >= threshold and _MEANINGFUL_RE.search(rec["text"] or ""):
            line = {**rec, "box": box, "reread": True}
            cell["_lines"].append(line)
            found.append(line)
    return found


def _plain(table: dict) -> str:
    return "\n".join(c.get("text", "") for c in table["cells"] if c.get("text"))
