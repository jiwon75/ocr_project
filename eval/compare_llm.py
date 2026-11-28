"""
[이 파일이 하는 일]
여러 LLM 모델(Ollama)의 요약·오타 교정 능력을 같은 문서·같은 조건으로 비교해 표로 만든다.

  python eval/compare_llm.py --docs "문서 폴더"                       → ollama list 의 모든 모델 비교
  python eval/compare_llm.py --docs "문서 폴더" --models gemma4:e4b hf.co/...   → 고른 모델만

  문서 폴더: eval/make_docs.py 로 만든 문서 5개 + answers.json 이 있는 곳
  결과:      --out 폴더 (안 주면 문서 폴더/비교결과_날짜시각/) 에 요약표.md, 상세.csv, 모델별/*.json

[왜 이렇게 만들었는지]
- 화면의 AI 도우미와 똑같은 함수(app/assist/core.py 의 summarize, suggest_corrections)를 그대로 부른다.
  이 함수들은 "LLM 을 부르는 함수(chat)"를 인자로 받으므로 모델 이름만 바꿔 넣으면 된다.
  → 프롬프트·청크 크기·제안 검사 규칙이 실제 서비스와 같아서, 여기서 좋은 모델이 실제로도 좋다.
- OCR 은 모델과 상관없으므로 한 번만 하고, 모든 모델이 같은 OCR 결과를 받는다 (공정한 비교).
- 모델마다 첫 요청(모델을 GPU 에 올리는 시간)을 따로 재서 처리 시간에 섞이지 않게 한다.
  다 쓴 모델은 내려서(unload) 다음 모델이 GPU 메모리를 온전히 쓰게 한다.
- [규칙 2] 교정 제안은 점수를 매기려고 "복사본"에만 적용한다. 실제 결과·DB 는 건드리지 않는다.
- 실행 결과는 프로젝트 폴더 밖에만 쓴다 (테스트 결과가 git 에 섞이지 않게). OCR 중간 파일은 임시 폴더에 두고 지운다.

점수
- 요약: 핵심 사실(answers.json 의 key_facts, 날짜·금액 등)이 요약에 몇 개 들어 있나 / 원문에 없는 숫자가 몇 개 나왔나(지어낸 내용)
- 교정: 제안을 하나씩 적용해 정답과의 글자 차이가 줄면 "맞게 고침", 늘면 "잘못 고침", 그대로면 "효과 없음"
        모든 제안을 적용했을 때 글자 오류율(CER, 정답 대비 틀린 글자 비율)이 얼마나 바뀌나
        디지털 PDF(OCR 을 안 거쳐 오타가 없음)에 낸 제안은 모두 불필요한 제안
"""
import argparse
import copy
import csv
import difflib
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402  (위에서 backend 를 import 경로에 넣은 뒤에 불러와야 한다)
from app.assist import core  # noqa: E402
from app.assist.ollama import LLMError, chat  # noqa: E402
from app.config import Settings  # noqa: E402
from app.pipeline import runner  # noqa: E402
from app.pipeline.merge import load_results, merge_document  # noqa: E402
from app.pipeline.storage import DocStorage, read_json  # noqa: E402

RULES_ONLY = "(규칙만, LLM 없음)"  # 비교 기준: LLM 없이 코드 규칙만으로 낸 교정 제안
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ---------------------------------------------------------------------
# 글자 비교 도우미
# ---------------------------------------------------------------------


def squeeze(text: str) -> str:
    """공백·줄바꿈을 모두 뺀다. 줄바꿈 위치나 띄어쓰기 차이는 오류로 세지 않기 위해서다."""
    return "".join(text.split())


def char_errors(ref: str, hyp: str) -> int:
    """정답(ref)과 결과(hyp)가 다른 글자 수 (바뀐·빠진·더해진 글자). 공백은 무시한다."""
    ref, hyp = squeeze(ref), squeeze(hyp)
    ops = difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes()
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in ops if op != "equal")


def fact_found(fact: str, summary: str) -> bool:
    """핵심 사실이 요약에 있나. 띄어쓰기와 천 단위 쉼표 차이(1,250만 / 1250만)는 같은 것으로 본다."""
    s = squeeze(summary)
    f = squeeze(fact)
    return f in s or f.replace(",", "") in s.replace(",", "")


