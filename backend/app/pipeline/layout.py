"""
[1단계 — 이 파일이 하는 일]
문서 전체의 구조를 먼저 파악한다.
  1) 모든 페이지에 레이아웃 분석을 돌려 블록(text/title/table/image/header/footer/caption)을 찾는다
  2) 페이지 안에서 읽기 순서(reading order)를 정한다 — 2단 편집 문서도 왼쪽 단 → 오른쪽 단 순서로
  3) 읽기 순서대로 block_id(p1-b001)와 문서 전체 순번 order 를 붙인다
  4) structure.json 으로 저장한다

[왜 "구조 먼저, 내용은 나중"으로 나눴는지]
번호를 먼저 다 정해 두면, 2단계에서 블록을 어떤 순서로 처리하든(실패·재시도·병렬 처리) 최종 순서가 흔들리지 않는다.
[규칙 1] 그래서 block_id 와 order 는 이 파일에서만 만들어지고, 다른 단계는 읽기만 한다.
"""
from pathlib import Path

import numpy as np
from PIL import Image

from app.pipeline.storage import DocStorage, write_json
from app.pipeline.structure import save_new_structure

# 레이아웃 모델의 세부 라벨 → 우리가 쓰는 블록 종류.
# 모델은 20가지 넘는 라벨을 주지만, 처리 방법이 같은 것끼리 묶어야 2단계 코드가 단순해진다.
LABEL_TO_TYPE = {
    "doc_title": "title",
    "paragraph_title": "title",
    "table": "table",
    "image": "image",
    "chart": "image",
    "seal": "image",
    "header_image": "image",
    "footer_image": "image",
    "header": "header",
    "footer": "footer",
    "number": "footer",  # 페이지 번호
    "figure_title": "caption",
    "table_title": "caption",
    "chart_title": "caption",
    # 그 밖의 text, abstract, content, reference, footnote, formula, algorithm, aside_text 등은 모두 text
}


def label_to_type(label: str) -> str:
    return LABEL_TO_TYPE.get(label, "text")


# ---------------------------------------------------------------------
# 읽기 순서: XY-cut 알고리즘
# ---------------------------------------------------------------------


def _split_by_gap(blocks: list[dict], axis: int) -> list[list[dict]]:
    """
    블록들을 axis 방향(0=가로 x, 1=세로 y)으로 투영했을 때 "빈 틈"이 있는 곳에서 자른다.
    예) 2단 문서를 x 방향으로 투영하면 왼쪽 단과 오른쪽 단 사이에 빈 틈이 생긴다 → 두 그룹으로 나뉜다.
    """
    lo, hi = axis, axis + 2  # bbox 의 [x1,y1,x2,y2] 중 x면 (0,2), y면 (1,3)
    items = sorted(blocks, key=lambda b: b["bbox"][lo])
    groups, current, current_end = [], [], None
    for b in items:
        start, end = b["bbox"][lo], b["bbox"][hi]
        if current and start >= current_end:  # 지금까지 그룹의 끝보다 뒤에서 시작 → 빈 틈이 있다
            groups.append(current)
            current, current_end = [], None
        current.append(b)
        current_end = end if current_end is None else max(current_end, end)
    if current:
        groups.append(current)
    return groups


def sort_reading_order(blocks: list[dict]) -> list[dict]:
    """
    받는 값: 한 페이지의 블록 목록 (bbox 필수)
    돌려주는 값: 읽기 순서로 정렬된 블록 목록

    방법(XY-cut): 세로 틈(단 구분)으로 먼저 나눠 보고 → 안 되면 가로 틈(위/아래)으로 나누고 → 각 조각에서 반복.
    - "세로 틈 먼저"인 이유: 2단 문서에서 가로로 먼저 자르면 왼쪽 단 1줄 → 오른쪽 단 1줄 식으로 섞여 읽힌다.
    - 페이지 전체 폭 제목이나 그림이 있으면 세로 틈이 없어서 가로로 먼저 잘리고,
      그 아래 2단 부분에서 다시 세로로 잘린다 → 제목 → 왼쪽 단 → 오른쪽 단 순서가 된다.
    - 더 나눌 수 없으면 위→아래, 왼→오른쪽 순으로 정렬한다.
    """
    if len(blocks) <= 1:
        return list(blocks)
    for axis in (0, 1):
        groups = _split_by_gap(blocks, axis)
        if len(groups) > 1:
            ordered = []
            for g in groups:
                ordered.extend(sort_reading_order(g))
            return ordered
    return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))


