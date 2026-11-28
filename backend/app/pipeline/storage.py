"""
[이 파일이 하는 일]
문서 하나(doc_id)에 딸린 파일들의 저장 위치를 정하고, JSON 을 안전하게 읽고 쓴다.

  storage/{doc_id}/
    original.pdf            업로드한 원본
    status.json             진행 상태 (API 가 읽음)
    structure.json          1단계 결과: 블록 목록
    pages/page-1.png        페이지 이미지 (프론트 왼쪽 화면에 사용)
    source/                 PDF 텍스트 레이어·HWP 파싱 원문 같은 "OCR 이 아닌" 원본 데이터
    blocks/p1-b001.png      2단계: 블록 crop 이미지
    results/p1-b001.json    2단계: 블록 처리 결과
    output/result.txt|md    3단계: 합친 결과
    output/chunks.json      3단계: 합친 결과를 검색·RAG 용으로 나눈 청크 (DB text_chunks 에도 저장)
    assist/summary.json     LLM 도우미: 요약 / assist/corrections.json 교정 제안 (app/assist)

[왜 이렇게 나눴는지]
경로를 코드 여기저기서 문자열로 만들면 오타 하나로 엉뚱한 곳에 저장된다. 경로 규칙은 이 클래스에만 둔다.
또 사용자가 보낸 doc_id/block_id 를 그대로 경로에 붙이면 "../../" 같은 값으로 다른 폴더 파일을 읽어 갈 수 있다(경로 조작 공격).
그래서 형식을 정규식으로 검사한 뒤에만 경로를 만든다.
"""
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

BLOCK_ID_RE = re.compile(r"^p\d+-b\d{3,}$")


def is_valid_doc_id(doc_id: str) -> bool:
    """doc_id 가 uuid 형식인지 검사한다."""
    try:
        return str(uuid.UUID(doc_id)) == doc_id
    except (ValueError, TypeError, AttributeError):
        return False


def is_valid_block_id(block_id: str) -> bool:
    """block_id 가 p1-b001 형식인지 검사한다."""
    return bool(BLOCK_ID_RE.match(block_id or ""))


def write_json(path: Path, data) -> None:
    """
    JSON 을 "임시 파일에 다 쓴 뒤 이름을 바꾸는" 방식으로 저장한다.
    바로 덮어쓰면, 쓰는 도중에 API 가 그 파일을 읽을 경우 반쯤 쓰인 깨진 JSON 을 읽게 된다.
    os.replace 는 한 번에(원자적으로) 바뀌므로, 읽는 쪽은 항상 이전 파일 또는 완성된 새 파일만 보게 된다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Windows 에서는 다른 프로세스가 파일을 읽는 순간 replace 가 잠깐 실패할 수 있어 몇 번 다시 시도한다
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_json(path: Path):
    """JSON 파일을 읽는다. 파일이 없으면 None."""
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class DocStorage:
    """문서 하나의 저장 경로 모음. DocStorage(settings.STORAGE_DIR, doc_id) 로 만든다."""

    def __init__(self, storage_dir: Path, doc_id: str):
        if not is_valid_doc_id(doc_id):
            raise ValueError(f"잘못된 doc_id: {doc_id!r}")
        self.doc_id = doc_id
        self.root = Path(storage_dir) / doc_id

    # ---- 폴더 ----
    @property
    def pages_dir(self) -> Path:
        return self.root / "pages"

    @property
    def blocks_dir(self) -> Path:
        return self.root / "blocks"

    @property
    def results_dir(self) -> Path:
        return self.root / "results"

    @property
    def source_dir(self) -> Path:
        return self.root / "source"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    # ---- 파일 ----
    def original(self, ext: str) -> Path:
        return self.root / f"original.{ext}"

    @property
    def structure_json(self) -> Path:
        return self.root / "structure.json"

    @property
    def status_json(self) -> Path:
        return self.root / "status.json"

    def page_image(self, page: int) -> Path:
        return self.pages_dir / f"page-{int(page)}.png"

    def block_image(self, block_id: str) -> Path:
        self._check_block_id(block_id)
        return self.blocks_dir / f"{block_id}.png"

    def block_result(self, block_id: str) -> Path:
        self._check_block_id(block_id)
        return self.results_dir / f"{block_id}.json"

    def block_source(self, block_id: str) -> Path:
        self._check_block_id(block_id)
        return self.source_dir / f"{block_id}.json"

    def output(self, fmt: str) -> Path:
        if fmt not in ("txt", "md"):
            raise ValueError(fmt)
        return self.output_dir / f"result.{fmt}"

    @property
    def chunks_json(self) -> Path:
        """3단계: 검색·RAG 용 청크 목록 (app/pipeline/chunking.py). DB text_chunks 와 같은 내용."""
        return self.output_dir / "chunks.json"

    def assist_json(self, kind: str) -> Path:
        """LLM 도우미 결과 파일. kind 는 summary | corrections 만 (경로 조작 방지)."""
        if kind not in ("summary", "corrections"):
            raise ValueError(kind)
        return self.root / "assist" / f"{kind}.json"

    @staticmethod
    def _check_block_id(block_id: str) -> None:
        if not is_valid_block_id(block_id):
            raise ValueError(f"잘못된 block_id: {block_id!r}")

    def create_dirs(self) -> None:
        for d in (self.root, self.pages_dir, self.blocks_dir, self.results_dir, self.source_dir, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)