def invented_numbers(summary: str, *sources: str) -> list[str]:
    """
    요약에 나온 숫자 중 정답·OCR 결과 어디에도 없는 것 = 모델이 지어낸 숫자 (두 자리 이상만. "1." 같은 번호는 빼려고).
    OCR 이 잘못 읽은 숫자(예: "i5 16GB" → "516GB")를 모델이 그대로 옮긴 것은 모델 탓이 아니므로 세지 않는다.
    """
    known = {n.replace(",", "").rstrip(".") for src in sources for n in NUMBER_RE.findall(src)}
    found = [n.replace(",", "").rstrip(".") for n in NUMBER_RE.findall(summary)]
    return sorted({n for n in found if len(n) >= 2 and n not in known})


# ---------------------------------------------------------------------
# 1) OCR (한 번만)
# ---------------------------------------------------------------------


def run_ocr(docs_dir: Path, answers: list[dict], work: Path) -> dict:
    """문서를 실제 OCR 파이프라인으로 처리한다. 돌려주는 값: {파일명: {"structure", "results", "seconds", "status"}}"""
    from app.pipeline.engines import load_engines

    # 임시 폴더를 쓰는 설정. 실제 storage/·DB 를 더럽히지 않는다. 비교용이라 문서당 시간 제한은 넉넉히 준다
    settings = Settings(STORAGE_DIR=work / "storage", DB_PATH=work / "app.db", LOG_DIR=work / "logs", PROCESS_TIMEOUT_SEC=600)
    db.init_db(settings.DB_PATH)
    print("OCR 모델 불러오는 중...")
    engines = load_engines(settings)
    out = {}
    for a in answers:
        src = docs_dir / a["file"]
        ftype = src.suffix[1:].lower()
        doc_id = str(uuid.uuid4())
        st = DocStorage(settings.STORAGE_DIR, doc_id)
        st.create_dirs()
        shutil.copy(src, st.original(ftype))
        file_id = db.insert_uploaded_file(settings.DB_PATH, doc_id, a["file"], str(st.original(ftype)), ftype)
        start = time.perf_counter()
        runner.run_document({"doc_id": doc_id, "file_id": file_id, "file_name": a["file"], "file_type": ftype,
                             "file_path": str(st.original(ftype)), "language": "auto"}, engines, settings)
        seconds = time.perf_counter() - start
        status = read_json(st.status_json)["state"]
        structure = read_json(st.structure_json) if status == "done" else None
        out[a["file"]] = {"structure": structure, "results": load_results(st, structure) if structure else {},
                          "seconds": round(seconds, 1), "status": status}
        print(f"  OCR {a['file']}: {status} ({seconds:.1f}s)")
    return out


def ocr_text(structure: dict, results: dict, settings: Settings) -> str:
    """화면의 .txt 결과와 같은 규칙으로 합친 글자 (페이지 구분선은 정답에 없으므로 뺀다)."""
    return merge_document(structure, results, "txt", page_separator=False, debug=False,
                          skip_page_furniture=settings.MERGE_SKIP_PAGE_FURNITURE)


# ---------------------------------------------------------------------
# 2) 모델 하나 평가
# ---------------------------------------------------------------------


def ollama_models(settings: Settings) -> list[str]:
    with urllib.request.urlopen(settings.OLLAMA_URL.rstrip("/") + "/api/tags", timeout=10) as res:
        return [m["name"] for m in json.load(res).get("models", [])]


