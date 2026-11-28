"""
[이 파일이 하는 일]
.env 파일에 적힌 설정 값을 읽어서 파이썬 객체(Settings)로 만들어 준다.

[왜 이렇게 나눴는지]
[규칙 4] 시간 제한, 파일 크기, 경로 같은 값을 코드 곳곳에 직접 쓰면
값을 바꿀 때마다 코드를 찾아다니며 고쳐야 하고, 실수로 한 곳을 빼먹기 쉽다.
그래서 모든 설정을 이 파일 한 곳에서 .env 로부터 읽고, 다른 코드는 settings.XXX 로만 꺼내 쓴다.

용어
- pydantic-settings: .env 파일이나 환경변수를 읽어서, 타입(int, float 등)까지 자동으로 바꿔 주는 라이브러리.
  예) .env 에 MAX_FILE_SIZE_MB=5 라고 쓰면 문자열 "5"가 아니라 숫자 5로 들어온다.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend 폴더의 절대 경로.
# 서버를 어느 폴더에서 실행하든 ./data, ./storage 같은 상대 경로가 항상 backend 기준이 되도록 하기 위해 쓴다.
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    .env 의 값을 담는 설정 객체.
    아래 적힌 기본값은 .env 에 해당 항목이 없을 때만 쓰인다. 실제 값은 .env 에서 관리한다.
    (.env.example 에도 같은 항목이 모두 있어야 한다 → tests/test_rules.py 에서 검사)
    """

    # ---- 서버 ----
    APP_NAME: str = "Document OCR"
    # CORS: 다른 주소(예: 프론트 5173 포트)에서 온 브라우저 요청을 허용할지 정하는 규칙
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # ---- 경로 ----
    STORAGE_DIR: Path = Path("./storage")  # 원본, 페이지 이미지, 블록 crop, 결과 JSON 저장 위치
    DB_PATH: Path = Path("./data/app.db")  # SQLite DB 파일
    LOG_DIR: Path = Path("./logs")  # 로그 파일 폴더

    # ---- 로그 ----
    LOG_LEVEL: str = "INFO"
    LOG_BACKUP_DAYS: int = 14  # 날짜별 로그 파일을 며칠치 보관할지

    # ---- 업로드 제한 ----
    MAX_FILE_SIZE_MB: int = 10
    MAX_PAGES: int = 20

    # ---- 처리 시간 / 워커 ----
    PROCESS_TIMEOUT_SEC: int = 30  # 파일 하나(1~3단계 전체)의 처리 시간 제한
    WORKER_COUNT: int = 1  # 동시에 처리할 파일 수. 워커마다 모델을 따로 불러오므로 메모리를 그만큼 더 쓴다
    WORKER_START_TIMEOUT_SEC: int = 600  # 워커가 모델을 불러오는 데 기다려 줄 최대 시간(첫 실행은 모델 다운로드 포함)

    # ---- OCR 판정 기준 ----
    # confidence(신뢰도): OCR 모델이 "이 글자가 맞을 확률"을 0~1 사이로 스스로 매긴 점수
    OCR_CONFIDENCE_THRESHOLD: float = 0.8  # [규칙 2] 이 값보다 낮으면 "추출 실패"
    OCR_CONFIDENCE_AGG: Literal["mean", "min"] = "mean"  # 블록 안 여러 줄의 점수를 합치는 방법 (평균/최솟값)
    TABLE_STRUCTURE_THRESHOLD: float = 0.5  # 표 구조 인식 점수가 이보다 낮으면 표 블록 실패
    # 표 칸의 신뢰도가 기준 미달일 때 [추출 실패: ...] 대신 읽은 글자 뒤에 (?) 를 붙여 보여 줄지 (예: "짧뽕 (?)")
    TABLE_SHOW_UNCERTAIN_TEXT: bool = True
    # 레이아웃 블록 어디에도 속하지 않은 글자(예: 영상 자막)를 페이지 전체에서 다시 찾아 글자 블록으로 추가할지
    LAYOUT_SWEEP_UNCOVERED_TEXT: bool = True
    # 그렇게 찾은 글자 중 높이가 페이지 높이의 이 비율보다 작은 것은 버린다 (영상 플레이어의 재생 시간 같은 작은 화면 글자).
    # 표 모양으로 묶이는 글자는 이 기준과 상관없이 표 블록이 된다
    LAYOUT_SWEEP_MIN_HEIGHT_RATIO: float = 0.03

    # ---- 전처리 ----
    PDF_RENDER_DPI: int = 200  # PDF 페이지를 이미지로 바꿀 해상도. 높을수록 정확하지만 느려진다
    PDF_TEXT_LAYER_MIN_CHARS: int = 20  # 페이지에 이만큼 글자가 있으면 "디지털 원본 PDF"로 보고 OCR 대신 직접 추출
    CROP_PADDING_PX: int = 12  # 블록을 잘라낼 때 주변 여백. 너무 딱 맞게 자르면 글자 검출 모델이 가장자리 글자를 놓친다

    # ---- 모델 ----
    LAYOUT_MODEL: str = "PP-DocLayout_plus-L"
    LAYOUT_THRESHOLD: float = 0.5  # 레이아웃 검출 점수가 이보다 낮은 블록은 버린다
    TEXT_DET_MODEL: str = "PP-OCRv5_mobile_det"
    # 글자 검출 상자 설정. 기본값(0.6 / 1.5)은 반투명 배경 위 한 자리 숫자("1", "4")를 놓치거나
    # "6" 의 윗부분을 잘라 "b" 로 읽히게 했다. 낮은 box_thresh 로 흐린 글자도 잡고, unclip 을 키워 상자에 여유를 준다
    TEXT_DET_BOX_THRESH: float = 0.3
    TEXT_DET_UNCLIP_RATIO: float = 2.0
    TEXT_REC_MODEL_KO: str = "korean_PP-OCRv5_mobile_rec"
    TEXT_REC_MODEL_EN: str = "en_PP-OCRv5_mobile_rec"
    TABLE_MODEL: str = "SLANet_plus"
    PADDLE_DISABLE_MODEL_SOURCE_CHECK: bool = True  # 시작할 때마다 모델 서버 연결을 확인하는 단계를 건너뛰어 시작을 빠르게 한다

    # ---- 결과 합치기 ----
    MERGE_PAGE_SEPARATOR: bool = True  # DB 에 저장하는 .txt 에 페이지 구분선을 넣을지 (기본값)
    # 레이아웃 모델이 머리말·꼬리말·쪽번호로 잡은 블록(이미지·PDF)을 .txt/.md 결과에서 뺄지. 화면에는 그대로 보인다
    MERGE_SKIP_PAGE_FURNITURE: bool = True

    # ---- 청킹 (추출 텍스트를 검색·RAG 용 조각으로 나눠 저장) — app/pipeline/chunking.py ----
    # 청크 하나의 최대 글자 수. 너무 작으면 문맥이 끊기고, 너무 크면 한 청크에 여러 주제가 섞여 검색이 부정확해진다
    CHUNK_SIZE_CHARS: int = 1000
    # 앞 청크 끝부분을 다음 청크 앞에 겹쳐 넣는 글자 수 (보통 크기의 10~20%). 0 이면 겹치지 않는다. CHUNK_SIZE_CHARS 보다 작아야 한다
    CHUNK_OVERLAP_CHARS: int = 150

    # ---- LLM 도우미 (Ollama: 내 PC 에서 돌리는 LLM 서버) — 요약·오타 교정 제안 ----
    # 꺼 두거나 Ollama 가 안 켜져 있어도 OCR 은 그대로 동작한다. 화면의 요약/교정 버튼만 쓸 수 없다고 안내된다
    LLM_ENABLED: bool = True
    OLLAMA_URL: str = "http://localhost:11434"
    # 이 PC(RTX 2060 6GB)에서 시험해 본 모델 중 한국어 품질·속도가 가장 나았던 것. 다른 모델은 `ollama list` 로 확인
    OLLAMA_MODEL: str = "gemma4:e4b"
    # 요청 하나를 기다려 줄 최대 시간(초). 첫 요청은 모델을 GPU/메모리에 올리느라 1~3분 걸릴 수 있다
    OLLAMA_TIMEOUT_SEC: int = 300
    # 긴 문서는 이 글자 수 단위로 나눠 LLM 에 보낸다 (한 번에 너무 많이 보내면 느려지고 뒷부분을 놓친다)
    LLM_CHUNK_CHARS: int = 2500
    # 교정 제안 안전장치: 틀린 부분과 고친 글자가 (자모 단위로) 이 비율 이상 닮아야 제안으로 받아 준다.
    # LLM 이 비슷하게 생긴 글자가 아니라 아예 다른 단어로 바꾸는 것(예: 짬뽕 → 떡볶이)을 막는다
    LLM_CORRECTION_MIN_SIMILARITY: float = 0.6

    # env_file: 읽을 .env 위치. extra="ignore": .env 에 모르는 항목이 있어도 오류 내지 않음
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    @field_validator("STORAGE_DIR", "DB_PATH", "LOG_DIR")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        """상대 경로를 backend 폴더 기준 절대 경로로 바꾼다. 실행 위치에 따라 경로가 달라지는 사고를 막기 위해서다."""
        value = Path(value)
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @model_validator(mode="after")
    def _check_chunk_settings(self):
        """청크 설정이 말이 되는지 서버 시작 때 확인한다 (잘못되면 문서마다 실패하는 대신 바로 알 수 있게)."""
        if self.CHUNK_SIZE_CHARS < 1:
            raise ValueError("CHUNK_SIZE_CHARS 는 1 이상이어야 합니다.")
        if not 0 <= self.CHUNK_OVERLAP_CHARS < self.CHUNK_SIZE_CHARS:
            raise ValueError("CHUNK_OVERLAP_CHARS 는 0 이상, CHUNK_SIZE_CHARS 보다 작아야 합니다.")
        return self

    @property
    def max_file_size_bytes(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """
    설정 객체를 돌려준다.
    lru_cache 로 한 번만 만들고 재사용한다. 매번 .env 파일을 다시 읽을 필요가 없기 때문.
    (테스트에서는 get_settings 대신 Settings(...) 를 직접 만들어 원하는 값으로 바꿔 쓴다)
    """
    return Settings()
