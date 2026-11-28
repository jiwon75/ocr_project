"""
[청킹 — 이 파일이 하는 일]
3단계(합치기)에서 만든 최종 텍스트를 검색·질문 답변(RAG)에 쓰기 좋은 크기의 조각(청크)으로 나눈다.

  규칙
  1. 블록을 order 순서대로 담다가 CHUNK_SIZE_CHARS 를 넘으면 새 청크를 시작한다 (블록·문장 중간에서 끊지 않는다)
  2. 제목 블록은 바로 다음 블록과 붙인다 (제목만 따로 있는 청크는 검색에 쓸모가 없다)
  3. 표는 따로 청크로 만든다 (표가 본문과 섞이거나 중간에 잘리면 의미가 흐려진다). 너무 긴 표만 줄 단위로 나눈다
  4. 블록 하나가 너무 길면 문장(없으면 줄·글자) 단위로 나눈다
  5. 본문 청크는 앞 청크의 끝부분(CHUNK_OVERLAP_CHARS 글자 정도)을 앞에 겹쳐 넣어, 경계에서 문맥이 끊기지 않게 한다
  6. 각 청크에는 페이지 범위와 들어 있는 block_id 를 붙인다 → 원문 위치를 찾아갈 수 있다

[왜 이렇게 나눴는지]
- 블록 글자는 merge.render_block 을 그대로 쓴다. 그래서 사용자 수정(edited_text), [추출 실패: ...] 표시([규칙 2]),
  머리말·꼬리말 제외까지 .txt 결과와 똑같이 적용된다. 청크에만 다른 글자가 들어가는 일이 없다.
- 모델이 필요 없는 글자 나누기라서, 블록을 고치면 API 서버에서 바로 다시 만들 수 있다 (runner.remerge).
- app/assist/core.py 의 chunk_texts 는 LLM 에 "잠깐 나눠 보내는" 용도이고, 이 파일은 결과로 "저장하는" 청크다.

용어
- 청크(chunk): 긴 글을 나눈 한 조각.
- 오버랩(overlap): 앞 청크의 끝부분을 다음 청크 앞에 다시 넣은 부분. 문장이 청크 경계에 걸쳐도 양쪽에서 찾을 수 있게 한다.
"""
import re
from dataclasses import dataclass, field

from app.pipeline.merge import is_dropped, render_block

SENTENCE_END_RE = re.compile(r"(?<=[.!?。…])\s+|\n+")  # 문장 끝(마침표 등 + 공백) 또는 줄바꿈


@dataclass
class Unit:
    """청크에 담을 글자 한 덩어리 (블록 하나, 또는 긴 블록을 나눈 한 조각)."""

    text: str
    kind: str  # "text" | "table"
    pages: list[int] = field(default_factory=list)
    block_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# 블록 → 덩어리
# ---------------------------------------------------------------------


def build_units(structure: dict, results: dict[str, dict], skip_page_furniture: bool) -> list[Unit]:
    """
    블록을 order 순서로 글자 덩어리로 바꾼다. 제목은 다음 덩어리 앞에 붙인다.
    그림만 있는 블록([이미지: p1-b006])은 검색할 글자가 없어서 뺀다. 차트 값 표가 있는 그림은 표로 넣는다.
    """
    units: list[Unit] = []
    title: Unit | None = None  # 다음 블록에 붙이려고 기다리는 제목
    for block in sorted(structure["blocks"], key=lambda b: b["order"]):  # [규칙 1] order 순서
        result = results.get(block["block_id"])
        if is_dropped(block, result, skip_page_furniture):
            continue
        btype = block["type"]
        if btype == "image" and not (result or {}).get("chart_table") and (result or {}).get("edited_text") is None:
            continue
        text = render_block(block, result, "txt").strip()
        if not text:
            continue
        unit = Unit(text, _kind(btype, result), [block["page"]], [block["block_id"]])
        if btype == "title":
            if title:  # 제목이 연달아 나오면 하나로 묶는다 (큰 제목 + 작은 제목)
                title = _join(title, unit, "\n")
            else:
                title = unit
            continue
        if title:
            unit = _join(title, unit, "\n")
            title = None
        units.append(unit)
    if title:  # 문서 마지막이 제목이면 그대로 넣는다
        units.append(title)
    return units


def _kind(btype: str, result: dict | None) -> str:
    """
    덩어리 종류. 표·차트는 "table", 나머지는 "text".
    단 열이 1개뿐인 표는 "text" 로 본다. HWP 는 본문을 칸 하나짜리 표(글상자)에 넣는 경우가 많아서
    (실제 사례: 1×1 표 안의 518자 본문, 10×1 표 안의 문항별 답안), 표로 다루면 긴 글에 오버랩이 빠지고 줄 단위로 잘린다.
    """
    if btype == "image":
        return "table"  # 차트 값 표 (그림만 있는 블록은 build_units 에서 이미 뺐다)
    if btype != "table":
        return "text"
    table = (result or {}).get("table") or {}
    return "text" if table.get("n_cols") == 1 and result.get("edited_text") is None else "table"


def _join(a: Unit, b: Unit, sep: str) -> Unit:
    """두 덩어리를 붙인다. 종류는 뒤쪽(본문/표)을 따른다."""
    return Unit(a.text + sep + b.text, b.kind, _unique(a.pages + b.pages), _unique(a.block_ids + b.block_ids))


def _unique(items: list) -> list:
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------------
# 긴 덩어리 나누기
# ---------------------------------------------------------------------


