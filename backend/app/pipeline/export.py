"""
[이 파일이 하는 일]
OCR 결과를 편집 가능한 PDF 와 PowerPoint(.pptx) 파일로 만든다. (결과 다운로드 버튼의 PDF / PPT)

  - PDF  : 글자가 그림이 아니라 진짜 글자로 들어간 PDF. 선택·복사·검색이 되고 Acrobat 등에서 고칠 수 있다.
           원본 페이지 하나 = PDF 페이지 하나부터 시작하고(내용이 길면 다음 페이지로 이어짐),
           제목·본문·표·그림을 위에서 아래로 깔끔하게 다시 배치한다 (PyMuPDF 의 Story = HTML → PDF).
  - PPTX : 원본 페이지 하나 = 슬라이드 하나. 블록을 원본과 같은 위치(bbox)에 글상자·표·그림으로 놓는다.
           PowerPoint 에서 글상자·표를 바로 고칠 수 있다. 위치를 모르는 HWP 블록은 위에서부터 차례로 쌓는다.

[왜 이렇게 나눴는지]
- 무엇을 넣을지는 .txt/.md 와 똑같이 정한다 (merge.py 의 규칙을 그대로 씀).
  [규칙 1] order 순서, [규칙 2] 실패 블록은 [추출 실패: ...] 표시, 사용자가 고친 edited_text 우선,
  머리말·꼬리말 빼기 설정(MERGE_SKIP_PAGE_FURNITURE)도 같다. 그래서 형식만 다르고 내용은 다운로드 결과와 같다.
- 한글 글꼴: PDF 는 PyMuPDF 에 들어 있는 CJK 대체 글꼴을 쓰므로 PC 에 글꼴이 없어도 된다.
  PPTX 는 글꼴 이름("맑은 고딕")만 적고, 파일을 여는 PC 의 글꼴로 보인다.
"""
import html
import io

import pymupdf
from PIL import Image

from app.pipeline.merge import _cell_text, _grid, _piece_text, debug_marker, is_skipped, render_block
from app.pipeline.storage import DocStorage

DEFAULT_PAGE = (1240, 1754)  # 페이지 크기를 모를 때(HWP) 쓰는 A4 비율 (150dpi 기준 픽셀)
PPT_FONT = "맑은 고딕"


# ---------------------------------------------------------------------
# 공통: 블록 → 내보낼 조각
# ---------------------------------------------------------------------


def _table_rows(block_id: str, table: dict) -> list[list[dict]]:
    """표 → 행 목록. 칸마다 {"text", "rowspan", "colspan"}, 병합으로 가려진 칸은 None."""
    rows = []
    for row in _grid(table):
        out = []
        for c in row:
            if isinstance(c, dict):
                out.append({"text": _cell_text(block_id, c), "rowspan": c.get("rowspan", 1), "colspan": c.get("colspan", 1)})
            elif c == "covered":
                out.append(None)
            else:
                out.append({"text": "", "rowspan": 1, "colspan": 1})
        rows.append(out)
    return rows


def block_items(block: dict, result: dict | None, storage: DocStorage) -> list[dict]:
    """
    블록 하나를 내보낼 조각 목록으로 바꾼다.
      {"kind": "heading", "level": 1|2, "text"} / {"kind": "text", "text"} /
      {"kind": "table", "rows"} / {"kind": "image", "path"}
    """
    block_id, btype = block["block_id"], block["type"]
    edited = (result or {}).get("edited_text") is not None
    if btype == "image" and not edited:
        items = []
        path = storage.block_image(block_id)
        if path.exists():
            items.append({"kind": "image", "path": path})
        else:
            items.append({"kind": "text", "text": render_block(block, result, "txt")})
        chart = (result or {}).get("chart_table")
        if chart:
            items.append({"kind": "table", "rows": _table_rows(block_id, chart)})
        return items
    if btype == "table" and not edited and result and result.get("status") == "done" and (result.get("table") or {}).get("cells"):
        table = result["table"]
        before = [_piece_text(block_id, p, "제목") for p in table.get("head_text") or []]
        if table.get("caption"):
            before.insert(0, _piece_text(block_id, table["caption"], "제목"))
        after = [_piece_text(block_id, p, "표 밖") for p in result.get("outside_text") or []]
        return ([{"kind": "text", "text": t} for t in before if t]
                + [{"kind": "table", "rows": _table_rows(block_id, table)}]
                + [{"kind": "text", "text": t} for t in after if t])
    text = render_block(block, result, "txt")
    if btype == "title":
        return [{"kind": "heading", "level": 1 if block.get("label") == "doc_title" else 2, "text": text}]
    return [{"kind": "text", "text": text}]


