"""
[이 파일이 하는 일]
HWP / HWPX 문서를 "직접 파싱"해서 문단·표·그림 목록을 뽑아낸다. (OCR 을 쓰지 않는다)

[왜 (B) 직접 파싱을 골랐는지] — (A) LibreOffice 로 PDF 변환 방식과 비교
  (A) 장점: PDF 로 바꾸면 페이지 이미지와 좌표(bbox)가 생겨서 왼쪽 화면에 박스를 그릴 수 있다.
      단점: LibreOffice + H2Orestart 확장을 서버마다 설치해야 하고, 변환이 수 초씩 걸리며(30초 제한에 불리),
            변환된 PDF 를 다시 OCR 하면 원래 정확했던 글자에 오인식이 생길 수 있다.
  (B) 장점: 문서 안에 저장된 글자를 그대로 꺼내므로 오인식이 없고 빠르다. 표의 병합 셀 정보도 정확히 얻는다.
            추가 설치가 필요 없다(olefile 만 사용).
      단점: 페이지 나눔·좌표 정보가 없다. → [규칙 3] 모든 블록의 bbox 를 null 로 두고, 페이지는 1로 둔다.
            글상자 속 글자, 수식 등 일부 개체는 빠질 수 있다.
  이 PC 에는 LibreOffice 가 없고, 텍스트 정확도가 더 중요하다고 보고 (B)를 골랐다.

형식
- HWPX: zip 파일. 안에 Contents/section0.xml 같은 XML 로 본문이 들어 있다. (표준 XML 이라 읽기 쉽다)
- HWP : 옛날 바이너리 형식. OLE(파일 안에 여러 "스트림"을 넣는 MS 형식) 안에 BodyText/Section0 이 있고,
        그 안은 zlib 으로 압축된 "레코드(태그+길이+데이터)"의 나열이다.

돌려주는 값 (둘 다 같은 모양): 블록 dict 의 리스트
  {"type": "text"|"title"|"table"|"image"|"header"|"footer", "label": "...", "text": "...",
   "table": {"n_rows", "n_cols", "cells": [{"row","col","rowspan","colspan","text"}]},
   "image_bytes": bytes|None, "image_ext": "png"|None}
"""
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET

import olefile

from app.pipeline.errors import PipelineError, user_message


def _local(tag: str) -> str:
    """'{http://www.hancom.co.kr/hwpml/2011/paragraph}p' → 'p'.
    HWPX 버전마다 네임스페이스 주소가 조금씩 달라서, 태그 이름의 끝부분만 보고 판단하기 위해 쓴다."""
    return tag.rsplit("}", 1)[-1]


def _attr(el, name: str, default=None):
    """네임스페이스가 붙은 속성도 이름 끝부분으로 찾는다."""
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return default


# =====================================================================
# HWPX
# =====================================================================

TITLE_STYLE_PREFIXES = ("제목", "개요", "heading", "title", "outline")