def split_long(unit: Unit, max_chars: int) -> list[Unit]:
    """
    max_chars 보다 긴 덩어리를 나눈다. 본문은 문장 → 글자 순, 표는 줄(행) → 글자 순으로 자른다.
    나뉜 조각은 모두 원래 블록의 페이지·block_id 를 그대로 가진다.
    """
    if len(unit.text) <= max_chars:
        return [unit]
    if unit.kind == "table":
        pieces = unit.text.split("\n")
        sep = "\n"
    else:
        pieces = [p for p in SENTENCE_END_RE.split(unit.text) if p and p.strip()]
        sep = " "
    parts: list[str] = []
    current = ""
    for piece in pieces:
        piece = piece.strip() if unit.kind == "text" else piece
        for small in _hard_cut(piece, max_chars):
            if current and len(current) + len(sep) + len(small) > max_chars:
                parts.append(current)
                current = small
            else:
                current = current + sep + small if current else small
    if current:
        parts.append(current)
    return [Unit(p, unit.kind, list(unit.pages), list(unit.block_ids)) for p in parts]


def _hard_cut(text: str, max_chars: int) -> list[str]:
    """문장 하나가 max_chars 보다 길면 어쩔 수 없이 글자 수로 자른다 (가능하면 공백에서)."""
    out = []
    while len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def overlap_tail(text: str, overlap_chars: int) -> str:
    """
    앞 청크의 끝부분 overlap_chars 글자 정도를 돌려준다.
    단어 중간에서 시작하지 않도록, 잘린 지점 뒤 첫 공백·줄바꿈부터 쓴다 (문장 시작이 가까이 있으면 거기부터).
    """
    if overlap_chars <= 0 or not text:
        return ""
    if len(text) <= overlap_chars:
        return text
    tail = text[-overlap_chars:]
    sentence = SENTENCE_END_RE.search(tail)
    if sentence and sentence.end() < len(tail):
        return tail[sentence.end():].strip()
    space = re.search(r"\s", tail)
    if space and space.end() < len(tail):
        return tail[space.end():].strip()
    return tail.strip()


# ---------------------------------------------------------------------
# 청크 만들기
# ---------------------------------------------------------------------


def build_chunks(units: list[Unit], size: int, overlap: int) -> list[dict]:
    """
    덩어리들을 size 글자 이하의 청크로 묶는다.
    돌려주는 값: [{"chunk_index"(1부터), "kind", "text", "char_count", "overlap_chars", "page_start", "page_end", "block_ids"}]
    overlap_chars 는 text 맨 앞에서 앞 청크와 겹치는 글자 수 (text[:overlap_chars] 가 앞 청크에서 가져온 부분).
    """
    if size <= 0:
        raise ValueError("CHUNK_SIZE_CHARS 는 1 이상이어야 합니다.")
    if not 0 <= overlap < size:
        raise ValueError("CHUNK_OVERLAP_CHARS 는 0 이상, CHUNK_SIZE_CHARS 보다 작아야 합니다.")

    chunks: list[dict] = []
    current: list[Unit] = []
    head = ""  # 이번 청크 앞에 겹쳐 넣을 앞 청크의 끝부분
    sep = "\n\n"

    def body_len() -> int:
        return len(sep.join(u.text for u in current))

    def flush() -> None:
        nonlocal current, head
        if not current:
            return
        body = sep.join(u.text for u in current)
        text = head + sep + body if head else body
        chunks.append({
            "chunk_index": len(chunks) + 1,
            "kind": "text",
            "text": text,
            "char_count": len(text),
            "overlap_chars": len(head),
            "page_start": min(p for u in current for p in u.pages),
            "page_end": max(p for u in current for p in u.pages),
            "block_ids": _unique([b for u in current for b in u.block_ids]),
        })
        head = overlap_tail(body, overlap)
        current = []

    # 오버랩을 붙여도 size 를 넘지 않도록 본문 조각은 (size - overlap - 구분자) 이하로 나눈다
    text_max = max(1, size - overlap - len(sep))
    for unit in units:
        if unit.kind == "table":
            # 표는 앞 본문과 섞지 않고, 오버랩도 넣지 않는다. 표 다음 본문은 오버랩 없이 새로 시작한다
            flush()
            head = ""
            for part in split_long(unit, size):
                chunks.append({
                    "chunk_index": len(chunks) + 1,
                    "kind": "table",
                    "text": part.text,
                    "char_count": len(part.text),
                    "overlap_chars": 0,
                    "page_start": min(part.pages),
                    "page_end": max(part.pages),
                    "block_ids": list(part.block_ids),
                })
            continue
        for piece in split_long(unit, text_max):
            extra = len(head) + len(sep) if head else 0
            if current and extra + body_len() + len(sep) + len(piece.text) > size:
                flush()
            current.append(piece)
    flush()
    return chunks


def chunk_document(structure: dict, results: dict[str, dict], settings) -> list[dict]:
    """문서 하나의 청크 목록. 설정값은 .env 의 CHUNK_SIZE_CHARS / CHUNK_OVERLAP_CHARS ([규칙 4])."""
    units = build_units(structure, results, settings.MERGE_SKIP_PAGE_FURNITURE)
    return build_chunks(units, settings.CHUNK_SIZE_CHARS, settings.CHUNK_OVERLAP_CHARS)


def write_chunks(storage, settings) -> list[dict]:
    """3단계에서 부른다: 청크를 만들어 output/chunks.json 에 저장하고 돌려준다 (DB 저장은 runner 의 db 단계)."""
    from app.pipeline.merge import load_results
    from app.pipeline.storage import read_json, write_json

    structure = read_json(storage.structure_json)
    chunks = chunk_document(structure, load_results(storage, structure), settings)
    write_json(storage.chunks_json, {
        "chunk_size": settings.CHUNK_SIZE_CHARS,
        "chunk_overlap": settings.CHUNK_OVERLAP_CHARS,
        "count": len(chunks),
        "chunks": chunks,
    })
    return chunks
