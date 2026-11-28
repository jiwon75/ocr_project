# 모델 비교용 문서

OCR·LLM(요약·오타 교정)·청킹을 비교하려고 만든 **가상 문서 5개**와 정답입니다. 내용은 모두 지어낸 것이라 캡처·공유해도 됩니다.

```bash
backend/.venv/bin/python eval/make_docs.py                    # eval/docs/ 에 만들기 (항상 똑같이 만들어짐)
backend/.venv/bin/python eval/make_docs.py --out "저장할 폴더"  # 원하는 폴더에 만들기 (예: 바탕화면 폴더)
```
WSL 에서 Windows 바탕화면은 `/mnt/c/Users/<사용자>/Desktop` (OneDrive 를 쓰면 `/mnt/c/Users/<사용자>/OneDrive/바탕 화면`) 입니다.

## 문서

| 파일 | 형식 | 비교 포인트 | OCR 결과 (이 PC, 2026-11-28) |
|---|---|---|---|
| `01_보안교육_공지_스캔.png` | 스캔 이미지 (흐림·기울기·잡음) | OCR 오타를 LLM이 고치는지, 짧은 공지 요약 | 오타 약 12개 (피싱→피성, 대응→대용, 노트북→노트복, 2417→241기 등) |
| `02_프로젝트_회의록.pdf` | 디지털 PDF 1쪽 | OCR 오류가 없는 상태에서 요약 품질 | 오류 없음 (텍스트 레이어 직접 추출) |
| `03_견적서_표_스캔.png` | 표 위주 스캔 이미지 | 숫자·금액(쉼표)을 바꾸지 않는지, 표 인식 | 오타 1개 (i5 누락), 합계 표 칸 순서 차이 |
| `04_한영혼합_보고서_스캔.pdf` | 스캔 PDF, 한국어+영어 | 언어 자동 인식, 영어 용어·숫자 교정 | 오타 많음 (문서→분서, 평가→뱅가, 점검→품검, OCR→ocR 등), 표 칸 일부 어긋남 |
| `05_도서관_연구보고서.pdf` | 디지털 PDF 2쪽 | 청크 여러 개, 긴 문서 요약 | 오류 없음 |

## 정답 (`answers.json`, 문서와 같은 폴더)

문서마다 아래 값이 들어 있습니다.

- `text`: 문서에 넣은 글자 그대로 (OCR·교정 결과와 비교할 정답, 표는 탭 구분)
- `key_facts`: 요약에 꼭 들어가야 할 핵심 사실 (날짜·금액·숫자 등). 요약 결과에 몇 개 들어 있는지로 요약 품질을 잽니다
- `purpose`: 이 문서로 무엇을 비교하는지

## 비교할 LLM 후보 (RTX 3060 12GB 기준)

Hugging Face 의 GGUF 파일을 Ollama 로 바로 받을 수 있습니다 (`ollama pull hf.co/저장소:양자화`).

| 모델 | 받는 명령 | 크기 | 특징 |
|---|---|---|---|
| gemma4:e4b (현재 기본값) | `ollama pull gemma4:e4b` | 6.6GB | 기준점 |
| EXAONE 3.5 7.8B (LG) | `ollama pull hf.co/LGAI-EXAONE/EXAONE-3.5-7.8B-Instruct-GGUF:Q4_K_M` | 4.8GB | 한국어 특화. 라이선스가 비상업용 |
| Kanana 1.5 8B (카카오) | `ollama pull hf.co/mradermacher/kanana-1.5-8b-instruct-2505-GGUF:Q4_K_M` 후 아래 "Kanana 설정" | 4.9GB | 한국어 특화 |
| Gemma 4 12B | `ollama pull hf.co/unsloth/gemma-4-12b-it-GGUF:Q4_K_M` | 7.1GB | 기본 모델의 큰 버전 |

모델을 바꿔 보려면 `backend/.env` 의 `OLLAMA_MODEL` 에 `ollama list` 에 나오는 이름을 그대로 넣고 backend 를 다시 켭니다.

### Kanana 설정 (꼭 필요)

`hf.co/...` 로 받은 그대로 쓰면 GGUF 안의 대화 형식을 Ollama 가 해석하지 못해, 교정(JSON 답) 요청이 500 오류가 나고 요약 끝에 `<|eot_id|>` 가 섞여 나옵니다. 표준 Llama 3 형식만 담은 설정으로 새 이름을 만들어 씁니다 (파일을 다시 받지 않음, 원래 모델은 지우면 안 됨).

```bash
ollama create kanana-1.5-8b -f eval/modelfiles/kanana-1.5-8b.Modelfile
```

## 비교 실행 (`compare_llm.py`)

```bash
backend/.venv/bin/python eval/compare_llm.py --docs "문서 폴더"                    # ollama list 의 모델 전부
backend/.venv/bin/python eval/compare_llm.py --docs "문서 폴더" --models gemma4:e4b kanana-1.5-8b
```

- 화면의 AI 도우미와 같은 함수(`app/assist/core.py`)·같은 `.env` 설정으로 요약·교정을 하고, `answers.json` 정답과 비교해 점수를 매깁니다.
- OCR 은 한 번만 하고 모든 모델이 같은 OCR 결과를 받습니다. 모델마다 첫 로딩 시간은 따로 재고, 다 쓴 모델은 GPU 에서 내립니다.
- 결과는 `--out` 폴더(기본: `문서 폴더/비교결과_날짜시각/`)에 `요약표.md`, `상세.csv`, `모델별/*.json`, `OCR결과.json`. **프로젝트 폴더 안에는 쓰지 않습니다** (실행 결과가 git 에 섞이지 않게). OCR 중간 파일은 임시 폴더에서 지웁니다.
- 4개 모델 약 10~15분 (RTX 3060 기준).

### 결과 (2026-11-28, RTX 3060 12GB, 문서 5개)

| 모델 | 요약(s/문서) | 요약 핵심 사실 | 교정: 맞게 / 잘못 | 스캔 문서 틀린 글자 | 오타 없는 문서에 낸 제안 |
|---|---|---|---|---|---|
| 규칙만 (LLM 없음) | - | - | 0 / 0 | 129 → 129 | 0 |
| Gemma 4 12B | 8.7 | 25/42 (60%) | **23 / 2** | **129 → 102** | 0 |
| gemma4:e4b (기본값) | **1.7** | 26/42 (62%) | 19 / 2 | 129 → 110 | 1 |
| EXAONE 3.5 7.8B | 4.5 | 27/42 (64%) | 4 / **0** | 129 → 125 | 0 |
| Kanana 1.5 8B | 5.1 | **28/42 (67%)** | 0 / 0 | 129 → 129 | 0 |

- 교정은 Gemma 4 12B > gemma4:e4b. 한국어 특화 모델(EXAONE·Kanana)은 거의 고치지 않음 (Kanana 는 짧은 입력에선 고치지만 문서 단위 입력에선 빈 답)
- 요약은 모두 60~67% 로 비슷. 지어낸 숫자는 모두 0 (OCR 이 잘못 읽은 숫자를 옮긴 것은 세지 않음)
- gemma4:e4b 는 오타 없는 문서에서 "2025년 → 2023년" 처럼 사실을 바꾸는 제안을 1건 냄 (사용자가 수락해야 적용되므로 [규칙 2] 는 지켜짐)
- 문서 5개 기준 결과라 차이가 작은 항목(요약)은 우열로 보기 어려움. temperature 0 이라 다시 돌려도 같은 결과