def parse_hwpx(path) -> list[dict]:
    """HWPX 파일을 읽어 블록 목록을 돌려준다. 손상된 파일이면 PipelineError."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file")) from e

    with zf:
        names = zf.namelist()
        # section0.xml, section1.xml ... 을 번호 순서대로 읽는다 (문서의 앞→뒤 순서)
        sections = sorted(
            (n for n in names if re.match(r"Contents/section\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
        )
        if not sections:
            raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file"))

        styles = _hwpx_styles(zf, names)
        blocks: list[dict] = []
        for name in sections:
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError as e:
                raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file")) from e
            for p in root:
                if _local(p.tag) == "p":
                    blocks.extend(_hwpx_paragraph(p, styles, zf, names))
        return blocks


def _hwpx_styles(zf, names) -> dict:
    """header.xml 의 스타일 id → 스타일 이름. "제목 1" 같은 스타일이면 title 블록으로 분류하려고 읽는다."""
    styles = {}
    if "Contents/header.xml" not in names:
        return styles
    try:
        root = ET.fromstring(zf.read("Contents/header.xml"))
    except ET.ParseError:
        return styles
    for el in root.iter():
        if _local(el.tag) == "style":
            styles[_attr(el, "id")] = f"{_attr(el, 'name', '')} {_attr(el, 'engName', '')}".strip()
    return styles


def _hwpx_text_of(el) -> str:
    """요소 안의 모든 글자(hp:t)를 문단 단위 줄바꿈으로 이어서 돌려준다 (표 셀, 머리말 등에 사용)."""
    paras = []
    for p in el.iter():
        if _local(p.tag) == "p":
            text = "".join(_hwpx_t_text(t) for t in p.iter() if _local(t.tag) == "t")
            paras.append(text)
    return "\n".join(s for s in paras if s.strip())


def _hwpx_t_text(t) -> str:
    """<hp:t>안녕<hp:tab/>하세요</hp:t> 처럼 글자 사이에 탭/줄바꿈 요소가 끼어 있을 수 있어서 직접 조립한다."""
    out = [t.text or ""]
    for sub in t:
        name = _local(sub.tag)
        if name == "tab":
            out.append("\t")
        elif name in ("lineBreak", "lineBreakCtrl"):
            out.append("\n")
        out.append(sub.tail or "")
    return "".join(out)


def _hwpx_paragraph(p, styles, zf, names) -> list[dict]:
    """
    최상위 문단 하나를 블록 0개 이상으로 바꾼다.
    문단 안에서 글자 → 표 → 글자 순서로 나오면 그 순서 그대로 블록을 만든다 (읽기 순서 유지).
    """
    style_name = styles.get(_attr(p, "styleIDRef"), "")
    is_title = style_name.lower().startswith(TITLE_STYLE_PREFIXES)
    blocks: list[dict] = []
    buf: list[str] = []

    def flush():
        text = "".join(buf).strip()
        buf.clear()
        if text:
            blocks.append({"type": "title" if is_title else "text", "label": f"hwp:{style_name or 'paragraph'}", "text": text})

    for run in p:
        if _local(run.tag) != "run":
            continue
        for child in run:
            name = _local(child.tag)
            if name == "t":
                buf.append(_hwpx_t_text(child))
            elif name == "tbl":
                flush()
                blocks.append(_hwpx_table(child))
            elif name == "pic":
                flush()
                blocks.append(_hwpx_picture(child, zf, names))
            elif name == "ctrl":
                # 머리말/꼬리말은 ctrl 안에 header/footer 로 들어 있다
                for sub in child:
                    sub_name = _local(sub.tag)
                    if sub_name in ("header", "footer"):
                        text = _hwpx_text_of(sub)
                        if text:
                            flush()
                            blocks.append({"type": sub_name, "label": f"hwp:{sub_name}", "text": text})
            else:
                # 글상자(rect 등) 안의 글자. 위치는 모르므로 일반 문단처럼 이어 붙인다
                inner = _hwpx_text_of(child)
                if inner:
                    flush()
                    blocks.append({"type": "text", "label": f"hwp:{name}", "text": inner})
    flush()
    return blocks


def _hwpx_table(tbl) -> dict:
    """<hp:tbl> → 표 블록. cellAddr(위치), cellSpan(병합) 정보를 그대로 옮긴다."""
    cells = []
    # tbl.iter() 로 전부 훑지 않고 "바로 아래 tr → 바로 아래 tc" 만 본다.
    # 표 안에 표(중첩 표)가 있으면 안쪽 표의 셀까지 바깥 표의 셀로 잘못 섞이기 때문. (안쪽 글자는 바깥 셀 텍스트에 포함된다)
    for tr in tbl:
        if _local(tr.tag) != "tr":
            continue
        for tc in tr:
            if _local(tc.tag) != "tc":
                continue
            addr = next((c for c in tc if _local(c.tag) == "cellAddr"), None)
            span = next((c for c in tc if _local(c.tag) == "cellSpan"), None)
            sub = next((c for c in tc if _local(c.tag) == "subList"), tc)
            cells.append(
                {
                    "row": int(_attr(addr, "rowAddr", 0)) if addr is not None else 0,
                    "col": int(_attr(addr, "colAddr", 0)) if addr is not None else 0,
                    "rowspan": int(_attr(span, "rowSpan", 1)) if span is not None else 1,
                    "colspan": int(_attr(span, "colSpan", 1)) if span is not None else 1,
                    "text": _hwpx_text_of(sub),
                }
            )
    n_rows = int(_attr(tbl, "rowCnt", 0)) or (max((c["row"] + c["rowspan"] for c in cells), default=0))
    n_cols = int(_attr(tbl, "colCnt", 0)) or (max((c["col"] + c["colspan"] for c in cells), default=0))
    return {"type": "table", "label": "hwp:table", "table": {"n_rows": n_rows, "n_cols": n_cols, "cells": cells}}


def _hwpx_picture(pic, zf, names) -> dict:
    """<hp:pic> → 이미지 블록. BinData 폴더에서 실제 이미지 파일을 찾아 담는다."""
    ref = None
    for el in pic.iter():
        if _local(el.tag) == "img":
            ref = _attr(el, "binaryItemIDRef")
            break
    data, ext = None, None
    if ref:
        match = next((n for n in names if n.startswith(f"BinData/{ref}.")), None)
        if match:
            data = zf.read(match)
            ext = match.rsplit(".", 1)[-1].lower()
    return {"type": "image", "label": "hwp:picture", "image_bytes": data, "image_ext": ext}


# =====================================================================
# HWP (바이너리 5.x)
# =====================================================================

# 레코드 태그 번호 (한컴 "한글 문서 파일 형식 5.0" 공개 문서 기준). HWPTAG_BEGIN = 16
TAG_PARA_HEADER = 66
TAG_PARA_TEXT = 67
TAG_CTRL_HEADER = 71
TAG_LIST_HEADER = 72
TAG_TABLE = 77
TAG_SHAPE_PICTURE = 85

# PARA_TEXT 안의 제어 문자 중, 뒤에 7글자(14바이트)짜리 추가 정보가 붙는 것들 (총 8글자 크기)
_INLINE_OR_EXTENDED = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}


def parse_hwp(path) -> list[dict]:
    """HWP(바이너리) 파일을 읽어 블록 목록을 돌려준다."""
    try:
        ole = olefile.OleFileIO(str(path))
    except (OSError, ValueError) as e:
        raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file")) from e

    with ole:
        if not ole.exists("FileHeader"):
            raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file"))
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file"))
        flags = struct.unpack_from("<I", header, 36)[0]
        compressed = bool(flags & 0x1)
        if flags & 0x2:  # 암호 설정
            raise PipelineError("preprocess", "encrypted", user_message("encrypted"))
        if flags & 0x4:  # 배포용 문서: 본문이 암호화된 ViewText 에 들어 있어 읽을 수 없다
            raise PipelineError("preprocess", "hwp_distribution", user_message("hwp_distribution"))

        sections = sorted(
            ("/".join(e) for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")),
            key=lambda n: int(re.sub(r"\D", "", n.split("/")[1]) or 0),
        )
        blocks: list[dict] = []
        for name in sections:
            data = ole.openstream(name).read()
            if compressed:
                try:
                    # -15: 헤더 없는 raw deflate 형식. HWP 는 zlib 헤더 없이 압축해서 저장한다
                    data = zlib.decompress(data, -15)
                except zlib.error as e:
                    raise PipelineError("preprocess", "corrupted_file", user_message("corrupted_file")) from e
            blocks.extend(parse_hwp_records(data))
        return blocks


def read_records(data: bytes) -> list[tuple[int, int, bytes]]:
    """
    레코드 나열을 (태그, 깊이, 데이터) 리스트로 나눈다.
    레코드 헤더 4바이트 = 태그(10비트) + 깊이(10비트) + 크기(12비트). 크기가 0xFFF 면 다음 4바이트가 실제 크기.
    깊이(level)는 "이 레코드가 누구의 자식인지"를 나타낸다. 예) 표 셀 안의 문단은 표보다 깊이가 크다.
    """
    records, pos, n = [], 0, len(data)
    while pos + 4 <= n:
        h = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, (h >> 20) & 0xFFF
        if size == 0xFFF:
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        records.append((tag, level, data[pos : pos + size]))
        pos += size
    return records


def decode_para_text(data: bytes) -> str:
    """
    PARA_TEXT(UTF-16 글자 배열)를 문자열로 바꾼다.
    표·그림 같은 개체는 본문 안에 "제어 문자 + 추가 정보 7글자"로 끼어 있으므로 건너뛴다.
    """
    out, i, n = [], 0, len(data) // 2
    chars = struct.unpack_from(f"<{n}H", data)
    while i < n:
        ch = chars[i]
        if ch < 32:
            if ch in _INLINE_OR_EXTENDED:
                if ch == 9:
                    out.append("\t")
                i += 8
                continue
            if ch == 10:
                out.append("\n")
            i += 1
            continue
        out.append(chr(ch))
        i += 1
    return "".join(out)


def _ctrl_id(data: bytes) -> str:
    """CTRL_HEADER 의 컨트롤 종류. 'tbl '(표), 'gso '(그림/도형), 'head'(머리말) 등. 바이트가 거꾸로 저장되어 있어 뒤집는다."""
    return data[:4][::-1].decode("latin-1") if len(data) >= 4 else ""


def _subtree_end(records, i: int) -> int:
    """i 번째 레코드의 자식들이 끝나는 위치. 자기보다 깊이가 같거나 얕은 레코드가 나오면 자식이 끝난 것이다."""
    level = records[i][1]
    j = i + 1
    while j < len(records) and records[j][1] > level:
        j += 1
    return j


def parse_hwp_records(data: bytes) -> list[dict]:
    """압축을 푼 BodyText 섹션 하나를 블록 목록으로 바꾼다. (테스트에서 직접 부를 수 있게 따로 뺐다)"""
    records = read_records(data)
    blocks: list[dict] = []
    pending: list[str] = []

    def flush():
        text = "".join(pending).strip()
        pending.clear()
        if text:
            blocks.append({"type": "text", "label": "hwp:paragraph", "text": text})

    i = 0
    while i < len(records):
        tag, level, payload = records[i]
        if tag == TAG_PARA_HEADER and level == 0:
            flush()
        elif tag == TAG_PARA_TEXT and level == 1:
            pending.append(decode_para_text(payload))
        elif tag == TAG_CTRL_HEADER and level == 1:
            end = _subtree_end(records, i)
            sub = records[i + 1 : end]
            ctrl = _ctrl_id(payload)
            if ctrl == "tbl ":
                flush()
                blocks.append(_hwp_table(sub))
            elif ctrl in ("head", "foot"):
                text = "\n".join(decode_para_text(d) for t, _, d in sub if t == TAG_PARA_TEXT).strip()
                if text:
                    flush()
                    blocks.append({"type": "header" if ctrl == "head" else "footer", "label": f"hwp:{ctrl}", "text": text})
            elif ctrl == "gso ":
                flush()
                if any(t == TAG_SHAPE_PICTURE for t, _, _ in sub):
                    # 그림 데이터(BinData)와의 연결 정보는 해석하지 않으므로 이미지 바이트는 없다
                    blocks.append({"type": "image", "label": "hwp:picture", "image_bytes": None, "image_ext": None})
                else:
                    text = "\n".join(decode_para_text(d) for t, _, d in sub if t == TAG_PARA_TEXT).strip()
                    if text:
                        blocks.append({"type": "text", "label": "hwp:textbox", "text": text})
            i = end
            continue
        i += 1
    flush()
    return blocks


def _hwp_table(sub) -> dict:
    """표 컨트롤의 자식 레코드들 → 표 블록."""
    n_rows = n_cols = 0
    for tag, _, d in sub:
        if tag == TAG_TABLE and len(d) >= 8:
            n_rows, n_cols = struct.unpack_from("<HH", d, 4)
            break

    cell_level = next((lv for t, lv, _ in sub if t == TAG_LIST_HEADER), None)
    cells, current = [], None
    for tag, level, d in sub:
        if tag == TAG_LIST_HEADER and level == cell_level:
            current = {**_cell_props(d, n_rows, n_cols), "text_parts": []}
            cells.append(current)
        elif tag == TAG_PARA_TEXT and current is not None:
            current["text_parts"].append(decode_para_text(d))
    for c in cells:
        c["text"] = "\n".join(p for p in c.pop("text_parts") if p.strip()).strip()
    if not n_rows:
        n_rows = max((c["row"] + c["rowspan"] for c in cells), default=0)
    if not n_cols:
        n_cols = max((c["col"] + c["colspan"] for c in cells), default=0)
    return {"type": "table", "label": "hwp:table", "table": {"n_rows": n_rows, "n_cols": n_cols, "cells": cells}}


def _cell_props(d: bytes, n_rows: int, n_cols: int) -> dict:
    """
    LIST_HEADER 에서 셀 위치(열, 행)와 병합 크기를 읽는다.
    공개 문서에는 셀 정보가 6바이트 뒤부터라고 되어 있지만, 실제 파일은 8바이트 뒤인 경우가 많다.
    그래서 8 → 6 순서로 읽어 보고, 표 크기 안에 들어오는 값을 쓴다.
    """
    for offset in (8, 6):
        if len(d) >= offset + 8:
            col, row, colspan, rowspan = struct.unpack_from("<HHHH", d, offset)
            if (not n_rows or row < n_rows) and (not n_cols or col < n_cols) and colspan >= 1 and rowspan >= 1:
                return {"row": row, "col": col, "rowspan": rowspan, "colspan": colspan}
    return {"row": 0, "col": 0, "rowspan": 1, "colspan": 1}
