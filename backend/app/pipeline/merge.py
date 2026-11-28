"""
[3단계 — 이 파일이 하는 일]
structure.json 의 order 순서대로 블록 결과를 이어 붙여 최종 결과를 만든다.
  - .txt : 순수 텍스트. 표는 탭(\t)으로 칸을 나눈다
  - .md  : 제목은 #, 표는 Markdown 표(병합 셀은 첫 칸에 글자, 나머지는 빈칸), 이미지는 자리표시자+링크
  - 옵션 : 페이지 구분선(--- page 2 ---), 디버그 모드([블록 p1-b003] 표시)
그리고 합친 .txt 를 DB extracted_texts 테이블에 저장한다.

[왜 이렇게 나눴는지]
- 합치기는 모델이 필요 없는 "글자 이어 붙이기"라서, 사용자가 블록을 고치면 API 서버에서 바로 다시 합칠 수 있다.
- [규칙 1] 순서는 오직 structure 의 order 로 정한다. 결과 파일이 만들어진 순서(처리 순서·retry 순서)는 보지 않는다.
- [규칙 2] 실패한 블록은 내용을 짐작해 채우지 않고 [추출 실패: p1-b003] 로 표시한다. 블록을 빼먹지도 않는다.
"""
from app import db
from app.pipeline.storage import DocStorage, read_json


def failure_marker(block_id: str, cell: str | None = None) -> str:
    """[규칙 2] 실패 표시. 블록 번호를 함께 적어 어느 블록인지 바로 찾을 수 있게 한다."""
    return f"[추출 실패: {block_id} {cell}]" if cell else f"[추출 실패: {block_id}]"


def debug_marker(block_id: str) -> str:
    """디버그 모드에서 블록마다 앞에 붙이는 표시."""
    return f"[블록 {block_id}]"


def image_marker(block_id: str) -> str:
    return f"[이미지: {block_id}]"


# ---------------------------------------------------------------------
# 표 그리기
# ---------------------------------------------------------------------


def _cell_text(block_id: str, cell: dict) -> str:
    """
    셀 하나의 표시 글자. 실패한 셀은 [추출 실패: p1-b003 r2c3] (r/c 는 사람이 보기 쉽게 1부터).
    단 신뢰도만 기준 미달이고 TABLE_SHOW_UNCERTAIN_TEXT 가 켜져 있으면 읽은 글자 뒤에 (?) 를 붙인다 (예: "짧뽕 (?)").
    """
    if cell.get("edited_text") is not None:
        # 사용자가 칸 하나를 고친 것(LLM 교정 제안을 수락한 경우 등). [규칙 2] 원본 text/raw_text 는 그대로 남아 있다
        return cell["edited_text"]
    if cell.get("status") == "failed" and cell.get("uncertain_text"):
        return f"{cell['uncertain_text']} (?)"
    if cell.get("status") == "failed":
        return failure_marker(block_id, f"r{cell['row'] + 1}c{cell['col'] + 1}")
    return cell.get("text") or ""


def _grid(table: dict):
    """셀 목록 → 2차원 표. 병합으로 가려진 칸은 "covered" 로 표시한다."""
    n_rows, n_cols = table["n_rows"], table["n_cols"]
    grid = [[None] * n_cols for _ in range(n_rows)]
    for cell in table["cells"]:
        r, c = cell["row"], cell["col"]
        if r >= n_rows or c >= n_cols:
            continue
        grid[r][c] = cell
        for dr in range(cell.get("rowspan", 1)):
            for dc in range(cell.get("colspan", 1)):
                if (dr or dc) and r + dr < n_rows and c + dc < n_cols:
                    grid[r + dr][c + dc] = "covered"
    return grid


def _piece_text(block_id: str, piece: dict, where: str) -> str:
    """표 제목·표 밖 글자 한 조각. [규칙 2] 기준 미달이면 [추출 실패: p1-b003 표 밖] 으로 표시한다."""
    if piece.get("status") == "failed":
        return failure_marker(block_id, where)
    return (piece.get("text") or "").replace("\n", " ")