def pages_of(structure: dict, results: dict, skip_page_furniture: bool) -> list[tuple[int, list[dict]]]:
    """[(페이지 번호, 그 페이지의 블록 목록)] — merge_document 와 같은 규칙으로 뺄 블록을 뺀다."""
    pages: dict[int, list[dict]] = {}
    for block in sorted(structure["blocks"], key=lambda b: b["order"]):  # [규칙 1]
        result = results.get(block["block_id"])
        if is_skipped(block, skip_page_furniture):
            continue
        if block.get("label") == "uncovered_text" and result and result.get("status") == "failed" and result.get("edited_text") is None:
            continue
        pages.setdefault(block["page"], []).append(block)
    return sorted(pages.items())


# ---------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------

PDF_CSS = """
* { font-family: sans-serif; }
body { font-size: 10pt; line-height: 1.45; }
h1 { font-size: 18pt; margin: 0 0 8pt 0; }
h2 { font-size: 13pt; margin: 10pt 0 6pt 0; }
p { margin: 0 0 7pt 0; }
table { border-collapse: collapse; margin: 0 0 9pt 0; }
td { border: 0.6pt solid #888; padding: 2pt 4pt; font-size: 9pt; vertical-align: top; }
.debug { font-size: 7pt; color: #ae3ec9; margin: 0; }
"""


def _esc(text: str) -> str:
    return html.escape(text or "").replace("\n", "<br/>")


