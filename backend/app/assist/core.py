"""
[이 파일이 하는 일]
LLM 도우미의 실제 내용: (1) 문서 요약 (2) OCR 오타 교정 "제안" 만들기 (3) 사용자가 고른 제안 적용.

[왜 이렇게 나눴는지]
- LLM 을 부르는 함수(chat)를 인자로 받는다. 그래서 테스트에서는 가짜 chat 을 넣어 Ollama 없이도 검사할 수 있다.
- [규칙 2] LLM 은 결과를 직접 고치지 않는다. 제안만 만들고, 사용자가 수락한 것만 edited_text 칸에 저장한다.
  원본(raw_text, text)은 그대로 남는다.
- LLM 은 가끔 그럴듯하지만 틀린 답을 한다. 그래서 받은 제안을 코드로 한 번 더 걸러 낸다 (validate_suggestion):
    · 틀린 부분이 원문에 실제로 있어야 하고
    · 고친 글자가 (한글 자모·비슷한 모양 글자 기준으로) 원래 글자와 충분히 닮아야 하며
    · 숫자가 들어 있으면 자릿수와 쉼표·점 위치가 그대로여야 한다 (1DD → 100 은 되고, 100 → 1000 은 안 된다)

용어
- 조각(unit): 교정 대상 하나. 글자 블록 하나("p1-b003") 또는 표 칸 하나("p1-b004:r2c3", r/c 는 1부터).
- 청크(chunk): 긴 문서를 LLM 에 나눠 보내는 단위. LLM_CHUNK_CHARS 글자 정도씩.
"""
import difflib
import json
import re

from app.pipeline.merge import merge_document

TEXT_TYPES = ("title", "text", "caption", "header", "footer")
CELL_ID_RE = re.compile(r"^(p\d+-b\d{3,}):r(\d+)c(\d+)$")
MARKER_RE = re.compile(r"\[(추출 실패|이미지): [^\]]*\]")
MAX_WRONG_CHARS = 40  # 한 제안이 바꿀 수 있는 최대 글자 수. 문장을 통째로 바꾸는 "다듬기"를 막는다


# ---------------------------------------------------------------------
# 나누기 (청크)
# ---------------------------------------------------------------------


def chunk_texts(pieces: list[str], max_chars: int) -> list[list[str]]:
    """조각들을 순서대로 묶되, 한 묶음이 max_chars 를 넘지 않게 한다. 조각 하나가 더 길면 그 조각만 따로 한 묶음."""
    chunks, current, size = [], [], 0
    for piece in pieces:
        if current and size + len(piece) > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------

SUMMARY_SYSTEM = """너는 문서 요약 도우미다. OCR(글자 인식)로 읽은 문서를 한국어로 요약한다.
- 3~5문장으로 핵심만 쓴다. 표가 있으면 무엇에 대한 표인지와 눈에 띄는 값만 말한다
- 문서에 없는 내용은 지어내지 않는다. OCR 오타로 보이는 글자는 문맥에 맞게 읽되, 확실하지 않은 숫자는 쓰지 않는다
- 요약 문장만 쓴다 (제목, 머리말, "요약:" 같은 말 없이)"""

SUMMARY_PART_SYSTEM = """너는 문서 요약 도우미다. 긴 문서의 한 부분을 받는다. 이 부분의 핵심 내용을 한국어 2~4문장으로 정리한다.
- 문서에 없는 내용은 지어내지 않는다. 요약 문장만 쓴다"""

SUMMARY_COMBINE_SYSTEM = """너는 문서 요약 도우미다. 긴 문서를 부분별로 요약한 글들을 받는다.
이것을 문서 전체에 대한 한국어 요약 3~5문장으로 합친다. 겹치는 내용은 한 번만 쓰고, 없는 내용은 지어내지 않는다. 요약 문장만 쓴다"""


