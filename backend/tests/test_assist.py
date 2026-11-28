"""
[LLM 도우미 테스트] Ollama 없이 가짜 chat 함수로 요약·교정 제안·적용을 확인한다.
- 제안 검사(엉뚱한 단어, 숫자 자릿수 변경 거르기)
- 긴 문서 나눠 보내기(청크)
- [규칙 2] 수락한 제안은 edited_text(블록/표 칸)에만 저장되고 원본은 그대로
- Ollama 가 꺼져 있어도 API 가 안내 문장만 돌려주고 다른 기능은 그대로
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.assist import core, jobs, ollama
from app.main import create_app
from app.pipeline import runner
from app.pipeline.merge import load_results, merge_document
from app.pipeline.storage import read_json, write_json
from tests import samples
from tests.fakes import FakeEngines
from tests.test_runtime import _upload, _wait, _wait_ready

UNITS = {u["id"]: u for u in [
    {"id": "a", "block_id": "p1-b001", "text": "짧뽕 곱빼기"},
    {"id": "b", "block_id": "p1-b002", "text": "1DD"},
    {"id": "c", "block_id": "p1-b003", "text": "가걱은 8,000원"},
]}


@pytest.mark.parametrize("item, ok", [
    ({"id": "a", "wrong": "짧뽕", "right": "짬뽕"}, True),  # 받침 하나 다른 한글
    ({"id": "b", "wrong": "1DD", "right": "100"}, True),  # 숫자 자리의 D → 0
    ({"id": "c", "wrong": "가걱", "right": "가격"}, True),
    ({"id": "a", "wrong": "짧뽕", "right": "떡볶이"}, False),  # 전혀 다른 단어
    ({"id": "b", "wrong": "1DD", "right": "1D"}, False),  # 자릿수가 바뀜
    ({"id": "c", "wrong": "8,000", "right": "80,00"}, False),  # 쉼표 위치가 바뀜
    ({"id": "a", "wrong": "없는글자", "right": "없는 글자"}, False),  # 원문에 없음
    ({"id": "z", "wrong": "짧뽕", "right": "짬뽕"}, False),  # 없는 조각
    ({"id": "a", "wrong": "짧뽕", "right": "짧뽕"}, False),  # 바뀐 것 없음
])
def test_validate_suggestion(item, ok):
    assert (core.validate_suggestion(item, UNITS, 0.6) is None) == ok


@pytest.mark.parametrize("text, expected", [
    ("미국나스닥1DD+15%", [("1DD", "100")]),
    ("가격 5OO원, 1O5번", [("5OO", "500"), ("1O5", "105")]),
    ("3D 프린터 10G 요금 B2B", []),  # 숫자 뒤 영문자 하나·영어 단어는 건드리지 않는다
    ("DO8 2026년", []),  # 숫자로 시작하지 않음
])
def test_rule_suggestions(text, expected):
    out = core.rule_suggestions([{"id": "a", "block_id": "p1-b001", "text": text}])
    assert [(s["wrong"], s["right"]) for s in out] == expected


def test_chunk_texts_keeps_order_and_limit():
    chunks = core.chunk_texts(["a" * 40, "b" * 40, "c" * 40, "d" * 200], 100)
    assert chunks == [["a" * 40, "b" * 40], ["c" * 40], ["d" * 200]]


def test_summarize_short_and_long():
    calls = []

    def chat(system, user, json_mode=False):
        calls.append(user)
        return f"요약{len(calls)}"

    assert core.summarize(chat, "짧은 글", 100) == {"summary": "요약1", "chunks": 1}
    calls.clear()
    long_text = "\n\n".join(f"문단{i} " + "가" * 60 for i in range(5))
    out = core.summarize(chat, long_text, 100)
    assert out["chunks"] == 5 and len(calls) == 6  # 부분 요약 5번 + 합치기 1번
    assert out["summary"] == "요약6"


def test_suggest_corrections_chunks_and_filters():
    units = list(UNITS.values()) + [{"id": "n", "block_id": "p1-b009", "text": "12,500"}]
    seen_chunks = []

    def chat(system, user, json_mode=False):
        assert json_mode
        seen_chunks.append(user)
        ids = [json.loads(line)["id"] for line in user.splitlines()]
        out = []
        if "a" in ids:
            out += [{"id": "a", "wrong": "짧뽕", "right": "떡볶이", "reason": "엉뚱함"},  # 버려져도 아래 올바른 제안은 남는다
                    {"id": "a", "wrong": "짧뽕", "right": "짬뽕", "reason": "음식 이름"},
                    {"id": "b", "wrong": "1DD", "right": "1OO", "reason": "규칙 제안과 같은 곳 → 버림"}]
        if "c" in ids:
            out.append({"id": "c", "wrong": "가걱", "right": "가격", "reason": "받침"})
        return "설명 문장 " + json.dumps({"corrections": out}, ensure_ascii=False)  # JSON 앞에 군말이 붙어도 읽는다

    out = core.suggest_corrections(chat, units, 40, 0.6)
    assert out["chunks"] == len(seen_chunks) >= 2  # 작은 청크 크기 → 나눠서 보냄
    assert not any('"n"' in c for c in seen_chunks)  # 숫자·기호뿐인 칸은 LLM 에 보내지 않는다
    # 규칙 제안(1DD → 100)이 먼저, 그다음 LLM 제안 순서
    assert [(s["id"], s["after"]) for s in out["suggestions"]] == [("b", "100"), ("a", "짬뽕 곱빼기"), ("c", "가격은 8,000원")]
    assert out["rejected"] == 1  # 짧뽕 → 떡볶이


def test_suggest_corrections_bad_json_is_empty():
    out = core.suggest_corrections(lambda *a, **k: "모르겠어요", list(UNITS.values()), 1000, 0.6)
    assert [s["wrong"] for s in out["suggestions"]] == ["1DD"] and out["rejected"] == 0  # 규칙 제안만 남는다


# ---------------- 실제 문서 결과에 적용 ----------------


def _fake_corrections(storage, suggestions):
    data = {"state": "done", "suggestions": [{"sid": i + 1, "applied": False, "reason": "", **s} for i, s in enumerate(suggestions)]}
    write_json(storage.assist_json("corrections"), data)


def test_collect_units_and_apply_keeps_original(tmp_path, run_doc, settings):
    _, _, st, job = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    structure = read_json(st.structure_json)
    units = core.collect_units(structure, load_results(st, structure))
    by_id = {u["id"]: u for u in units}
    assert by_id["p1-b002"]["text"] == "첫 번째 문단입니다."
    assert by_id["p1-b003"]["text"] == "흐릿한 글자"  # 신뢰도 미달 블록도 원본으로 제안 대상
    cell = next(u for u in units if ":r" in u["id"])

    _fake_corrections(st, [
        {"id": "p1-b002", "block_id": "p1-b002", "wrong": "문단", "right": "문장"},
        {"id": cell["id"], "block_id": cell["block_id"], "wrong": cell["text"], "right": cell["text"] + "!"},
        {"id": "p1-b002", "block_id": "p1-b002", "wrong": "없는", "right": "있는"},  # 지금 글자에 없음 → 건너뜀
    ])
    out = runner.apply_corrections(settings, job, [1, 2, 3])
    assert out["applied"] == [1, 2] and out["skipped"] == [3]

    r = read_json(st.block_result("p1-b002"))
    assert r["edited_text"] == "첫 번째 문장입니다." and r["text"] == "첫 번째 문단입니다."  # [규칙 2] 원본 유지
    table = read_json(st.block_result(cell["block_id"]))
    edited_cell = next(c for c in table["table"]["cells"] if c.get("edited_text"))
    assert edited_cell["edited_text"] == cell["text"] + "!" and edited_cell["raw_text"]
    txt = st.output("txt").read_text(encoding="utf-8")
    assert "첫 번째 문장입니다." in txt and cell["text"] + "!" in txt  # 다시 합쳐짐 (표는 탭 구분 그대로)

    # 이미 적용한 제안은 다시 적용하지 않는다
    assert runner.apply_corrections(settings, job, [1])["applied"] == []
    # "원래대로" 는 표 칸 수정도 되돌린다
    runner.apply_edit(settings, {**job, "block_id": cell["block_id"]}, None)
    table = read_json(st.block_result(cell["block_id"]))
    assert not any("edited_text" in c for c in table["table"]["cells"])


def test_document_text_drops_markers(tmp_path, run_doc):
    _, _, st, _ = run_doc(samples.make_fake_page(tmp_path / "p.png"), "png", FakeEngines())
    structure = read_json(st.structure_json)
    results = load_results(st, structure)
    assert "[추출 실패" in merge_document(structure, results, "txt")
    text = core.document_text(structure, results)
    assert "[추출 실패" not in text and "[이미지" not in text and "첫 번째 문단입니다." in text


# ---------------- API ----------------


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, "tests.fakes:make_fake_engines")) as c:
        _wait_ready(c)
        yield c


def test_api_without_ollama(client, settings, tmp_path):
    settings.OLLAMA_URL = "http://127.0.0.1:9"  # 아무것도 없는 주소
    info = client.get("/api/assist/status").json()
    assert info["reachable"] is False and "Ollama" in info["message"]
    doc = _upload(client, samples.make_fake_page(tmp_path / "p.png"))
    _wait(client, doc["doc_id"])
    r = client.post(f"/api/documents/{doc['doc_id']}/assist/summary")
    assert r.status_code == 409 and "Ollama" in r.json()["detail"]
    assert client.get(f"/api/documents/{doc['doc_id']}/result").status_code == 200  # OCR 결과는 그대로


def test_api_summary_and_corrections_flow(client, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(ollama, "status", lambda s: {"reachable": True, "model_available": True, "message": None})

    def fake_chat(s, system, user, json_mode=False):
        if json_mode:
            return json.dumps({"corrections": [{"id": "p1-b002", "wrong": "문단", "right": "문장", "reason": "테스트"}]})
        return "가짜 요약입니다."

    monkeypatch.setattr(ollama, "chat", fake_chat)
    doc = _upload(client, samples.make_fake_page(tmp_path / "p.png"))
    _wait(client, doc["doc_id"])
    base = f"/api/documents/{doc['doc_id']}/assist"
    assert client.post(f"{base}/summary").status_code == 202
    assert client.post(f"{base}/corrections").status_code == 202
    assert client.post(f"{base}/nope").status_code == 404

    deadline = time.time() + 10
    while time.time() < deadline:
        data = client.get(base).json()
        if all(data[k] and data[k]["state"] != "running" for k in jobs.KINDS):
            break
        time.sleep(0.05)
    assert data["summary"]["state"] == "done" and data["summary"]["summary"] == "가짜 요약입니다."
    sugg = data["corrections"]["suggestions"]
    assert [(s["block_id"], s["after"]) for s in sugg] == [("p1-b002", "첫 번째 문장입니다.")]

    r = client.post(f"{base}/corrections/apply", json={"sids": [sugg[0]["sid"]]})
    assert r.status_code == 200 and r.json()["applied"] == [1]
    assert r.json()["corrections"]["suggestions"][0]["applied"] is True
    assert "첫 번째 문장입니다." in client.get(f"/api/documents/{doc['doc_id']}/result").text
    assert client.post(f"{base}/corrections/apply", json={"sids": []}).status_code == 422
