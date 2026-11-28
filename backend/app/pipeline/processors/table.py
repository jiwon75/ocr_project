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

import numpy as np
from PIL import Image, ImageOps

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
    structure_ok = bool(cells) and len(cell_boxes) >= len(cells) and structure["score"] >= ctx.settings.TABLE_STRUCTURE_THRESHOLD
    # 이미지 OCR 은 글자 줄 위치로도 표 칸을 만들어 본다. 구조 모델이 틀렸을 때 대신 쓰기 위해서다
    line_grid = grid_from_lines(lines) if source == "ocr" else None

    if structure_ok:
        per_cell, unassigned = assign_lines_to_cells(lines, cell_boxes)
        for i, cell in enumerate(cells):
            cell["box"] = cell_boxes[i]
            cell["_lines"] = per_cell.get(i, [])
    agrees = structure_ok and line_grid is not None and structure_agrees(cells, line_grid)
    if structure_ok:
        # 구조 모델 결과 다듬기: 엇갈린 열 합치기 → 맨 위·아래의 "글자가 한 칸뿐인 행"(제목·배너·단위 표시)을 표 밖으로
        cells, n_rows, n_cols, caption_cell = tidy_table(cells, n_rows, n_cols)
        cells, n_rows, head_extra, tail_extra = _strip_sparse_edge_rows(cells, n_rows)
    use_lines = line_grid is not None and (
        not structure_ok
        or not agrees
        # 다듬은 뒤에도 열 수가 글자 줄 표와 다르면(제목·배너까지 표로 잡아 열이 늘어난 경우) 글자 줄 표를 쓴다
        or n_cols != line_grid["n_cols"]
    )
    if use_lines:
        # 구조 모델이 서로 다른 행의 글자를 한 칸에 넣었다 = 행·열을 잘못 나눴다. 글자 줄 위치로 만든 칸을 쓴다
        cells, n_rows, n_cols = line_grid["cells"], line_grid["n_rows"], line_grid["n_cols"]
        caption_lines, unassigned = line_grid["head"], line_grid["tail"]
        result["grid_source"] = "lines"
    elif structure_ok:
        caption_lines = (caption_cell.pop("_lines") if caption_cell is not None else []) + head_extra
        # 칸에 못 들어간 줄 중 표 첫 행보다 위에 있는 것(화면 제목 옆 배너 등)은 표 위에, 나머지는 표 아래에 쓴다
        top = min((l["box"][1] for c in cells for l in c["_lines"]), default=0)
        caption_lines += [l for l in unassigned if l["box"][3] <= top]
        unassigned = [l for l in unassigned if l["box"][3] > top] + tail_extra
        result["grid_source"] = "structure"
    else:
        # 구조도 못 알아내고 글자 줄로도 표 모양이 안 나오면 표 전체를 실패로 둔다.
        # 글자만 한 줄로 이어 붙이면 표의 의미가 망가지기 때문
        result.update(
            status="failed",
            failure_reason="table_structure_failed",
            failure_detail=f"표 구조 인식 실패 (cells={len(cells)}, boxes={len(structure['cells'])}, score={structure['score']:.3f})",
            processed_at=now(),
        )
        return result


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
        if not ok and reason == "low_confidence" and ctx.settings.TABLE_SHOW_UNCERTAIN_TEXT:
            # 표 칸은 [추출 실패] 대신 읽은 글자에 (?) 를 붙여 보여 준다. 칸이 비면 표를 읽기 어렵기 때문.
            # status 는 그대로 failed 라서 화면의 실패 셀 수·재시도 버튼은 그대로 동작한다
            cell["uncertain_text"] = text
        failed_cells += 0 if ok else 1

    table = {"n_rows": n_rows, "n_cols": n_cols, "cells": cells}

    # 표 위쪽 글자(제목·배너)와 표 영역 안이지만 칸 밖인 글자(자막·단위 표시)는 버리지 않고 3단계에서 표 위·아래에 쓴다.
    # 줄(같은 높이)마다 한 조각으로 만든다. 신뢰도 기준 미달인 조각(로고 일부·아이콘 등 잡음)은 결과에 넣지 않고
    # JSON 의 noise_lines 에 남긴다 — 칸 밖의 짧은 잡음까지 [추출 실패] 로 찍으면 결과가 읽기 어려워지기 때문
    head, noise_head = _row_pieces(caption_lines, threshold, agg)
    outside, noise_tail = _row_pieces(unassigned, threshold, agg)
    if head:
        table["head_text"] = head
    result["noise_lines"] = noise_head + noise_tail

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


