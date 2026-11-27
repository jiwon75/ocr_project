"""
[이 파일이 하는 일]
FastAPI 앱을 만들고, 서버가 켜질 때/꺼질 때 할 일(lifespan)을 정한다.

용어
- lifespan: 서버가 "시작할 때 한 번" 하고 "끝날 때 한 번" 할 일을 적는 곳.
  여기서 DB 테이블 만들기, 로그 설정, 워커(모델 로딩) 시작을 한다.

[왜 create_app() 함수로 만들었는지]
테스트에서 설정(임시 폴더, 작은 시간 제한)과 가짜 OCR 엔진을 넣은 앱을 따로 만들기 위해서다.
실제 서버는 맨 아래 app = create_app() 을 쓴다.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.api import api_router
from app.config import Settings, get_settings
from app.logger import log_ctx, logger, setup_main_logging
from app.pipeline.worker import WorkerPool

DEFAULT_ENGINE_FACTORY = "app.pipeline.engines:load_engines"


def create_app(settings: Settings | None = None, engine_factory: str = DEFAULT_ENGINE_FACTORY) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ---- 서버 시작 시 ----
        pool = WorkerPool(settings, engine_factory)
        # 워커 로그를 받아 파일/콘솔에 쓰는 일꾼을 먼저 띄운다 (워커가 시작하자마자 로그를 보낼 수 있으므로)
        listener = setup_main_logging(settings.LOG_DIR, settings.LOG_LEVEL, settings.LOG_BACKUP_DAYS, pool.log_queue)
        db.init_db(settings.DB_PATH)  # 테이블이 없으면 만든다
        settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        # 모델 로딩은 워커 프로세스에서 백그라운드로 진행된다. 그동안 서버는 먼저 떠서 health 에 not_ready 로 응답한다
        pool.start()
        app.state.pool = pool
        logger.info(f"서버 시작 (워커 {settings.WORKER_COUNT}개, 시간 제한 {settings.PROCESS_TIMEOUT_SEC}초)", extra=log_ctx(stage="startup"))
        yield
        # ---- 서버 종료 시 ----
        pool.stop()
        logger.info("서버 종료", extra=log_ctx(stage="shutdown"))
        if listener:
            listener.stop()

    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,  # [규칙 4] 허용 주소도 .env 에서
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
