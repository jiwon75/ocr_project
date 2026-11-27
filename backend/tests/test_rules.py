"""
["반드시 지킬 규칙" 테스트]
규칙 1: block_id/order 는 1단계에서만 정하고 이후 절대 바뀌지 않는다
규칙 2: 잘못된 text 는 고치지 않고 "추출 실패"로 표시, 원본·confidence 보존, 수정은 edited_text 로 따로
규칙 3: bbox 를 모르면 null
규칙 4: 설정은 .env 에, .env 는 .gitignore 에, .env.example 은 같은 항목
"""
import re
from pathlib import Path

import pytest

from app.config import BASE_DIR, Settings
from app.pipeline import runner
from app.pipeline.process import process_block
from app.pipeline.processors.common import ProcessContext
from app.pipeline.storage import read_json, write_json
from app.pipeline.structure import IdentityChangedError, identity_of, set_block_status
from tests import fakes, samples
from tests.fakes import FakeEngines

ROOT = BASE_DIR.parent


# ===================== 규칙 1 =====================


def test_rule1_retry_keeps_block_id_and_order(tmp_path, run_doc, settings, monkeypatch):
    monkeypatch.setitem(fakes._flaky_state, "failed_once", False)
    engines = FakeEngines(flaky=True)  # 첫 처리에서 p1-b002 만 실패
    doc_id, _, st, job = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", engines)
    before = identity_of(read_json(st.structure_json)["blocks"])
    assert read_json(st.block_result("p1-b002"))["status"] == "failed"

    runner.run_block_retry({**job, "block_id": "p1-b002"}, engines, settings)

    after_structure = read_json(st.structure_json)
    assert identity_of(after_structure["blocks"]) == before  # 번호·순서·위치 그대로
    r = read_json(st.block_result("p1-b002"))
    assert (r["block_id"], r["order"], r["status"], r["attempts"]) == ("p1-b002", 2, "done", 2)


def test_rule1_failed_block_stays_in_place(tmp_path, run_doc, monkeypatch):
    """실패한 블록을 지우거나 건너뛰어 번호를 당기지 않는다."""
    monkeypatch.setitem(fakes._flaky_state, "failed_once", False)
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines(flaky=True))
    structure = read_json(st.structure_json)
    assert [b["block_id"] for b in structure["blocks"]] == [f"p1-b00{i}" for i in range(1, 7)]
    txt = st.output("txt").read_text(encoding="utf-8")
    assert txt.index("테스트 문서 제목") < txt.index("[추출 실패: p1-b002]") < txt.index("[추출 실패: p1-b003]")


def test_rule1_edit_keeps_identity(tmp_path, run_doc, settings):
    _, _, st, job = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    before = identity_of(read_json(st.structure_json)["blocks"])
    runner.apply_edit(settings, {**job, "block_id": "p1-b003"}, "사람이 고친 글자")
    assert identity_of(read_json(st.structure_json)["blocks"]) == before


