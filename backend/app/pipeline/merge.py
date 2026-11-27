"""
[3단계 — 이 파일이 하는 일]
structure.json 의 order 순서대로 블록 결과를 이어 붙여 최종 결과를 만든다.
  - .txt : 순수 텍스트. 표는 탭(\t)으로 칸을 나눈다
  - .md  : 제목은 #, 표는 Markdown 표(병합 셀이 있으면 HTML 표), 이미지는 자리표시자+링크
  - 옵션 : 페이지 구분선(--- page 2 ---), 디버그 모드(<!-- p1-b003 --> 주석)
그리고 합친 .txt 를 DB extracted_texts 테이블에 저장한다.

[왜 이렇게 나눴는지]
- 합치기는 모델이 필요 없는 "글자 이어 붙이기"라서, 사용자가 블록을 고치면 API 서버에서 바로 다시 합칠 수 있다.
- [규칙 1] 순서는 오직 structure 의 order 로 정한다. 결과 파일이 만들어진 순서(처리 순서·retry 순서)는 보지 않는다.
- [규칙 2] 실패한 블록은 내용을 짐작해 채우지 않고 [추출 실패: p1-b003] 로 표시한다. 블록을 빼먹지도 않는다.
"""
import html

from app import db
from app.pipeline.storage import DocStorage, read_json


def failure_marker(block_id: str, cell: str | None = None) -> str:
    """[규칙 2] 실패 표시. 블록 번호를 함께 적어 어느 블록인지 바로 찾을 수 있게 한다."""
    return f"[추출 실패: {block_id} {cell}]" if cell else f"[추출 실패: {block_id}]"


def image_marker(block_id: str) -> str:
    return f"[이미지: {block_id}]"


# ---------------------------------------------------------------------
# 표 그리기
# ---------------------------------------------------------------------


def _cell_text(block_id: str, cell: dict) -> str:
    """셀 하나의 표시 글자. 실패한 셀은 [추출 실패: p1-b003 r2c3] (r/c 는 사람이 보기 쉽게 1부터)."""
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


def table_to_tsv(block_id: str, table: dict) -> str:
    """표 → 탭 구분 텍스트. 병합으로 가려진 칸은 빈칸으로 둔다 (칸 수를 맞춰야 엑셀에 붙여 넣을 때 열이 밀리지 않는다)."""
    rows = []
    for row in _grid(table):
        cells = [_cell_text(block_id, c).replace("\n", " ") if isinstance(c, dict) else "" for c in row]
        rows.append("\t".join(cells))
    return "\n".join(rows)


def table_to_markdown(block_id: str, table: dict) -> str:
    """
    표 → Markdown 표. 단, 병합 셀이 하나라도 있으면 HTML 표로 만든다.
    Markdown 표 문법에는 병합 셀을 표현하는 방법이 없어서, 억지로 쪼개면 내용이 틀린 칸에 들어간 것처럼 보이기 때문.
    """
    if not table["cells"]:
        return ""
    has_span = any(c.get("rowspan", 1) > 1 or c.get("colspan", 1) > 1 for c in table["cells"])
    grid = _grid(table)
    if has_span:
        out = ["<table>"]
        for row in grid:
            out.append("  <tr>")
            for c in row:
                if isinstance(c, dict):
                    attrs = ""
                    if c.get("rowspan", 1) > 1:
                        attrs += f' rowspan="{c["rowspan"]}"'
                    if c.get("colspan", 1) > 1:
                        attrs += f' colspan="{c["colspan"]}"'
                    text = html.escape(_cell_text(block_id, c)).replace("\n", "<br>")
                    out.append(f"    <td{attrs}>{text}</td>")
                elif c is None:
                    out.append("    <td></td>")
            out.append("  </tr>")
        out.append("</table>")
        return "\n".join(out)

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
            return f"![{image_marker(block_id)}](blocks/{result['image_path']})"
        return image_marker(block_id)

    # [규칙 2] 처리 결과가 없거나(pending) 실패면 내용을 채우지 않고 실패 표시만 한다
    if not result or result.get("status") != "done":
        return failure_marker(block_id)

    if btype == "table":
        table = result.get("table") or {"cells": [], "n_rows": 0, "n_cols": 0}
        return table_to_markdown(block_id, table) if fmt == "md" else table_to_tsv(block_id, table)

    text = result.get("text") or ""
    if btype == "title":
        return _title_prefix(block, fmt) + text.replace("\n", " ")
    return text


def _title_prefix(block: dict, fmt: str) -> str:
    """md 에서 문서 제목은 #, 그 밖의 제목은 ## 을 붙인다. txt 에는 아무것도 붙이지 않는다."""
    if fmt != "md":
        return ""
    return "# " if block.get("label") == "doc_title" else "## "


def merge_document(structure: dict, results: dict[str, dict], fmt: str = "txt", page_separator: bool = True, debug: bool = False) -> str:
    """
    블록 결과들을 order 순서로 이어 붙인다.
    받는 값: structure, {block_id: 결과}, 형식, 페이지 구분선 여부, 디버그 여부
    돌려주는 값: 최종 문자열
    """
    parts: list[str] = []
    last_page = None
    for block in sorted(structure["blocks"], key=lambda b: b["order"]):  # [규칙 1] order 순서
        if page_separator and last_page is not None and block["page"] != last_page:
            parts.append(f"--- page {block['page']} ---")
        last_page = block["page"]
        piece = render_block(block, results.get(block["block_id"]), fmt)
        if debug:
            piece = f"<!-- {block['block_id']} -->\n{piece}"
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
    txt = merge_document(structure, results, "txt", settings.MERGE_PAGE_SEPARATOR, False)
    md = merge_document(structure, results, "md", settings.MERGE_PAGE_SEPARATOR, False)
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