def table_to_tsv(block_id: str, table: dict) -> str:
    """표 → 탭 구분 텍스트. 병합으로 가려진 칸은 빈칸으로 둔다 (칸 수를 맞춰야 엑셀에 붙여 넣을 때 열이 밀리지 않는다)."""
    rows = []
    for row in _grid(table):
        cells = [_cell_text(block_id, c).replace("\n", " ") if isinstance(c, dict) else "" for c in row]
        rows.append("\t".join(cells))
    return "\n".join(rows)


def table_to_markdown(block_id: str, table: dict) -> str:
    """
    표 → Markdown 표. .md 파일에는 항상 순수 Markdown 표만 쓴다 (HTML <table> 을 섞지 않는다).
    Markdown 표 문법에는 병합 셀이 없으므로, 병합 칸의 글자는 첫 칸(왼쪽 위)에 두고 가려진 칸은 빈칸으로 둔다.
    txt 의 탭 구분 표와 같은 규칙이라 두 결과의 칸 위치가 서로 맞는다.
    """
    if not table["cells"] or not table.get("n_rows") or not table.get("n_cols"):
        return ""
    grid = _grid(table)

    def md_cell(c):
        text = _cell_text(block_id, c) if isinstance(c, dict) else ""
        return text.replace("|", "\\|").replace("\n", "<br>")

    lines = ["| " + " | ".join(md_cell(c) for c in grid[0]) + " |", "| " + " | ".join("---" for _ in grid[0]) + " |"]
    for row in grid[1:]:
        lines.append("| " + " | ".join(md_cell(c) for c in row) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# 블록 하나 → 글자
# ---------------------------------------------------------------------


def render_block(block: dict, result: dict | None, fmt: str) -> str:
    """
    블록 하나를 최종 결과의 한 조각으로 바꾼다.
    받는 값: structure 의 블록, 블록 결과(없으면 None), 형식("txt"|"md")
    """
    block_id = block["block_id"]
    btype = block["type"]

    # 사용자가 고친 내용이 있으면 그것을 쓴다. (원본 raw_text 는 결과 JSON 에 그대로 남아 있다)
    edited = result.get("edited_text") if result else None
    if edited is not None:
        return _title_prefix(block, fmt) + edited if btype == "title" else edited

    if btype == "image":
        if fmt == "md" and result and result.get("image_path"):
            marker = f"![{image_marker(block_id)}](blocks/{result['image_path']})"
        else:
            marker = image_marker(block_id)
        chart = (result or {}).get("chart_table")
        if chart:
            # 막대 차트는 그림 아래에 값 표를 붙인다 (processors/image.py chart_to_table)
            body = table_to_markdown(block_id, chart) if fmt == "md" else table_to_tsv(block_id, chart)
            return f"{marker}\n\n{body}"
        return marker

    # [규칙 2] 처리 결과가 없거나(pending) 실패면 내용을 채우지 않고 실패 표시만 한다
    if not result or result.get("status") != "done":
        return failure_marker(block_id)

    if btype == "table":
        table = result.get("table") or {"cells": [], "n_rows": 0, "n_cols": 0}
        body = table_to_markdown(block_id, table) if fmt == "md" else table_to_tsv(block_id, table)
        # 표 제목(맨 윗줄 전체 병합 칸이었던 글자)은 표 위에, 칸 밖 글자(자막·주석)는 표 아래에 쓴다
        before = [_piece_text(block_id, p, "제목") for p in table.get("head_text") or []]
        if table.get("caption"):  # 예전 형식 결과(제목 한 조각)
            before.insert(0, _piece_text(block_id, table["caption"], "제목"))
        after = [_piece_text(block_id, p, "표 밖") for p in result.get("outside_text") or []]
        return "\n\n".join(s for s in before + [body] + after if s)

    text = result.get("text") or ""
    if btype == "title":
        return _title_prefix(block, fmt) + text.replace("\n", " ")
    return text


def _title_prefix(block: dict, fmt: str) -> str:
    """md 에서 문서 제목은 #, 그 밖의 제목은 ## 을 붙인다. txt 에는 아무것도 붙이지 않는다."""
    if fmt != "md":
        return ""
    return "# " if block.get("label") == "doc_title" else "## "


PAGE_FURNITURE_TYPES = ("header", "footer")  # 머리말·꼬리말·쪽번호 (layout.py 의 LABEL_TO_TYPE)


def is_skipped(block: dict, skip_page_furniture: bool) -> bool:
    """
    결과 파일에서 뺄 블록인지.
    이미지·PDF 에서 레이아웃 모델이 머리말·꼬리말·쪽번호로 잡은 블록은 본문이 아닌 경우가 많다
    (실제 사례: 영상 캡처의 재생 시간 "10:10/11:54", 플레이어 아이콘). 화면에는 그대로 보여 준다.
    HWP 의 머리말은 파일에 명시된 글자라 빼지 않는다.
    """
    return skip_page_furniture and block["type"] in PAGE_FURNITURE_TYPES and block.get("source") != "hwp"


def merge_document(structure: dict, results: dict[str, dict], fmt: str = "txt", page_separator: bool = True, debug: bool = False,
                   skip_page_furniture: bool = False) -> str:
    """
    블록 결과들을 order 순서로 이어 붙인다.
    받는 값: structure, {block_id: 결과}, 형식, 페이지 구분선 여부, 디버그 여부, 머리말·꼬리말 블록 제외 여부
    돌려주는 값: 최종 문자열
    """
    parts: list[str] = []
    last_page = None
    for block in sorted(structure["blocks"], key=lambda b: b["order"]):  # [규칙 1] order 순서
        if is_skipped(block, skip_page_furniture):
            continue
        result = results.get(block["block_id"])
        if block.get("label") == "uncovered_text" and result and result.get("status") == "failed" and result.get("edited_text") is None:
            # 레이아웃 블록 밖에서 다시 찾은 글자가 신뢰도 미달이면 결과에 넣지 않는다 (화면 버튼·아이콘 같은 잡음이 대부분).
            # 레이아웃 모델이 잡은 블록이 아니라 우리가 덧붙인 것이라 [규칙 2]의 "블록을 빼먹지 않는다" 대상이 아니다. 화면에는 보인다
            continue
        if page_separator and last_page is not None and block["page"] != last_page:
            parts.append(f"--- page {block['page']} ---")
        last_page = block["page"]
        piece = render_block(block, result, fmt)
        if debug:
            # 예전에는 <!-- p1-b003 --> (HTML 주석)이었는데, .md 미리보기에서는 주석이 숨겨져 켜고 끈 결과가 똑같아 보였다.
            # 어느 블록에서 나온 글인지 눈으로 찾는 게 목적이라 txt/md 모두 보이는 글자로 쓴다
            piece = f"{debug_marker(block['block_id'])}\n{piece}"
        parts.append(piece)
    return "\n\n".join(parts).strip() + "\n"


def load_results(storage: DocStorage, structure: dict) -> dict[str, dict]:
    """structure 의 모든 블록 결과 JSON 을 읽어 {block_id: 결과} 로 돌려준다. 없는 결과는 빠진다(→ 실패 표시됨)."""
    results = {}
    for block in structure["blocks"]:
        r = read_json(storage.block_result(block["block_id"]))
        if r is not None:
            results[block["block_id"]] = r
    return results


def merge_outputs(storage: DocStorage, settings) -> str:
    """
    .txt / .md 결과 파일을 만들고, DB 에 넣을 .txt 문자열을 돌려준다.
    파일에는 설정의 기본 옵션(페이지 구분선)을 쓰고, 디버그 모드는 끈다. (다른 옵션은 result API 에서 그때그때 만든다)
    """
    structure = read_json(storage.structure_json)
    results = load_results(storage, structure)
    skip = settings.MERGE_SKIP_PAGE_FURNITURE
    txt = merge_document(structure, results, "txt", settings.MERGE_PAGE_SEPARATOR, False, skip)
    md = merge_document(structure, results, "md", settings.MERGE_PAGE_SEPARATOR, False, skip)
    storage.output_dir.mkdir(parents=True, exist_ok=True)
    storage.output("txt").write_text(txt, encoding="utf-8")
    storage.output("md").write_text(md, encoding="utf-8")
    return txt


def save_to_db(settings, file_id: int, file_name: str, text: str) -> int:
    """
    합친 .txt 를 extracted_texts 에 저장(같은 file_id 가 있으면 갱신)한다.
    [규칙 2] [추출 실패: ...] 표시가 들어 있어도 그대로 저장한다. 실패를 숨기면 나중에 확인할 방법이 없기 때문.
    """
    return db.save_extracted_text(settings.DB_PATH, file_id, file_name, text)