# ---------------------------------------------------------------------
# 번호 붙이기 — [규칙 1] block_id/order 가 만들어지는 유일한 곳
# ---------------------------------------------------------------------


def assign_block_ids(pages_blocks: list[tuple[int, list[dict]]]) -> list[dict]:
    """
    받는 값: [(페이지번호, 읽기 순서로 정렬된 블록 목록), ...] (페이지 순서대로)
    돌려주는 값: block_id 와 order 가 붙은 블록 목록

    block_id = p{페이지}-b{페이지 안 순번 3자리}, order = 문서 전체 통합 순번(1부터)
    """
    out, order = [], 0
    for page_no, blocks in pages_blocks:
        for seq, b in enumerate(blocks, start=1):
            order += 1
            out.append({"block_id": f"p{page_no}-b{seq:03d}", "order": order, "page": page_no, **b, "status": "pending"})
    return out


# ---------------------------------------------------------------------
# 1단계 실행
# ---------------------------------------------------------------------


def _inside(box: list[float], bbox: list[float]) -> bool:
    """글자 상자의 가운데 점이 블록 안에 있는지."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]


def _mostly_inside(box: list[float], bbox: list[float], ratio: float = 0.7) -> bool:
    """글자 상자 넓이의 ratio 이상이 블록 안에 있는지."""
    iw = min(box[2], bbox[2]) - max(box[0], bbox[0])
    ih = min(box[3], bbox[3]) - max(box[1], bbox[1])
    area = max(1e-6, (box[2] - box[0]) * (box[3] - box[1]))
    return iw > 0 and ih > 0 and iw * ih / area >= ratio


def _table_from_boxes(boxes: list[list[float]], min_rows: int = 3, min_cols: int = 2) -> list[float] | None:
    """
    글자 상자들이 행·열로 가지런하면(3행 2열 이상) 표 영역을 돌려준다. 아니면 None.
    2단계 표 처리와 같은 방법(processors.table.grid_from_lines)으로 판단한다.
    """
    from app.pipeline.processors.common import sort_lines
    from app.pipeline.processors.table import grid_from_lines

    grid = grid_from_lines(sort_lines([{"text": "", "box": list(b)} for b in boxes]))
    if grid is None or grid["n_rows"] < min_rows or grid["n_cols"] < min_cols:
        return None
    inner = [l["box"] for c in grid["cells"] for l in c["_lines"]]
    return [min(b[0] for b in inner), min(b[1] for b in inner), max(b[2] for b in inner), max(b[3] for b in inner)]


def _group_boxes(boxes: list[list[float]], vgap_ratio: float, line_height: float | None = None) -> list[list[float]]:
    """
    글자 줄 상자들을 문단 상자로 묶는다.
      1) 같은 높이에서 가로로 가까운(글자 높이 2배 이내) 상자끼리 한 줄로
      2) 가로로 겹치고 세로 틈이 기준 높이 × vgap_ratio 보다 작은 줄끼리 한 문단으로
         기준 높이: line_height 를 주면 그 값(한 줄 높이), 안 주면 두 묶음 중 낮은 쪽 높이(이미지 OCR 의 예전 방식)
         (묶음 높이를 쓰면 긴 목록 두 개가 소제목을 사이에 두고도 하나로 합쳐졌다 — 볼보 PDF 4쪽)
    """
    loose = sorted(boxes, key=lambda b: ((b[1] + b[3]) / 2, b[0]))
    groups: list[list[float]] = []  # 묶음마다 감싸는 상자
    for b in loose:
        h = b[3] - b[1]
        for g in groups:
            same_line = min(b[3], g[3]) - max(b[1], g[1]) > 0.5 * min(h, g[3] - g[1])
            near = b[0] - g[2] < 2 * h and g[0] - b[2] < 2 * h
            if same_line and near:
                g[:] = [min(g[0], b[0]), min(g[1], b[1]), max(g[2], b[2]), max(g[3], b[3])]
                break
        else:
            groups.append(list(b))
    # 겹치거나 바로 위아래로 붙은 묶음은 한 블록(문단)으로 합친다. 따로 두면 블록을 잘라 읽을 때 옆 줄 글자까지
    # 함께 읽혀 같은 글이 두 번 나온다 (실제 사례: 세 줄짜리 배너 "누구나 평생 마르지 않는…" 이 중복 출력됐다)
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                gap = vgap_ratio * (line_height or min(a[3] - a[1], b[3] - b[1]))
                x_touch = min(a[2], b[2]) - max(a[0], b[0]) > 0
                y_touch = min(a[3], b[3]) - max(a[1], b[1]) > -gap
                if x_touch and y_touch:
                    groups[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del groups[j]
                    merged = True
                    break
            if merged:
                break
    return groups


def uncovered_text_blocks(engines, page_rgb: np.ndarray, blocks: list[dict], source: str, min_height_ratio: float = 0.0) -> list[dict]:
    """
    레이아웃 모델이 놓친 글자를 찾는다. (실제 사례: 영상 캡처 아래쪽 자막을 아무 블록으로도 잡지 않아 결과에서 통째로 빠졌다)
    페이지 전체에서 글자 줄을 검출하고, 어느 블록에도 속하지 않는 줄을
      1) 행·열로 가지런히 놓여 있으면 표 블록 하나로 묶는다
         (실제 사례: 빨간 강조 테두리가 있는 표를 레이아웃 모델이 못 잡아, 칸 글자 95개가 낱개 블록으로 쪼개졌다)
      2) 나머지는 페이지 높이의 min_height_ratio 보다 작은 글자(영상 플레이어의 재생 시간 등)를 버리고,
         가로로 가까운 것끼리 묶어 text 블록으로 만든다.
    여기서는 위치만 정한다. 글자는 2단계에서 다른 블록과 똑같이 읽고 [규칙 2]로 판정한다.
    이 bbox 는 실제로 검출된 글자 상자를 감싼 것이라 추측이 아니다 ([규칙 3]).
    """
    # 블록에 70% 이상 들어간 글자만 "블록 안"으로 본다. 블록 가장자리에 반쯤 걸친 글자는 블록을 잘라 읽을 때
    # 일부만 남아 엉뚱하게 읽힌다 (실제 사례: 표 블록에 반쯤 걸친 "귀곰" 로고가 표 안에서 "곰" 으로만 읽혔다)
    # 표 블록이 아닌 블록은 예전처럼 가운데 점만 본다 (꼬리말 아이콘·제목처럼 상자가 블록보다 조금 큰 글자가 중복되지 않게)
    def covered(b, blk):
        return _mostly_inside(b, blk["bbox"]) if blk["type"] == "table" else _inside(b, blk["bbox"])

    loose = [b for b in engines.detect_text(page_rgb) if not any(covered(b, blk) for blk in blocks)]
    found = []
    table_box = _table_from_boxes(loose)
    if table_box is not None:
        found.append({"type": "table", "label": "uncovered_table", "score": None,
                      "bbox": [round(v, 1) for v in table_box], "source": source})
        loose = [b for b in loose if not _inside(b, table_box)]
    if min_height_ratio and page_rgb is not None:
        min_h = page_rgb.shape[0] * min_height_ratio
        loose = [b for b in loose if b[3] - b[1] >= min_h]
    groups = _group_boxes(loose, 0.6)
    return found + [
        {"type": "text", "label": "uncovered_text", "score": None,
         "bbox": [round(v, 1) for v in g], "source": source}
        for g in groups
    ]


# ---------------------------------------------------------------------
# 디지털 PDF(텍스트 레이어) 페이지: 레이아웃 모델이 놓치거나 잘못 묶은 글자 바로잡기
# ---------------------------------------------------------------------


def _x_columns(boxes: list[list[float]], min_gap: float) -> list[list[list[float]]]:
    """상자들을 가로 빈 틈(min_gap 이상)으로 나눈 단(column) 목록. 틈이 없으면 [boxes] 하나."""
    cols: list[list] = []  # [x1, x2, 상자들]
    for b in sorted(boxes, key=lambda b: b[0]):
        if cols and b[0] - cols[-1][1] < min_gap:
            cols[-1][1] = max(cols[-1][1], b[2])
            cols[-1][2].append(b)
        else:
            cols.append([b[0], b[2], [b]])
    return [c[2] for c in cols]


def _median_height(boxes: list[list[float]]) -> float:
    hs = sorted(b[3] - b[1] for b in boxes)
    return hs[len(hs) // 2] if hs else 0.0


def _overmerged_text_block(block: dict, line_boxes: list[list[float]], others: list[dict]) -> bool:
    """
    레이아웃 모델이 여러 덩어리를 한 text 블록으로 잘못 묶었는지. 이런 블록은 버리고 글자를 다시 묶는다.
      - 다른 블록(제목·목록)을 안에 품고 있다 → 같은 글이 두 번 나온다
        (실제 사례: 볼보 사양 PDF 4쪽, 페이지 전체를 덮은 블록 안에 "운반 용구 및 적재" 제목과 목록이 또 있었다)
      - 안의 글자가 가로 빈 틈으로 2단 이상 나뉜다 → 줄 단위로 읽으면 왼쪽·오른쪽 단 글자가 섞인다
        (실제 사례: 같은 PDF 5쪽 "이름 / 값" 3열 사양표가 "이름 3개 → 값 2개" 로 섞여 나왔다)
    """
    if block["type"] != "text":
        return False
    if any(o is not block and _mostly_inside(o["bbox"], block["bbox"], 0.8) for o in others):
        return True
    inside = [b for b in line_boxes if _inside(b, block["bbox"])]
    cols = _x_columns(inside, 2 * _median_height(inside))
    return len(cols) >= 2 and sum(1 for c in cols if len(c) >= 2) >= 2


def uncovered_pdf_blocks(line_boxes: list[list[float]], blocks: list[dict]) -> list[dict]:
    """
    디지털 PDF 페이지에서 어느 블록에도 들어가지 않은 글자 줄(파일에 저장된 글자)을 블록으로 만든다.
    (실제 사례: 볼보 사양 PDF 3쪽 "인테리어" 목록 11줄을 레이아웃 모델이 아무 블록으로도 잡지 않아 결과에서 통째로 빠졌다)
    이미지용 uncovered_text_blocks 와 같은 생각이지만, 글자 위치를 OCR 검출 대신 PDF 에서 정확히 얻으므로
    작은 글자를 버리지 않는다. 세로 틈이 큰 곳(소제목 사이 등)에서 먼저 나눈 뒤,
      1) 3열 이상으로 가지런한 부분은 표 블록 ("이름 / 값" 사양표)
      2) 나머지는 문단(text) 블록. 목록 항목 사이 간격(글자 높이 정도)까지는 한 문단으로 묶는다
    """
    loose = [b for b in line_boxes if not any(_inside(b, blk["bbox"]) for blk in blocks)]
    if not loose:
        return []
    h = _median_height(loose)
    loose.sort(key=lambda b: b[1])
    bands, bottom = [], None
    for b in loose:
        if bands and b[1] - bottom <= 4 * h:
            bands[-1].append(b)
            bottom = max(bottom, b[3])
        else:
            bands.append([b])
            bottom = b[3]
    found = []
    for band in bands:
        table_box = _table_from_boxes(band, min_rows=2, min_cols=3)
        if table_box is not None:
            found.append({"type": "table", "label": "uncovered_table", "score": None,
                          "bbox": [round(v, 1) for v in table_box], "source": "text-layer"})
            band = [b for b in band if not _inside(b, table_box)]
        found += [{"type": "text", "label": "uncovered_text", "score": None,
                   "bbox": [round(v, 1) for v in g], "source": "text-layer"} for g in _group_boxes(band, 1.0, h)]
    return found


def detect_page_blocks(engines, page_rgb: np.ndarray, page_info: dict, sweep_uncovered: bool = False,
                       sweep_min_height_ratio: float = 0.0, pdf_path: Path | None = None) -> list[dict]:
    """
    페이지 하나의 블록을 찾아 읽기 순서로 정렬해 돌려준다.
    블록을 하나도 못 찾으면 페이지 전체를 text 블록 하나로 둔다.
    (그림 위주 사진처럼 레이아웃 모델이 아무것도 못 잡아도, 글자가 있을 수 있으므로 통째로 OCR 해 보기 위해서.
     이 bbox 는 추측이 아니라 실제 처리할 영역(페이지 전체)이라서 [규칙 3]에 어긋나지 않는다)
    """
    h, w = page_rgb.shape[:2]
    blocks = []
    for d in engines.detect_layout(page_rgb):
        # 모델이 페이지 밖으로 살짝 벗어난 좌표를 줄 때가 있어 페이지 안으로 잘라 둔다
        x1, y1, x2, y2 = d["bbox"]
        x1, y1, x2, y2 = max(0.0, x1), max(0.0, y1), min(float(w), x2), min(float(h), y2)
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        blocks.append(
            {
                "type": label_to_type(d["label"]),
                "label": d["label"],
                "score": round(d["score"], 4),
                "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                "source": "text-layer" if page_info.get("text_layer") else "ocr",
            }
        )
    # 표 블록 안에 거의 다 들어간 다른 블록은 뺀다. 표 처리가 표 영역의 모든 글자를 표 칸·표 위·표 아래로 이미 쓰므로
    # 그대로 두면 같은 글이 두 번 나온다 (실제 사례: 표 영역에 화면 오른쪽 위 배너까지 들어가 배너가 두 번 출력됐다)
    tables = [b for b in blocks if b["type"] == "table"]
    blocks = [b for b in blocks if b["type"] == "table" or not any(_mostly_inside(b["bbox"], t["bbox"], 0.8) for t in tables)]
    if not blocks:
        blocks = [
            {
                "type": "text",
                "label": "full_page",
                "score": None,
                "bbox": [0.0, 0.0, float(w), float(h)],
                "source": "text-layer" if page_info.get("text_layer") else "ocr",
                "layout_fallback": True,
            }
        ]
    elif sweep_uncovered and not page_info.get("text_layer"):
        blocks += uncovered_text_blocks(engines, page_rgb, blocks, "ocr", sweep_min_height_ratio)
    elif sweep_uncovered and pdf_path is not None:
        from app.pipeline.preprocess import pdf_words_in_bbox

        line_boxes = [l["box"] for l in pdf_words_in_bbox(pdf_path, page_info["page"], [0, 0, w, h], page_info["scale"])
                      if l["text"].strip()]
        blocks = [b for b in blocks if not _overmerged_text_block(b, line_boxes, blocks)]
        blocks += uncovered_pdf_blocks(line_boxes, blocks)
    if page_info.get("text_layer") and pdf_path is not None:
        # 머리말은 맨 앞, 꼬리말은 맨 뒤에 따로 둔다. 페이지 폭 전체에 걸친 머리말·꼬리말이 단 사이 틈을 가려서
        # 2단 페이지가 단별로 나뉘지 않고 "왼쪽 제목, 오른쪽 제목, 왼쪽 목록…" 처럼 섞였다 (볼보 PDF 4쪽).
        # 이미지 쪽 결과가 바뀌지 않도록 지금은 디지털 PDF 페이지에만 쓴다
        top = [b for b in blocks if b["type"] == "header"]
        bottom = [b for b in blocks if b["type"] == "footer"]
        body = [b for b in blocks if b["type"] not in ("header", "footer")]
        return sort_reading_order(top) + sort_reading_order(body) + sort_reading_order(bottom)
    return sort_reading_order(blocks)


def run_layout(prepared: dict, storage: DocStorage, engines, doc_meta: dict, sweep_uncovered: bool = False,
               sweep_min_height_ratio: float = 0.0) -> dict:
    """
    받는 값: 전처리 결과, 저장소, 엔진, {"doc_id", "file_name", "language"}, 블록 밖 글자 다시 찾기 여부
    돌려주는 값: structure dict (structure.json 에도 저장됨)
    """
    if prepared["kind"] == "hwp":
        blocks = _hwp_structure(prepared["hwp_blocks"], storage)
    else:
        pages_blocks = []
        pdf_path = storage.original("pdf") if prepared["kind"] == "pdf" else None
        for page in prepared["pages"]:  # 모든 페이지를 먼저 분석한다 (내용 처리는 2단계에서)
            page_rgb = load_page_rgb(storage.page_image(page["page"]))
            pages_blocks.append((page["page"], detect_page_blocks(engines, page_rgb, page, sweep_uncovered, sweep_min_height_ratio,
                                                                  pdf_path if page.get("text_layer") else None)))
        blocks = assign_block_ids(pages_blocks)

    structure = {
        "doc_id": doc_meta["doc_id"],
        "file_name": doc_meta["file_name"],
        "language": doc_meta["language"],
        "kind": prepared["kind"],
        "pages": prepared["pages"],
        "blocks": blocks,
    }
    save_new_structure(storage, structure)
    return structure


def _hwp_structure(hwp_blocks: list[dict], storage: DocStorage) -> list[dict]:
    """
    HWP 파싱 결과에 번호를 붙인다. 파싱된 원문(글자·표)은 source/{block_id}.json 에 따로 둬서
    2단계가 PDF·이미지와 같은 방식으로 "블록 하나씩" 처리할 수 있게 한다.
    [규칙 3] HWP 는 좌표를 모르므로 bbox 는 null.
    """
    plain = [{"type": b["type"], "label": b.get("label"), "score": None, "bbox": None, "source": "hwp"} for b in hwp_blocks]
    blocks = assign_block_ids([(1, plain)])
    for block, parsed in zip(blocks, hwp_blocks):
        source = {k: v for k, v in parsed.items() if k not in ("image_bytes",)}
        if parsed.get("image_bytes"):
            name = f"{block['block_id']}.{parsed.get('image_ext') or 'bin'}"
            (storage.source_dir / name).write_bytes(parsed["image_bytes"])
            source["image_file"] = name
        write_json(storage.block_source(block["block_id"]), source)
    return blocks


def load_page_rgb(path: Path) -> np.ndarray:
    """페이지 PNG → RGB numpy 배열."""
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))
