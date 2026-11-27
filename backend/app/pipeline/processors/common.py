"""
[이 파일이 하는 일]
2단계 블록 처리기(text/table/image)가 함께 쓰는 도구 모음.
  - ProcessContext : 처리에 필요한 것(설정, 엔진, 저장소, 페이지 이미지)을 한 상자에 담아 넘긴다
  - crop_block     : 페이지에서 블록 영역을 잘라 blocks/{block_id}.png 로 저장
  - read_lines     : 블록의 글자 줄을 얻는다 (OCR 또는 PDF 텍스트 레이어)
  - join_lines / aggregate_confidence / judge_text : 줄 합치기, 신뢰도 계산, "추출 실패" 판정 [규칙 2]

[왜 이렇게 나눴는지]
text 블록도, 표의 셀도, 이미지 속 글자도 "잘라서 → 줄 찾고 → 인식하고 → 판정"하는 과정은 같다.
같은 코드를 세 곳에 복사하면 한 곳만 고치고 나머지를 빠뜨리기 쉬워서 여기 한 번만 쓴다.
"""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from app.config import Settings
from app.pipeline.layout import load_page_rgb
from app.pipeline.preprocess import pdf_words_in_bbox
from app.pipeline.storage import DocStorage, read_json
from app.pipeline.structure import IDENTITY_FIELDS


@dataclass
class ProcessContext:
    """2단계 처리 함수들이 공통으로 받는 값 묶음. 인자를 7~8개씩 넘기는 대신 이것 하나만 넘긴다."""

    settings: Settings
    engines: object
    storage: DocStorage
    structure: dict
    pdf_path: Path | None = None  # 텍스트 레이어를 꺼낼 원본 PDF (PDF 일 때만)
    _page_cache: dict = field(default_factory=dict)

    @property
    def language(self) -> str:
        return self.structure.get("language", "auto")

    def page_info(self, page_no: int) -> dict:
        return next(p for p in self.structure["pages"] if p["page"] == page_no)

    def page_rgb(self, page_no: int) -> np.ndarray:
        """페이지 이미지를 한 번만 읽어서 재사용한다. 블록마다 같은 페이지 파일을 다시 읽으면 느리기 때문."""
        if page_no not in self._page_cache:
            self._page_cache[page_no] = load_page_rgb(self.storage.page_image(page_no))
        return self._page_cache[page_no]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def base_result(block: dict) -> dict:
    """
    블록 결과 JSON 의 뼈대.
    [규칙 1] block_id/order/page/type/bbox 는 structure 의 값을 "복사"만 한다. 새로 계산하지 않는다.
    """
    result = {f: block.get(f) for f in IDENTITY_FIELDS}
    result.update(
        {
            "source": block.get("source"),
            "status": "pending",
            "failure_reason": None,  # "empty" | "low_confidence" | "error" | "table_structure_failed" | "timeout"
            "failure_detail": None,
            "text": None,  # 판정을 통과한 최종 텍스트. 실패면 None
            "raw_text": None,  # [규칙 2] OCR 원본 결과. 실패해도 지우지 않고 남긴다
            "confidence": None,
            "lines": [],
            "edited_text": None,  # [규칙 2] 사용자가 화면에서 고친 내용 (원본과 따로 저장)
        }
    )
    return result


def failed_result(block: dict, reason: str, detail: str) -> dict:
    """처리 중 오류가 난 블록의 결과. [규칙 1] 블록을 지우지 않고 그 자리에 실패 상태로 남긴다."""
    result = base_result(block)
    result.update(status="failed", failure_reason=reason, failure_detail=detail, processed_at=now())
    return result


def crop_block(ctx: ProcessContext, block: dict) -> tuple[np.ndarray, tuple[float, float]]:
    """
    받는 값: 블록 (bbox 필수)
    돌려주는 값: (crop 이미지, crop 의 왼쪽 위 좌표(페이지 기준))
    여백(CROP_PADDING_PX)을 두고 자른다. 딱 맞게 자르면 글자 검출 모델이 가장자리 글자를 놓치기 때문(실제로 측정해서 확인).
    """
    page = ctx.page_rgb(block["page"])
    h, w = page.shape[:2]
    pad = ctx.settings.CROP_PADDING_PX
    x1, y1, x2, y2 = block["bbox"]
    x1, y1 = max(0, int(x1) - pad), max(0, int(y1) - pad)
    x2, y2 = min(w, int(round(x2)) + pad), min(h, int(round(y2)) + pad)
    crop = page[y1:y2, x1:x2]
    Image.fromarray(crop).save(ctx.storage.block_image(block["block_id"]))
    return crop, (float(x1), float(y1))


def ocr_lines(ctx: ProcessContext, crop: np.ndarray) -> list[dict]:
    """
    crop 이미지에서 글자 줄을 찾고 인식한다.
    돌려주는 값: [{"text", "confidence", "lang", "box"(crop 기준), "candidates"}] — 읽기 순서로 정렬됨
    """
    boxes = ctx.engines.detect_text(crop)
    if not boxes:
        return []
    h, w = crop.shape[:2]
    images, kept = [], []
    for x1, y1, x2, y2 in boxes:
        x1, y1, x2, y2 = max(0, int(x1)), max(0, int(y1)), min(w, int(round(x2))), min(h, int(round(y2)))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        images.append(crop[y1:y2, x1:x2])
        kept.append([x1, y1, x2, y2])
    recs = ctx.engines.recognize(images, ctx.language)
    lines = [{**rec, "box": box} for rec, box in zip(recs, kept)]
    return sort_lines(lines)


