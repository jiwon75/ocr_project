"""
pytest 공통 준비물(fixture).
- settings : 임시 폴더를 쓰는 설정. 실제 data/, storage/ 를 더럽히지 않기 위해 테스트마다 새 폴더를 쓴다
- run_doc  : 워커 없이 같은 프로세스 안에서 문서 하나를 끝까지 처리하는 도우미 (빠른 단계별 테스트용)
- real_engines : 실제 PaddleOCR 모델 (테스트 세션에서 한 번만 불러옴, @pytest.mark.models 테스트만 사용)
"""
import shutil
from pathlib import Path

import pytest

from app import db
from app.config import Settings
from app.pipeline import runner
from app.pipeline.storage import DocStorage


@pytest.fixture
def settings(tmp_path) -> Settings:
    # _env_file=None: 실제 .env 를 읽지 않는다. 개발자 PC 의 .env 값에 따라 테스트 결과가 달라지면 안 되기 때문
    s = Settings(
        _env_file=None,
        STORAGE_DIR=tmp_path / "storage",
        DB_PATH=tmp_path / "data" / "app.db",
        LOG_DIR=tmp_path / "logs",
        OCR_CONFIDENCE_THRESHOLD=0.8,
        PROCESS_TIMEOUT_SEC=30,
        WORKER_START_TIMEOUT_SEC=120,
    )
    db.init_db(s.DB_PATH)
    return s


@pytest.fixture
def run_doc(settings, tmp_path):
    """
    run_doc(파일경로, 형식, 엔진, language="auto") → (doc_id, file_id, DocStorage)
    업로드 API 가 하는 일(파일 저장 + uploaded_files 기록)을 흉내 낸 뒤 runner.run_document 를 직접 부른다.
    """
    import uuid

    def _run(path: Path, file_type: str, engines, language: str = "auto"):
        doc_id = str(uuid.uuid4())
        storage = DocStorage(settings.STORAGE_DIR, doc_id)
        storage.create_dirs()
        dest = storage.original(file_type)
        shutil.copy(path, dest)
        file_id = db.insert_uploaded_file(settings.DB_PATH, doc_id, path.name, str(dest), file_type)
        job = {"doc_id": doc_id, "file_id": file_id, "file_name": path.name, "file_type": file_type, "file_path": str(dest), "language": language}
        runner.run_document(job, engines, settings)
        return doc_id, file_id, storage, job

    return _run


@pytest.fixture(scope="session")
def real_engines():
    """실제 모델. 불러오는 데 시간이 걸려서 세션 전체에서 한 번만 만든다 (서버의 '한 번만 로딩'과 같은 이유)."""
    from app.pipeline.engines import load_engines

    return load_engines(Settings(_env_file=None))