def unload(settings: Settings, model: str) -> None:
    """모델을 GPU·메모리에서 내린다 (keep_alive=0). 다음 모델이 메모리를 온전히 쓰게 해서 속도 비교를 공정하게 한다."""
    body = json.dumps({"model": model, "keep_alive": 0}).encode()
    req = urllib.request.Request(settings.OLLAMA_URL.rstrip("/") + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except OSError:
        pass  # 내리기 실패는 비교 결과에 영향이 작아서 무시한다


def score_corrections(suggestions: list[dict], structure: dict, results: dict, answer: str, settings: Settings) -> dict:
    """제안을 복사본에 하나씩 / 전부 적용해 정답과의 글자 차이 변화를 잰다. [규칙 2] 원본 results 는 바꾸지 않는다."""
    before = char_errors(answer, ocr_text(structure, results, settings))
    good = bad = neutral = 0
    for s in suggestions:
        trial = copy.deepcopy(results)
        core.apply_to_result(trial[s["block_id"]], s["id"], s["wrong"], s["right"])
        delta = char_errors(answer, ocr_text(structure, trial, settings)) - before
        s["effect"] = "맞게 고침" if delta < 0 else "잘못 고침" if delta > 0 else "효과 없음"
        good, bad, neutral = good + (delta < 0), bad + (delta > 0), neutral + (delta == 0)
    all_applied = copy.deepcopy(results)
    for s in suggestions:
        core.apply_to_result(all_applied[s["block_id"]], s["id"], s["wrong"], s["right"])
    after = char_errors(answer, ocr_text(structure, all_applied, settings))
    n = len(squeeze(answer))
    return {"good": good, "bad": bad, "neutral": neutral, "errors_before": before, "errors_after": after,
            "cer_before": round(before / n * 100, 2), "cer_after": round(after / n * 100, 2)}


def evaluate_model(model: str, ocr: dict, answers: list[dict], base: Settings) -> dict:
    """모델 하나로 문서 5개를 요약·교정하고 점수를 매긴다."""
    settings = base.model_copy(update={"OLLAMA_MODEL": model})
    rules_only = model == RULES_ONLY
    ask = (lambda system, user, json_mode=False: "{}") if rules_only else (
        lambda system, user, json_mode=False: chat(settings, system, user, json_mode))
    report = {"model": model, "load_seconds": None, "docs": []}

    if not rules_only:
        print(f"\n[{model}] 모델 불러오는 중 (첫 요청)...")
        start = time.perf_counter()
        try:
            ask("한 단어로만 답해.", "준비됐니?")
        except LLMError as e:
            report["error"] = str(e)
            print(f"  ✗ {e}")
            return report
        report["load_seconds"] = round(time.perf_counter() - start, 1)
        print(f"  첫 로딩 {report['load_seconds']}s")

    for a in answers:
        o = ocr[a["file"]]
        doc = {"file": a["file"], "scanned": "스캔" in a["file"]}
        if o["status"] != "done":
            doc["error"] = f"OCR 실패 ({o['status']})"
            report["docs"].append(doc)
            continue
        structure, results = o["structure"], o["results"]

        # 요약 (규칙만 기준은 요약을 하지 않는다)
        if not rules_only:
            start = time.perf_counter()
            try:
                out = core.summarize(ask, core.document_text(structure, results), settings.LLM_CHUNK_CHARS)
                summary = out["summary"]
                doc["summary"] = summary
                doc["summary_seconds"] = round(time.perf_counter() - start, 1)
                doc["facts_found"] = [f for f in a["key_facts"] if fact_found(f, summary)]
                doc["facts_missing"] = [f for f in a["key_facts"] if not fact_found(f, summary)]
                doc["facts_total"] = len(a["key_facts"])
                doc["invented_numbers"] = invented_numbers(summary, a["text"], ocr_text(structure, results, settings))
            except LLMError as e:
                doc["summary_error"] = str(e)

        # 교정
        start = time.perf_counter()
        try:
            out = core.suggest_corrections(ask, core.collect_units(structure, results), settings.LLM_CHUNK_CHARS,
                                           settings.LLM_CORRECTION_MIN_SIMILARITY)
            doc["correct_seconds"] = round(time.perf_counter() - start, 1)
            doc["suggestions"] = out["suggestions"]
            doc["rejected"] = out["rejected"]
            doc["correction"] = score_corrections(out["suggestions"], structure, results, a["text"], settings)
        except LLMError as e:
            doc["correct_error"] = str(e)

        c = doc.get("correction") or {}
        summary_part = "" if rules_only else (
            f"요약 {doc.get('summary_seconds', '-')}s 핵심사실 {len(doc.get('facts_found', []))}/{len(a['key_facts'])} | ")
        print(f"  {a['file']}: {summary_part}교정 {doc.get('correct_seconds', '-')}s 제안 {len(doc.get('suggestions', []))}"
              f" (맞음 {c.get('good', '-')} / 잘못 {c.get('bad', '-')}) CER {c.get('cer_before', '-')}→{c.get('cer_after', '-')}%")
        report["docs"].append(doc)

    if not rules_only:
        unload(settings, model)
    return report


# ---------------------------------------------------------------------
# 3) 결과 정리
# ---------------------------------------------------------------------


def totals(report: dict) -> dict:
    """모델 하나의 문서 5개 점수를 합친다."""
    docs = [d for d in report["docs"] if "error" not in d]
    summed = [d for d in docs if "summary" in d]
    corr = [d for d in docs if "correction" in d]
    scanned = [d for d in corr if d["scanned"]]
    digital = [d for d in corr if not d["scanned"]]
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    found = sum(len(d["facts_found"]) for d in summed)
    total = sum(d["facts_total"] for d in summed)
    err_before = sum(d["correction"]["errors_before"] for d in scanned)
    err_after = sum(d["correction"]["errors_after"] for d in scanned)
    return {
        "load_seconds": report.get("load_seconds"),
        "summary_seconds": avg([d["summary_seconds"] for d in summed]),
        "facts": f"{found}/{total}" if total else "-",
        "facts_pct": round(found / total * 100) if total else None,
        "invented": sum(len(d["invented_numbers"]) for d in summed) if summed else None,
        "correct_seconds": avg([d["correct_seconds"] for d in corr]),
        "suggestions": sum(len(d["suggestions"]) for d in corr),
        "good": sum(d["correction"]["good"] for d in corr),
        "bad": sum(d["correction"]["bad"] for d in corr),
        "neutral": sum(d["correction"]["neutral"] for d in corr),
        "scan_errors": f"{err_before}→{err_after}" if scanned else "-",
        "digital_needless": sum(len(d["suggestions"]) for d in digital),
        "failures": sum(1 for d in report["docs"] if any(k in d for k in ("error", "summary_error", "correct_error"))),
    }


def write_outputs(out: Path, reports: list[dict], ocr: dict, answers: list[dict], base: Settings, started: datetime) -> None:
    (out / "모델별").mkdir(parents=True, exist_ok=True)
    for r in reports:
        safe = re.sub(r"[^\w.-]+", "_", r["model"]).strip("_") or "model"
        (out / "모델별" / f"{safe}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    ocr_dump = {f: {"status": o["status"], "seconds": o["seconds"],
                    "text": ocr_text(o["structure"], o["results"], base) if o["structure"] else None} for f, o in ocr.items()}
    (out / "OCR결과.json").write_text(json.dumps(ocr_dump, ensure_ascii=False, indent=2), encoding="utf-8")

    # 상세.csv: 모델 × 문서 한 줄씩. utf-8-sig 는 엑셀에서 한글이 깨지지 않게 하는 인코딩
    with open(out / "상세.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["모델", "문서", "요약 시간(s)", "핵심 사실", "빠진 사실", "원문에 없는 숫자", "교정 시간(s)", "제안 수",
                    "맞게 고침", "잘못 고침", "효과 없음", "버린 제안", "CER 전(%)", "CER 후(%)", "오류"])
        for r in reports:
            for d in r["docs"]:
                c = d.get("correction") or {}
                w.writerow([r["model"], d["file"], d.get("summary_seconds", ""),
                            f"{len(d['facts_found'])}/{d['facts_total']}" if "facts_found" in d else "",
                            ", ".join(d.get("facts_missing", [])), ", ".join(d.get("invented_numbers", [])),
                            d.get("correct_seconds", ""), len(d.get("suggestions", [])), c.get("good", ""), c.get("bad", ""),
                            c.get("neutral", ""), d.get("rejected", ""), c.get("cer_before", ""), c.get("cer_after", ""),
                            d.get("error") or d.get("summary_error") or d.get("correct_error") or r.get("error", "")])

    # 요약표.md: 한눈에 보는 표 + 문서별 요약문 + 교정 제안 목록
    lines = [f"# LLM 모델 비교 결과 ({started:%Y-%m-%d %H:%M})", "",
             f"- 문서 {len(answers)}개, 청크 {base.LLM_CHUNK_CHARS}자, 교정 유사도 기준 {base.LLM_CORRECTION_MIN_SIMILARITY}",
             "- OCR 처리 시간: " + ", ".join(f"{f} {o['seconds']}s" for f, o in ocr.items()), "",
             "## 한눈에 보기", "",
             "| 모델 | 첫 로딩(s) | 요약(s/문서) | 핵심 사실 | 지어낸 숫자 | 교정(s/문서) | 제안 | 맞게 고침 | 잘못 고침 | 효과 없음 | 스캔 문서 틀린 글자 (교정 전→후) | 디지털 문서 불필요 제안 | 실패 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in reports:
        if "error" in r:
            lines.append(f"| {r['model']} | 실패: {r['error']} |" + " |" * 11)
            continue
        t = totals(r)
        dash = lambda v: "-" if v is None else v  # noqa: E731
        facts = f"{t['facts']} ({t['facts_pct']}%)" if t["facts_pct"] is not None else "-"
        lines.append(f"| {r['model']} | {dash(t['load_seconds'])} | {dash(t['summary_seconds'])} | {facts} | {dash(t['invented'])} | "
                     f"{dash(t['correct_seconds'])} | {t['suggestions']} | {t['good']} | {t['bad']} | {t['neutral']} | "
                     f"{t['scan_errors']} | {t['digital_needless']} | {t['failures']} |")
    lines += ["", "읽는 법",
              "- **핵심 사실**: 요약에 날짜·금액 같은 중요한 내용이 몇 개 들어갔나 (높을수록 좋음)",
              "- **지어낸 숫자**: 요약에 나왔지만 원문에 없는 숫자 개수 (0 이어야 좋음)",
              "- **맞게/잘못 고침**: 교정 제안을 하나씩 적용했을 때 정답에 가까워졌나/멀어졌나. **잘못 고침이 적은 것이 더 중요**",
              "- **스캔 문서 틀린 글자**: 스캔 문서 정답 대비 틀린 글자 수. 표 칸 순서 차이도 포함돼서 0 이 될 수는 없음",
              "- **디지털 문서 불필요 제안**: 오타가 없는 문서에 낸 제안 (0 이어야 좋음)",
              f"- **{RULES_ONLY}**: LLM 없이 코드 규칙만으로 낸 제안. 모델 점수는 이것보다 나아야 의미가 있음", ""]
    for a in answers:
        lines += [f"## {a['file']}", "", f"> {a['purpose']}", "", f"핵심 사실: {', '.join(a['key_facts'])}", ""]
        for r in reports:
            d = next((x for x in r["docs"] if x["file"] == a["file"]), None)
            if not d or r["model"] == RULES_ONLY and not d.get("suggestions"):
                continue
            lines.append(f"### {r['model']}")
            if "summary" in d:
                lines += ["", f"**요약** ({d['summary_seconds']}s, 핵심 사실 {len(d['facts_found'])}/{d['facts_total']}"
                              + (f", 빠짐: {', '.join(d['facts_missing'])}" if d["facts_missing"] else "")
                              + (f", 원문에 없는 숫자: {', '.join(d['invented_numbers'])}" if d["invented_numbers"] else "") + ")",
                          "", d["summary"] or "(빈 요약)"]
            if d.get("suggestions"):
                lines += ["", "**교정 제안**", "", "| 위치 | 틀린 글자 | 고친 글자 | 결과 |", "|---|---|---|---|"]
                lines += [f"| {s['id']} | {s['wrong']} | {s['right']} | {s['effect']} |" for s in d["suggestions"]]
            for key in ("error", "summary_error", "correct_error"):
                if key in d:
                    lines += ["", f"⚠ {d[key]}"]
            lines.append("")
    (out / "요약표.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="LLM 모델 요약·교정 비교")
    parser.add_argument("--docs", type=Path, required=True, help="make_docs.py 로 만든 문서 + answers.json 이 있는 폴더")
    parser.add_argument("--models", nargs="*", help="비교할 모델 (ollama list 이름). 안 주면 설치된 모델 전부")
    parser.add_argument("--out", type=Path, help="결과 폴더 (기본: 문서 폴더/비교결과_날짜시각)")
    args = parser.parse_args()

    started = datetime.now()
    docs_dir = args.docs.resolve()
    answers = json.loads((docs_dir / "answers.json").read_text(encoding="utf-8"))
    out = (args.out or docs_dir / f"비교결과_{started:%Y-%m-%d_%H%M}").resolve()
    if out == ROOT or ROOT in out.parents:
        # 실행 결과가 git 에 섞이지 않도록 프로젝트 폴더 안에는 쓰지 않는다
        parser.error(f"결과 폴더는 프로젝트 밖이어야 합니다: {out}")

    base = Settings()  # backend/.env (Ollama 주소, 청크 크기 등) [규칙 4]
    installed = ollama_models(base)
    models = args.models or installed
    missing = [m for m in models if m not in installed and f"{m}:latest" not in installed]
    if missing:
        parser.error(f"Ollama 에 없는 모델: {missing} (ollama pull 로 받거나 ollama list 로 이름 확인)")
    print(f"비교할 모델 {len(models)}개: {', '.join(models)}")

    work = Path(tempfile.mkdtemp(prefix="ocr-compare-"))
    try:
        ocr = run_ocr(docs_dir, answers, work)
        reports = [evaluate_model(RULES_ONLY, ocr, answers, base)]
        for model in models:
            reports.append(evaluate_model(model, ocr, answers, base))
            write_outputs(out, reports, ocr, answers, base, started)  # 모델 하나 끝날 때마다 저장 (중간에 멈춰도 남게)
        write_outputs(out, reports, ocr, answers, base, started)
    finally:
        shutil.rmtree(work, ignore_errors=True)  # OCR 중간 파일은 지운다
    print(f"\n결과 → {out}")


if __name__ == "__main__":
    main()