def document_text(structure: dict, results: dict) -> str:
    """요약할 문서 글자. 화면/다운로드와 같은 합치기 결과에서 [추출 실패]·[이미지] 표시만 뺀다."""
    text = merge_document(structure, results, "txt", page_separator=False, skip_page_furniture=True)
    text = MARKER_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def summarize(chat, text: str, max_chars: int) -> dict:
    """
    문서 요약. 짧으면 한 번에, 길면 나눠서 부분 요약 → 합치기 (map-reduce 방식).
    돌려주는 값: {"summary", "chunks"(나눈 개수)}
    """
    if not text.strip():
        return {"summary": "", "chunks": 0}
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks = ["\n\n".join(c) for c in chunk_texts(paragraphs, max_chars)]
    if len(chunks) == 1:
        return {"summary": chat(SUMMARY_SYSTEM, chunks[0]).strip(), "chunks": 1}
    parts = [chat(SUMMARY_PART_SYSTEM, f"[문서 {i}/{len(chunks)} 부분]\n{c}").strip() for i, c in enumerate(chunks, 1)]
    # 부분 요약을 합친 것도 길면 한 번 더 나눠 합친다 (아주 긴 문서). 보통은 한 번에 끝난다
    while len("\n\n".join(parts)) > max_chars and len(parts) > 1:
        parts = [chat(SUMMARY_COMBINE_SYSTEM, "\n\n".join(g)).strip() for g in chunk_texts(parts, max_chars)]
    summary = chat(SUMMARY_COMBINE_SYSTEM, "\n\n".join(f"- {p}" for p in parts)).strip() if len(parts) > 1 else parts[0]
    return {"summary": summary, "chunks": len(chunks)}


# ---------------------------------------------------------------------
# 교정 대상 조각
# ---------------------------------------------------------------------


def _cell_base(cell: dict) -> str | None:
    """표 칸의 현재 글자: 이미 고친 것 → 통과한 글자 → 신뢰도 미달이라 (?) 로 보이는 글자."""
    for key in ("edited_text", "text", "uncertain_text"):
        if cell.get(key):
            return cell[key]
    return None


def _block_base(result: dict) -> str | None:
    """글자 블록의 현재 글자: 이미 고친 것 → 통과한 글자 → 신뢰도 미달로 실패한 원본(raw_text)."""
    if result.get("edited_text") is not None:
        return result["edited_text"]
    if result.get("status") == "done":
        return result.get("text")
    if result.get("failure_reason") == "low_confidence":
        return result.get("raw_text")
    return None


def collect_units(structure: dict, results: dict) -> list[dict]:
    """교정 대상 조각 목록 [{"id", "block_id", "text"}] (order 순서). 이미지·차트·통째로 고친 표는 뺀다."""
    units = []
    for block in sorted(structure["blocks"], key=lambda b: b["order"]):
        result = results.get(block["block_id"])
        if not result:
            continue
        if block["type"] in TEXT_TYPES:
            base = _block_base(result)
            if base and base.strip():
                units.append({"id": block["block_id"], "block_id": block["block_id"], "text": base})
        elif block["type"] == "table" and result.get("status") == "done" and result.get("edited_text") is None:
            for cell in (result.get("table") or {}).get("cells", []):
                base = _cell_base(cell)
                if base and base.strip():
                    cid = f"{block['block_id']}:r{cell['row'] + 1}c{cell['col'] + 1}"
                    units.append({"id": cid, "block_id": block["block_id"], "text": base})
    return units


# ---------------------------------------------------------------------
# 제안 검사
# ---------------------------------------------------------------------

# 숫자 자리에서 자주 헷갈리는 영문자 → 숫자. 닮은 정도를 잴 때만 쓴다 (실제 글자를 바꾸지는 않는다)
LOOKALIKE = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                           "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6", "b": "6", "q": "9", "g": "9"})


def _jamo(text: str) -> str:
    """한글 음절을 자모(초성·중성·종성)로 푼다. 짧뽕/짬뽕 처럼 받침 하나 다른 글자가 '많이 닮음'으로 계산되게 하기 위해서."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(chr(0x1100 + code // 588))
            out.append(chr(0x1161 + (code % 588) // 28))
            if code % 28:
                out.append(chr(0x11A7 + code % 28))
        else:
            out.append(ch)
    return "".join(out)


def similarity(a: str, b: str) -> float:
    """두 글자가 얼마나 닮았는지 0~1. 자모로 풀고 비슷한 모양 글자를 맞춘 뒤 비교한다."""
    a, b = _jamo(a.translate(LOOKALIKE)), _jamo(b.translate(LOOKALIKE))
    return difflib.SequenceMatcher(None, a, b).ratio()


def _number_shape(text: str) -> str:
    """숫자 모양: 숫자·숫자로 헷갈리는 글자는 #, 쉼표·점은 그대로, 나머지는 뺀다. 예) "1DD,5OO원" → "###,###"."""
    shape = []
    for c in text:
        if c.isdigit() or (c.isascii() and c.translate(LOOKALIKE).isdigit()):
            shape.append("#")
        elif c in ",.":
            shape.append(c)
    return "".join(shape)