def test_rule1_set_block_status_cannot_change_identity(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    # 정상: status 만 바꾸는 것은 허용
    set_block_status(st, {"p1-b001": "pending"})
    # 처리기가 실수로 order 를 바꾼 결과를 만들었다고 가정 → 저장하지 않고 오류
    structure = read_json(st.structure_json)
    block = dict(structure["blocks"][0])

    class BadEngines(FakeEngines):
        pass

    ctx = ProcessContext(settings=Settings(_env_file=None), engines=BadEngines(), storage=st, structure=structure)
    from app.pipeline import process as process_mod

    original = process_mod.PROCESSORS["title"]
    try:
        process_mod.PROCESSORS["title"] = lambda c, b: {**original(c, b), "order": 99}
        with pytest.raises(IdentityChangedError):
            process_block(ctx, block)
    finally:
        process_mod.PROCESSORS["title"] = original
    assert read_json(st.block_result("p1-b001"))["order"] == 1  # 잘못된 결과는 저장되지 않았다


# ===================== 규칙 2 =====================


def test_rule2_low_confidence_marked_failed_with_raw_kept(tmp_path, run_doc, settings):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    r = read_json(st.block_result("p1-b003"))
    assert r["status"] == "failed" and r["failure_reason"] == "low_confidence"
    assert r["text"] is None  # 최종 텍스트로 쓰지 않음
    assert r["raw_text"] == "흐릿한 글자" and r["confidence"] == 0.42  # 원본과 점수는 그대로 보존
    assert "[추출 실패: p1-b003]" in st.output("txt").read_text(encoding="utf-8")
    assert "흐릿한 글자" not in st.output("txt").read_text(encoding="utf-8")


def test_rule2_empty_result_marked_failed(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    r = read_json(st.block_result("p1-b004"))
    assert r["status"] == "failed" and r["failure_reason"] == "empty"
    assert "[추출 실패: p1-b004]" in st.output("txt").read_text(encoding="utf-8")


def test_rule2_threshold_comes_from_settings(tmp_path, run_doc, settings):
    settings.OCR_CONFIDENCE_THRESHOLD = 0.4  # 기준을 낮추면 0.42 는 통과해야 한다
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    assert read_json(st.block_result("p1-b003"))["text"] == "흐릿한 글자"


def test_rule2_edit_saved_separately_original_kept(tmp_path, run_doc, settings):
    _, file_id, st, job = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    runner.apply_edit(settings, {**job, "block_id": "p1-b003"}, "선명한 글자")
    r = read_json(st.block_result("p1-b003"))
    assert r["edited_text"] == "선명한 글자"
    assert r["raw_text"] == "흐릿한 글자" and r["confidence"] == 0.42 and r["status"] == "failed"  # 원본은 그대로
    txt = st.output("txt").read_text(encoding="utf-8")
    assert "선명한 글자" in txt and "[추출 실패: p1-b003]" not in txt
    # 수정 취소(None) → 다시 실패 표시
    runner.apply_edit(settings, {**job, "block_id": "p1-b003"}, None)
    assert "[추출 실패: p1-b003]" in st.output("txt").read_text(encoding="utf-8")


def test_rule2_no_text_correction_code():
    """맞춤법 교정·사전 보정·LLM 후처리 라이브러리를 쓰지 않는지 소스 전체에서 확인한다."""
    banned = re.compile(r"\b(hanspell|symspell|spellchecker|autocorrect|openai|anthropic|transformers)\b", re.I)
    for path in (BASE_DIR / "app").rglob("*.py"):
        assert not banned.search(path.read_text(encoding="utf-8")), path


# ===================== 규칙 3 =====================


def test_rule3_hwp_blocks_bbox_null_in_structure_json(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_hwpx(tmp_path / "d.hwpx"), "hwpx", engines=None)
    raw = st.structure_json.read_text(encoding="utf-8")
    assert '"bbox": null' in raw
    structure = read_json(st.structure_json)
    assert all(b["bbox"] is None for b in structure["blocks"])
    assert all(read_json(st.block_result(b["block_id"]))["bbox"] is None for b in structure["blocks"])
    assert not any(st.block_image(b["block_id"]).exists() for b in structure["blocks"])  # 자를 영역이 없으니 crop 도 없다


# ===================== 규칙 4 =====================


def _env_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_rule4_backend_env_example_matches_settings():
    example = _env_keys(BASE_DIR / ".env.example")
    fields = set(Settings.model_fields)
    assert example == fields, f"빠진 항목: {fields - example}, 모르는 항목: {example - fields}"


def test_rule4_frontend_env_example_matches_code():
    used = set()
    for path in (ROOT / "frontend" / "src").rglob("*.js*"):
        # config.js 는 const env = import.meta.env 로 받아 env.VITE_... 로 쓰므로, VITE_ 이름 전체를 찾는다
        used |= set(re.findall(r"\b(VITE_[A-Z0-9_]+)\b", path.read_text(encoding="utf-8")))
    example = _env_keys(ROOT / "frontend" / ".env.example")
    assert used and used <= example, f".env.example 에 없는 항목: {used - example}"


def test_rule4_env_files_are_gitignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "storage/", "logs/", "data/*.db"):
        assert any(line.strip().endswith(pattern) or line.strip() == pattern for line in ignore), pattern


def test_rule4_limits_not_hardcoded():
    """
    시간 제한(30), 파일 크기(5MB) 같은 값을 config.py 밖의 "코드"에 직접 쓰지 않았는지 확인한다.
    설명용 주석·docstring 은 제외하고, 실제 코드의 숫자와 문자열만 본다 (ast = 파이썬 코드를 구조로 읽는 표준 모듈).
    """
    import ast

    defaults = Settings(_env_file=None)
    banned_numbers = {defaults.PROCESS_TIMEOUT_SEC, defaults.max_file_size_bytes}
    banned_text = re.compile(rf"\b{defaults.MAX_FILE_SIZE_MB}\s*MB|\b{defaults.PROCESS_TIMEOUT_SEC}\s*(초|s)\b", re.I)
    for path in (BASE_DIR / "app").rglob("*.py"):
        if path.name == "config.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) not in docstrings:
                if isinstance(node.value, int) and not isinstance(node.value, bool):
                    assert node.value not in banned_numbers, f"{path.name}:{node.lineno}: 숫자 {node.value}"
                if isinstance(node.value, str):
                    assert not banned_text.search(node.value), f"{path.name}:{node.lineno}: {node.value!r}"