def read_lines(ctx: ProcessContext, block: dict, crop: np.ndarray, offset: tuple[float, float]) -> tuple[list[dict], str]:
    """
    블록의 글자 줄을 얻는다.
    - 디지털 PDF 페이지(텍스트 레이어 있음): 파일에 저장된 글자를 그대로 꺼낸다 (OCR 오인식이 없다)
      단, 그 영역에 저장된 글자가 하나도 없으면(예: PDF 안에 붙여 넣은 스캔 이미지) OCR 로 읽는다
    - 그 밖: OCR
    돌려주는 값: (줄 목록, 실제로 쓴 방법 "text-layer"|"ocr")  — 줄의 box 는 crop 기준 좌표
    """
    if block.get("source") == "text-layer" and ctx.pdf_path is not None:
        info = ctx.page_info(block["page"])
        lines = pdf_words_in_bbox(ctx.pdf_path, block["page"], block["bbox"], info["scale"])
        if any(l["text"].strip() for l in lines):
            ox, oy = offset
            for l in lines:
                x1, y1, x2, y2 = l["box"]
                l["box"] = [x1 - ox, y1 - oy, x2 - ox, y2 - oy]
            return lines, "text-layer"
    return ocr_lines(ctx, crop), "ocr"


def sort_lines(lines: list[dict]) -> list[dict]:
    """
    줄을 읽기 순서(위→아래, 같은 높이면 왼→오른쪽)로 정렬한다.
    검출 모델은 줄을 아래→위 같은 뒤죽박죽 순서로 줄 때가 있어서(실제로 측정함) 직접 정렬해야 한다.
    각 줄에 "row"(몇 번째 행인지)를 붙여, 같은 행의 조각은 나중에 공백으로 이어 붙일 수 있게 한다.
    """
    items = sorted(lines, key=lambda l: (l["box"][1] + l["box"][3]) / 2)
    rows: list[list[dict]] = []
    for line in items:
        y1, y2 = line["box"][1], line["box"][3]
        if rows:
            ry1 = min(l["box"][1] for l in rows[-1])
            ry2 = max(l["box"][3] for l in rows[-1])
            overlap = min(y2, ry2) - max(y1, ry1)
            # 세로로 절반 이상 겹치면 같은 행으로 본다
            if overlap > 0.5 * min(y2 - y1, ry2 - ry1):
                rows[-1].append(line)
                continue
        rows.append([line])
    out = []
    for r, row in enumerate(rows):
        for line in sorted(row, key=lambda l: l["box"][0]):
            out.append({**line, "row": r})
    return out


def join_lines(lines: list[dict]) -> str:
    """정렬된 줄 → 문자열. 같은 행은 공백, 행이 바뀌면 줄바꿈. 원래 줄바꿈 위치를 그대로 지킨다(임의로 문단을 합치지 않음)."""
    rows: dict[int, list[str]] = {}
    for i, line in enumerate(lines):
        rows.setdefault(line.get("row", i), []).append(line["text"])
    return "\n".join(" ".join(t for t in parts if t) for _, parts in sorted(rows.items())).strip()


def aggregate_confidence(lines: list[dict], mode: str) -> float | None:
    """
    여러 줄의 confidence 를 블록 하나의 점수로 합친다.
    - 텍스트 레이어·HWP 처럼 confidence 가 없는 줄(None)만 있으면 None
    - mean: 글자 수로 가중 평균. 글자 1~2개짜리 잡음 줄이 긴 문장과 똑같은 무게를 갖지 않게 하기 위해서다
    - min : 가장 낮은 줄의 점수. 한 줄이라도 의심스러우면 블록 전체를 실패로 보고 싶을 때 (.env 에서 선택)
    """
    scored = [l for l in lines if l.get("confidence") is not None]
    if not scored:
        return None
    if mode == "min":
        nonempty = [l for l in scored if l["text"].strip()] or scored
        return round(min(l["confidence"] for l in nonempty), 4)
    total = sum(len(l["text"]) for l in scored)
    if total == 0:
        return 0.0
    return round(sum(l["confidence"] * len(l["text"]) for l in scored) / total, 4)


def judge_text(text: str | None, confidence: float | None, threshold: float) -> tuple[bool, str | None]:
    """
    [규칙 2] "추출 실패" 판정. 내용을 고치지 않고 통과/실패만 정한다.
    - 결과가 비어 있으면 → 실패("empty")
    - confidence 가 기준값보다 낮으면 → 실패("low_confidence")
    - confidence 가 없는 경우(텍스트 레이어·HWP)는 비어 있는지만 본다
    돌려주는 값: (통과 여부, 실패 이유)
    """
    if not text or not text.strip():
        return False, "empty"
    if confidence is not None and confidence < threshold:
        return False, "low_confidence"
    return True, None


def read_source(ctx: ProcessContext, block: dict) -> dict:
    """HWP 파싱 원문(source/{block_id}.json)을 읽는다."""
    return read_json(ctx.storage.block_source(block["block_id"])) or {}