def validate_suggestion(item: dict, units: dict[str, dict], min_similarity: float) -> str | None:
    """제안이 받아들일 만한지. 괜찮으면 None, 아니면 버리는 이유(로그용)."""
    uid, wrong, right = item.get("id"), item.get("wrong"), item.get("right")
    if uid not in units:
        return "없는 조각 id"
    if not isinstance(wrong, str) or not isinstance(right, str) or not wrong.strip() or wrong == right:
        return "형식 오류 또는 바뀐 것 없음"
    if wrong not in units[uid]["text"]:
        return "원문에 없는 글자"
    if len(wrong) > MAX_WRONG_CHARS:
        return "너무 긴 변경"
    if similarity(wrong, right) < min_similarity:
        return "원래 글자와 너무 다름"
    if any(c.isdigit() for c in wrong + right) and _number_shape(wrong) != _number_shape(right):
        return "숫자 자릿수·구분 기호가 바뀜"
    return None


# ---------------------------------------------------------------------
# 교정 제안 만들기
# ---------------------------------------------------------------------

CORRECTION_SYSTEM = """너는 OCR(글자 인식) 결과 교정기다. 글자 모양이 비슷해서 잘못 읽힌 것이 확실한 부분만 찾는다.
- 한글 오인식: 받침이나 모음이 비슷한 다른 글자로 읽혀 없는 단어가 된 경우 (예: '찌게'→'찌개', '볶'이 '뽁'으로 읽힌 경우)
- 숫자 오인식: 숫자 사이에 섞인 O, o, D 는 0, l, I 는 1, S 는 5, B 는 8 (예: '5OO'→'500', 'l2'→'12')
- 숫자의 자릿수와 쉼표·소수점 위치는 절대 바꾸지 않는다
- 띄어쓰기, 맞춤법 다듬기, 문체, 표현은 고치지 않는다. 고유명사·영어 단어도 확실하지 않으면 그대로 둔다
- 확실하지 않으면 제안하지 않는다. 예시의 단어를 그대로 쓰지 말고, 받은 글자 안에서만 찾는다
입력은 한 줄에 하나씩 {"id": ..., "text": ...} 이다.
JSON 으로만 답한다: {"corrections":[{"id":"조각 id","wrong":"text 에 있는 그대로의 틀린 부분","right":"고친 글자","reason":"짧은 이유"}]}
고칠 것이 없으면 {"corrections":[]}"""


