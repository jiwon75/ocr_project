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


def detect_page_blocks(engines, page_rgb: np.ndarray, page_info: dict) -> list[dict]:
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
    return sort_reading_order(blocks)


def run_layout(prepared: dict, storage: DocStorage, engines, doc_meta: dict) -> dict:
    """
    받는 값: 전처리 결과, 저장소, 엔진, {"doc_id", "file_name", "language"}
    돌려주는 값: structure dict (structure.json 에도 저장됨)
    """
    if prepared["kind"] == "hwp":
        blocks = _hwp_structure(prepared["hwp_blocks"], storage)
    else:
        pages_blocks = []
        for page in prepared["pages"]:  # 모든 페이지를 먼저 분석한다 (내용 처리는 2단계에서)
            page_rgb = load_page_rgb(storage.page_image(page["page"]))
            pages_blocks.append((page["page"], detect_page_blocks(engines, page_rgb, page)))
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