def _pdf_html(items: list[tuple[str, dict]], content_width: float, debug: bool) -> str:
    parts = []
    last_block = None
    for block_id, item in items:
        if debug and block_id != last_block:
            parts.append(f'<p class="debug">{_esc(debug_marker(block_id))}</p>')
        last_block = block_id
        kind = item["kind"]
        if kind == "heading":
            parts.append(f"<h{item['level']}>{_esc(item['text'])}</h{item['level']}>")
        elif kind == "text":
            if item["text"].strip():
                parts.append(f"<p>{_esc(item['text'])}</p>")
        elif kind == "table":
            rows = []
            for row in item["rows"]:
                cells = []
                for c in row:
                    if c is None:
                        continue
                    span = (f' rowspan="{c["rowspan"]}"' if c["rowspan"] > 1 else "") + (f' colspan="{c["colspan"]}"' if c["colspan"] > 1 else "")
                    cells.append(f"<td{span}>{_esc(c['text'])}</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            parts.append('<table width="100%">' + "".join(rows) + "</table>")
        elif kind == "image":
            with Image.open(item["path"]) as im:
                w, h = im.size
            width = min(content_width, w * 0.5)  # 200dpi 원본 기준 대략 실제 크기, 페이지 폭을 넘지 않게
            parts.append(f'<p><img src="{item["path"].name}" width="{width:.0f}" height="{width * h / w:.0f}"/></p>')
    return "".join(parts)


def to_pdf(structure: dict, results: dict, storage: DocStorage, skip_page_furniture: bool, debug: bool = False) -> bytes:
    """편집 가능한 PDF (A4). 원본 페이지마다 새 PDF 페이지에서 시작한다."""
    mediabox = pymupdf.paper_rect("a4")
    where = mediabox + (48, 48, -48, -48)  # 여백 약 1.7cm
    archive = pymupdf.Archive(str(storage.blocks_dir)) if storage.blocks_dir.exists() else None
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    pages = pages_of(structure, results, skip_page_furniture) or [(1, [])]
    for _, blocks in pages:
        items = [(b["block_id"], it) for b in blocks for it in block_items(b, results.get(b["block_id"]), storage)]
        story = pymupdf.Story(html=_pdf_html(items, where.width, debug) or "<p></p>", user_css=PDF_CSS, archive=archive)
        more = True
        while more:
            device = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
    writer.close()
    # 한글 대체 글꼴 전체(약 3.5MB)가 들어가 파일이 커진다. 실제로 쓴 글자만 남기고(subset) 압축한다
    doc = pymupdf.open("pdf", buf.getvalue())
    doc.subset_fonts()
    return doc.tobytes(garbage=3, deflate=True)


# ---------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------


def _font_pt(height_pt: float, lines: int, low: float = 8, high: float = 40) -> float:
    """상자 높이와 줄 수로 글자 크기를 어림한다 (줄 간격의 약 60% — 더 크면 원본보다 넓어져 줄이 바뀐다)."""
    return max(low, min(high, height_pt / max(1, lines) * 0.6))


def to_pptx(structure: dict, results: dict, storage: DocStorage, skip_page_furniture: bool) -> bytes:
    """원본 페이지 하나 = 슬라이드 하나. 블록을 원본 위치에 글상자·표·그림으로 놓는다."""
    from pptx import Presentation
    from pptx.util import Emu, Pt

    page_info = {p["page"]: p for p in structure.get("pages") or []}
    first = next(iter(page_info.values()), {})
    pw, ph = first.get("width") or DEFAULT_PAGE[0], first.get("height") or DEFAULT_PAGE[1]
    prs = Presentation()
    prs.slide_width = Emu(9144000)  # 10인치. 높이는 원본 페이지 비율에 맞춘다
    prs.slide_height = Emu(int(9144000 * ph / pw))
    blank = prs.slide_layouts[6]
    margin = int(prs.slide_width * 0.05)

    def style(frame, size):
        for para in frame.paragraphs:
            for run in para.runs:
                run.font.size = Pt(size)
                run.font.name = PPT_FONT

    def add_text(slide, text, box, size, bold=False):
        left, top, width, height = box
        tb = slide.shapes.add_textbox(left, top, width, height)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.text = text
        style(tf, size)
        if bold:
            for para in tf.paragraphs:
                for run in para.runs:
                    run.font.bold = True

    def add_table(slide, rows, box, size):
        left, top, width, height = box
        n_rows, n_cols = len(rows), max((len(r) for r in rows), default=0)
        if not n_rows or not n_cols:
            return
        shape = slide.shapes.add_table(n_rows, n_cols, left, top, width, height)
        table = shape.table
        for row_obj in table.rows:
            row_obj.height = max(1, height // n_rows)
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                if cell is None:
                    continue
                target = table.cell(r, c)
                if cell["rowspan"] > 1 or cell["colspan"] > 1:
                    target.merge(table.cell(min(n_rows - 1, r + cell["rowspan"] - 1), min(n_cols - 1, c + cell["colspan"] - 1)))
                target.text = cell["text"]
                target.margin_left = target.margin_right = Pt(3)
                target.margin_top = target.margin_bottom = Pt(1)
                style(target.text_frame, size)

    for page_no, blocks in pages_of(structure, results, skip_page_furniture) or [(1, [])]:
        slide = prs.slides.add_slide(blank)
        info = page_info.get(page_no, {})
        sx = prs.slide_width / (info.get("width") or pw)
        sy = prs.slide_height / (info.get("height") or ph)
        cursor = margin  # 위치를 모르는 블록(HWP)을 쌓을 세로 위치
        for block in blocks:
            items = block_items(block, results.get(block["block_id"]), storage)
            if block.get("bbox"):
                x1, y1, x2, y2 = block["bbox"]
                left, top = int(x1 * sx), int(y1 * sy)
                width, height = max(int((x2 - x1) * sx), Pt(20)), max(int((y2 - y1) * sy), Pt(12))
                if any(it["kind"] == "image" for it in items):
                    # 그림(차트 포함)은 그림만 놓는다. 차트 값 표까지 같은 자리에 놓으면 겹친다 (값 표는 PDF·txt 에 있다)
                    items = [it for it in items if it["kind"] == "image"]
                # 표 위·아래 글자(제목·주석)가 있으면 상자를 줄 수(표는 행 수) 비율로 나눠 쓴다
                weights = [len(it["rows"]) if it["kind"] == "table" else 1 if it["kind"] == "image"
                           else it["text"].count("\n") + 1 for it in items]
                total = sum(weights) or 1
                part_top = top
                for item, weight in zip(items, weights):
                    part_h = height * weight // total
                    pitch_pt = part_h / 12700 / weight  # 한 줄(표는 한 행) 높이
                    box = (left, part_top, width, part_h)
                    if item["kind"] == "image":
                        slide.shapes.add_picture(str(item["path"]), left, part_top, width, part_h)
                    elif item["kind"] == "table":
                        add_table(slide, item["rows"], box, max(6, min(14, pitch_pt * 0.45)))
                    elif item["text"].strip():
                        add_text(slide, item["text"], box, _font_pt(pitch_pt, 1, 7, 36), item["kind"] == "heading")
                    part_top += part_h
            else:
                # HWP: 위치가 없어 위에서부터 차례로 쌓는다. 넘치면 새 슬라이드
                width = prs.slide_width - 2 * margin
                for item in items:
                    lines = 1 if item["kind"] == "image" else (len(item["rows"]) if item["kind"] == "table" else item.get("text", "").count("\n") + 1)
                    size = 20 if item["kind"] == "heading" else 12
                    height = Pt(size * 1.6 * lines) if item["kind"] != "image" else int(width * 0.4)
                    if cursor + height > prs.slide_height - margin and cursor > margin:
                        slide = prs.slides.add_slide(blank)
                        cursor = margin
                    box = (margin, cursor, width, height)
                    if item["kind"] == "image":
                        pic = slide.shapes.add_picture(str(item["path"]), margin, cursor, height=height)
                        height = pic.height
                    elif item["kind"] == "table":
                        add_table(slide, item["rows"], box, 10)
                    elif item["text"].strip():
                        add_text(slide, item["text"], box, size, item["kind"] == "heading")
                    cursor += height + Pt(8)
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()