# 숫자로 시작하고, 숫자로 헷갈리는 영문자가 섞인 덩어리. 앞뒤에 다른 영문자·숫자가 붙어 있으면(영어 단어 등) 뺀다
NUMBER_RUN_RE = re.compile(r"(?<![A-Za-z0-9])\d[0-9ODQoIlSB]*(?![A-Za-z0-9])")
DIGIT_LOOKALIKE = str.maketrans({"O": "0", "D": "0", "Q": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8"})


def rule_suggestions(units: list[dict]) -> list[dict]:
    """
    LLM 없이 규칙으로 찾는 숫자 오인식. 예) "나스닥1DD" → "나스닥100", "5OO원" → "500원".
    작은 LLM 은 이런 것을 자주 놓쳐서(이 PC 의 gemma4 로 시험) 규칙으로 보탠다.
    "3D", "10G" 처럼 숫자 뒤 영문자 하나는 단위·이름일 수 있어 건드리지 않고,
    영문자가 2개 이상이거나 숫자 사이에 끼어 있을 때(1O5)만 제안한다. 제안일 뿐이라 사용자가 고르지 않으면 바뀌지 않는다.
    """
    out = []
    for unit in units:
        for m in NUMBER_RUN_RE.finditer(unit["text"]):
            run = m.group(0)
            letters = [i for i, c in enumerate(run) if not c.isdigit()]
            if not letters:
                continue
            sandwiched = any(0 < i < len(run) - 1 and run[i - 1].isdigit() and run[i + 1].isdigit() for i in letters)
            if len(letters) >= 2 or sandwiched:
                out.append({"id": unit["id"], "wrong": run, "right": run.translate(DIGIT_LOOKALIKE),
                            "reason": "숫자 사이에 숫자와 닮은 영문자가 섞여 있음 (규칙)"})
    return out


def _has_letters(text: str) -> bool:
    """글자(한글·영문)가 하나라도 있는지. 숫자·기호뿐인 칸은 LLM 이 고칠 것이 없어 보내지 않는다 (속도·잡음 줄이기)."""
    return bool(re.search(r"[^\W\d_]", text))


def _parse_json(answer: str) -> list[dict]:
    """LLM 답에서 corrections 목록을 꺼낸다. 형식이 깨져 있으면 빈 목록."""
    match = re.search(r"\{.*\}", answer, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("corrections") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def suggest_corrections(chat, units: list[dict], max_chars: int, min_similarity: float) -> dict:
    """
    조각들을 청크로 나눠 LLM 에 보내고, 검사를 통과한 제안만 돌려준다.
    돌려주는 값: {"suggestions": [{"sid", "id", "block_id", "wrong", "right", "reason", "before", "after", "applied"}],
                 "rejected": 버린 제안 수, "chunks": 청크 수}
    """
    by_id = {u["id"]: u for u in units}
    lines = [json.dumps({"id": u["id"], "text": u["text"]}, ensure_ascii=False) for u in units if _has_letters(u["text"])]
    chunks = chunk_texts(lines, max_chars)

    def candidates():
        yield from rule_suggestions(units)  # 규칙 제안이 먼저. 같은 곳에 대한 LLM 제안은 아래에서 버린다
        for chunk in chunks:
            yield from _parse_json(chat(CORRECTION_SYSTEM, "\n".join(chunk), json_mode=True))

    suggestions, seen, rejected = [], set(), 0
    for item in candidates():
        key = (str(item.get("id")), str(item.get("wrong")))  # str: LLM 이 목록 같은 엉뚱한 값을 줘도 오류 없이 비교
        # 고칠 것 없다는 뜻으로 같은 글자를 돌려주는 모델이 있다. 이것은 "버린 제안" 수에 세지 않는다
        if key in seen or item.get("wrong") == item.get("right"):
            continue
        if validate_suggestion(item, by_id, min_similarity):
            rejected += 1
            continue
        seen.add(key)  # 통과한 것만 기억한다. 버린 제안 때문에 같은 곳의 올바른 제안까지 빠지면 안 되므로
        unit = by_id[item["id"]]
        suggestions.append({
            "sid": len(suggestions) + 1,
            "id": item["id"],
            "block_id": unit["block_id"],
            "wrong": item["wrong"],
            "right": item["right"],
            "reason": str(item.get("reason") or "")[:200],
            "before": unit["text"],
            "after": unit["text"].replace(item["wrong"], item["right"]),
            "applied": False,
        })
    return {"suggestions": suggestions, "rejected": rejected, "chunks": len(chunks)}


# ---------------------------------------------------------------------
# 제안 적용 (사용자가 수락한 것만)
# ---------------------------------------------------------------------


def apply_to_result(result: dict, unit_id: str, wrong: str, right: str) -> bool:
    """
    블록 결과 하나에 제안 하나를 적용한다 (결과 dict 를 직접 바꾼다).
    [규칙 2] 글자 블록은 edited_text, 표 칸은 칸의 edited_text 에만 쓴다. text/raw_text 는 그대로.
    지금 글자에 틀린 부분이 더 이상 없으면(그새 사용자가 고쳤거나 재시도함) 적용하지 않고 False.
    """
    m = CELL_ID_RE.match(unit_id)
    if m:
        row, col = int(m.group(2)) - 1, int(m.group(3)) - 1
        cell = next((c for c in (result.get("table") or {}).get("cells", []) if c["row"] == row and c["col"] == col), None)
        base = _cell_base(cell) if cell else None
        if not base or wrong not in base or result.get("edited_text") is not None:
            return False
        cell["edited_text"] = base.replace(wrong, right)
        return True
    base = _block_base(result)
    if not base or wrong not in base:
        return False
    result["edited_text"] = base.replace(wrong, right)
    return True