# ---------------------------------------------------------------------
# 글자 줄 위치로 표 칸 만들기
# 구조 모델(SLANet_plus)은 영상 캡처처럼 배경이 복잡한 표에서 칸 높이를 여러 행에 걸치게 잡아
# 순위 1~5 가 한 칸에 뭉치거나 행이 밀리는 일이 있다. 반면 글자 검출·인식은 거의 정확했다.
# 그래서 글자 줄을 세로 위치로 묶어 행을, 가로 위치로 묶어 열을 만든다. (병합 칸은 표현하지 못한다)
# ---------------------------------------------------------------------


def _x_overlap(a: list[float], b: list[float]) -> float:
    """두 가로 구간이 겹치는 길이를 짧은 쪽 길이로 나눈 값 (0~1)."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    return max(0.0, inter) / max(1e-6, min(a[1] - a[0], b[1] - b[0]))


def grid_from_lines(lines: list[dict]) -> dict | None:
    """
    받는 값: sort_lines 를 거친 줄 목록 (줄마다 "row" 가 있다)
    돌려주는 값: {"cells", "n_rows", "n_cols", "head", "tail"} 또는 None(표 모양이 안 나올 때)
      head: 표 위쪽 줄(제목 후보), tail: 표 아래쪽 줄(자막·주석 등)
    방법
      1) 줄 수가 가장 흔한 행들(= 칸이 다 찬 행)에서 i 번째 줄들의 가로 범위를 모아 i 번째 열로 삼는다
      2) 모든 행의 줄을 가로로 가장 많이 겹치는 열에 넣는다
      3) 줄이 하나뿐이거나 여러 열에 걸친 줄이 있는 행이 맨 위/아래에 있으면 표 밖으로 본다
    """
    rows: dict[int, list[dict]] = {}
    for line in lines:
        rows.setdefault(line.get("row", 0), []).append(line)
    order = sorted(rows)
    counts = [len(rows[r]) for r in order if len(rows[r]) >= 2]
    if len(counts) < 2:
        return None
    k = max(counts, key=lambda c: (counts.count(c), c))  # 가장 흔한 줄 수 (같으면 큰 쪽)
    full = [sorted(rows[r], key=lambda l: l["box"][0]) for r in order if len(rows[r]) == k]
    if len(full) < 2:
        return None
    cols = [[min(row[i]["box"][0] for row in full), max(row[i]["box"][2] for row in full)] for i in range(k)]
    if any(_x_overlap(cols[i], cols[i + 1]) > 0.5 for i in range(k - 1)):
        return None  # 열끼리 크게 겹친다 = 줄이 세로로 가지런하지 않다 (표가 아니다)

    def hits(line):
        span = [line["box"][0], line["box"][2]]
        return [i for i, col in enumerate(cols) if _x_overlap(span, col) > 0.3]

    def is_table_row(row):
        # 줄이 2개 이상이고, 여러 열에 걸친 줄이 없고, 열의 절반 이상을 채운 행만 표의 행으로 본다
        # (어느 열에도 안 겹치는 줄은 허용한다 — 아래에서 새 열이 되거나 가장 가까운 열에 들어간다)
        col_hits = [hits(l) for l in row]
        filled = {h[0] for h in col_hits if len(h) == 1}
        return len(row) >= 2 and not any(len(h) >= 2 for h in col_hits) and len(filled) * 2 >= k

    table_rows = [r for r in order if is_table_row(rows[r])]
    if len(table_rows) < 2:
        return None
    first, last = order.index(table_rows[0]), order.index(table_rows[-1])
    head = [l for r in order[:first] for l in rows[r]]
    tail = [l for r in order[last + 1:] for l in rows[r]]
    middle = order[first:last + 1]

    # 병합 칸으로 된 열(예: "월 25만원" 이 두 행에 걸친 "구분" 열)은 칸이 다 찬 행에 없어서 위에서 열로 잡히지 않는다.
    # 표 안에서 어느 열에도 안 겹치는 줄이 2개 이상 세로로 모여 있으면 그 범위를 새 열로 더한다
    loose = sorted([l["box"][0], l["box"][2]] for r in middle for l in rows[r] if not hits(l))
    extra: list[list] = []  # [x1, x2, 줄 수]
    for x1, x2 in loose:
        if extra and x1 <= extra[-1][1]:
            extra[-1][1] = max(extra[-1][1], x2)
            extra[-1][2] += 1
        else:
            extra.append([x1, x2, 1])
    cols = sorted(cols + [[x1, x2] for x1, x2, n in extra if n >= 2])
    n_cols = len(cols)

    def best_col(line):
        span = [line["box"][0], line["box"][2]]
        return max(range(n_cols), key=lambda i: (_x_overlap(span, cols[i]), -abs((span[0] + span[1]) - sum(cols[i]))))

    # 표 중간의 "칸이 덜 찬 행"
    #  - 그 줄들이 들어갈 칸이 바로 위 행에서 비어 있으면 위 행에 붙인다. 병합 칸 글자("월 25만원")는 병합된 두 행
    #    사이 높이에 따로 한 줄로 잡히는데, 병합 칸은 첫 행에 쓰는 것이 맞기 때문이다
    #    (가장 가까운 행에 붙이면 그룹마다 위·아래가 들쭉날쭉했다)
    #  - 아니면 그대로 한 행으로 둔다. 검출이 숫자 몇 개를 놓쳐 덜 찬 진짜 행이다 (빈 칸은 뒤에서 다시 읽는다)
    grid_rows: dict[int, list[dict]] = {}
    above = None
    for r in middle:
        if r not in table_rows and above is not None:
            used = {best_col(l) for l in grid_rows[above]}
            if not ({best_col(l) for l in rows[r]} & used):
                grid_rows[above] += rows[r]
                continue
        grid_rows[r] = list(rows[r])
        above = r

    cells = []
    for gr, r in enumerate(sorted(grid_rows)):
        row = grid_rows[r]
        y1, y2 = min(l["box"][1] for l in row), max(l["box"][3] for l in row)
        per_col: dict[int, list[dict]] = {}
        for line in row:
            per_col.setdefault(best_col(line), []).append(line)
        for c in range(n_cols):
            cells.append({"row": gr, "col": c, "rowspan": 1, "colspan": 1,
                          "box": [cols[c][0], y1, cols[c][1], y2], "_lines": per_col.get(c, [])})
    return {"cells": cells, "n_rows": len(grid_rows), "n_cols": n_cols, "head": head, "tail": tail}


def structure_agrees(cells: list[dict], line_grid: dict) -> bool:
    """
    구조 모델의 칸(cells, "_lines" 포함)이 글자 줄로 만든 행과 맞는지.
    한 칸 안에 글자 줄 표의 서로 다른 두 행의 글자가 들어 있으면 구조 모델이 행을 잘못 나눈 것이다.
    """
    row_of = {id(l): c["row"] for c in line_grid["cells"] for l in c["_lines"]}
    for cell in cells:
        if len({row_of[id(l)] for l in cell.get("_lines", []) if id(l) in row_of}) > 1:
            return False
    return True


def _row_pieces(lines: list[dict], threshold: float, agg: str) -> tuple[list[dict], list[dict]]:
    """
    줄 목록 → (통과한 조각 목록, 버린 줄 목록). 같은 높이의 줄은 한 조각으로 묶는다.
    신뢰도 기준 미달인 줄은 조각에서 빼서 버린 줄 목록으로 돌려준다.
    """
    # 글자·숫자가 2개 미만인 조각("o", "D", 아이콘)도 잡음으로 본다
    keep = [l for l in lines if (l.get("confidence") is None or l["confidence"] >= threshold)
            and len(_MEANINGFUL_RE.findall(l["text"])) >= 2]
    dropped = [l for l in lines if l not in keep]
    rows: dict[int, list[dict]] = {}
    for line in sort_lines(keep):
        rows.setdefault(line["row"], []).append(line)
    return [_judged_piece(rows[r], threshold, agg) for r in sorted(rows)], dropped


def _strip_sparse_edge_rows(cells: list[dict], n_rows: int):
    """
    맨 위·맨 아래에서 "글자가 든 칸이 하나 이하인 행"을 표 밖으로 뺀다 (병합 칸이 걸친 행은 건드리지 않는다).
    실제 사례: 레이아웃 모델이 화면 제목·배너·"*단위: 만원" 까지 표 영역으로 잡아, 표 맨 위에 제목만 든 행이 생겼다.
    돌려주는 값: (셀 목록, 행 수, 위쪽으로 뺀 줄, 아래쪽으로 뺀 줄)
    """
    head, tail = [], []

    def sparse(r):
        row = [c for c in cells if c["row"] == r]
        if any(c["rowspan"] != 1 for c in cells if c["row"] <= r < c["row"] + c["rowspan"] and c["row"] != r):
            return False
        return len(row) > 0 and sum(1 for c in row if c["_lines"]) <= 1 and all(c["rowspan"] == 1 for c in row)

    while n_rows > 2 and sparse(0):
        head += [l for c in cells if c["row"] == 0 for l in c["_lines"]]
        cells = [c for c in cells if c["row"] != 0]
        _shift(cells, "row", -1)
        n_rows -= 1
    while n_rows > 2 and sparse(n_rows - 1):
        tail = [l for c in cells if c["row"] == n_rows - 1 for l in c["_lines"]] + tail
        cells = [c for c in cells if c["row"] != n_rows - 1]
        n_rows -= 1
    return cells, n_rows, head, tail


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


def _equalized_x2(piece: np.ndarray) -> np.ndarray:
    """
    흑백으로 바꿔 밝기 분포를 고르게 펴고(histogram equalization) 2배로 키운다.
    흐린 배경 위 숫자에서 점수가 크게 올랐다 (실제 측정: "9" 0.64 → 0.90). 원본과 둘 다 읽어 점수가 높은 쪽을 쓴다.
    """
    im = ImageOps.equalize(ImageOps.grayscale(Image.fromarray(piece)))
    im = im.resize((im.width * 2, im.height * 2), Image.BICUBIC).convert("RGB")
    return np.asarray(im)


def _needs_reread(cell: dict, row_lines: list[dict], threshold: float) -> bool:
    """
    다시 읽어 볼 칸인지.
    - 비어 있는 칸: 검출 모델이 반투명 배경 위 한 자리 숫자("1", "4")를 놓친 경우
    - 짧은 글자(3자 이하)인데 점수가 기준 미달이거나, 글자·숫자가 아니거나("•", "]"),
      검출 상자가 행 높이의 절반도 안 되는 칸: 흐린 숫자의 일부만 검출돼 "9" 가 "•" 로, "7" 이 "]" 로 읽힌 경우 (실제 사례)
    긴 글자(머리글·브랜드명)는 좁게 잘라 다시 읽으면 오히려 글자가 잘리므로 건드리지 않는다.
    """
    lines = cell["_lines"]
    if not lines:
        return True
    text = " ".join(l["text"] for l in lines).strip()
    if len(text) > 3:
        return False
    row_h = max(l["box"][3] for l in row_lines) - min(l["box"][1] for l in row_lines)
    own_h = max(l["box"][3] for l in lines) - min(l["box"][1] for l in lines)
    low = any(l.get("confidence") is not None and l["confidence"] < threshold for l in lines)
    return low or not _MEANINGFUL_RE.search(text) or own_h < 0.5 * row_h


def reread_empty_cells(ctx: ProcessContext, crop, cells: list[dict], threshold: float) -> list[dict]:
    """
    검출 모델이 놓쳤거나 일부만 잡은 칸을 다시 읽는다 (_needs_reread 참고).
    그 열 글자들의 가운데를 중심으로 글자 폭만큼, 그 행의 글자 세로 범위만큼 잘라 인식 모델에 직접 넣는다.
    - 열 전체 폭으로 자르면 빈 공간이 많아져 한 자리 숫자 점수가 0.5~0.7 로 떨어졌다 (실제 측정). 그래서 좁게 자른다
    - 자르는 폭에 따라 "11" 이 "1" 로 읽히기도 해서, 폭을 두 가지로 잘라 둘 중 점수가 높은 결과를 쓴다
    - 점수가 기준 이상이고 글자·숫자가 들어 있을 때만 채운다 (진짜 빈칸에 엉터리 글자가 들어가지 않게).
      원래 글자가 "•" 처럼 의미 없는 기호였으면, 다시 읽은 결과가 기준 미달이어도 그것으로 바꿔 [추출 실패]로 드러나게 한다
    돌려주는 값: 새로 읽은 줄 목록 (셀의 "_lines" 도 바꾼다)
    """
    h, w = crop.shape[:2]
    targets, images = [], []
    for cell in cells:
        if cell["rowspan"] != 1 or cell["colspan"] != 1:
            continue
        others = [l for c in cells if c["row"] == cell["row"] and c is not cell for l in c["_lines"]]
        col_cells = [c for c in cells if c["col"] == cell["col"] and c["colspan"] == 1 and c["_lines"]]
        if not others or len(col_cells) < 2 or not _needs_reread(cell, others + cell["_lines"], threshold):
            continue
        col_lines = [l for c in col_cells for l in c["_lines"]]
        y1 = max(0, int(min(l["box"][1] for l in others)) - 4)
        y2 = min(h, int(max(l["box"][3] for l in others)) + 4)
        centers = sorted((l["box"][0] + l["box"][2]) / 2 for l in col_lines)
        widths = sorted(l["box"][2] - l["box"][0] for l in col_lines)
        cx, med_w = centers[len(centers) // 2], widths[len(widths) // 2]
        row_h = y2 - y1
        for half in (max(med_w * 0.75, row_h * 0.55), max(med_w * 0.9, row_h * 0.8)):
            x1, x2 = max(0, int(cx - half)), min(w, int(cx + half))
            if x2 - x1 < 4 or row_h < 4:
                break
            piece = crop[y1:y2, x1:x2]
            targets += [(cell, [x1, y1, x2, y2])] * 2
            images += [piece, _equalized_x2(piece)]
    if not images:
        return []

    best: dict[int, tuple[dict, dict, list]] = {}  # id(cell) → (cell, 가장 점수 높은 결과, 상자)
    for (cell, box), rec in zip(targets, ctx.engines.recognize(images, ctx.language)):
        if not _MEANINGFUL_RE.search(rec["text"] or ""):
            continue
        prev = best.get(id(cell))
        if prev is None or rec["confidence"] > prev[1]["confidence"]:
            best[id(cell)] = (cell, rec, box)

    found = []
    for cell, rec, box in best.values():
        neighbours = [c for c in cells if c["row"] == cell["row"] and abs(c["col"] - cell["col"]) == 1]
        if any(" ".join(l["text"] for l in c["_lines"]).strip() == rec["text"].strip() for c in neighbours):
            # 옆 칸과 같은 글자 = 두 칸에 걸친(병합된) 글자가 비어 있는 칸 자리까지 넘어온 것. 칸을 채우지 않는다
            # (실제 사례: 두 칸 병합 머리글 "구분" 이 빈 옆 칸에서 다시 읽혀 "구분 | 구분" 이 됐다)
            continue
        old = cell["_lines"]
        old_text = " ".join(l["text"] for l in old).strip()
        old_meaningless = bool(old) and not _MEANINGFUL_RE.search(old_text)
        if rec["confidence"] >= threshold or old_meaningless:
            line = {**rec, "box": box, "reread": True, "replaced": [l["text"] for l in old] or None}
            cell["_lines"] = [line]
            found.append(line)
    return found


def _plain(table: dict) -> str:
    return "\n".join(c.get("text", "") for c in table["cells"] if c.get("text"))
