"""
[이 파일이 하는 일]
로그(프로그램이 남기는 기록)를 콘솔과 파일(logs/app.log) 두 곳에 같은 형식으로 남기도록 설정한다.

  2026-11-27 16:40:01 | ERROR | doc=ab12 | file=sample.pdf | stage=layout | 1532ms | ValueError: ...

[왜 이렇게 나눴는지]
- 처리 단계마다 "언제, 어떤 파일이, 어느 단계에서, 얼마나 걸려서, 성공/실패했는지"를 남겨야
  문제가 생겼을 때 원인을 찾을 수 있다. 그 형식을 한 곳에서 정해 두면 모든 코드가 같은 모양으로 기록한다.
- 워커 프로세스(실제 OCR 을 돌리는 별도 프로그램)도 로그를 남긴다. 여러 프로세스가 같은 파일에 직접 쓰면
  Windows 에서는 날짜가 바뀔 때 로그 파일 이름 바꾸기(로테이션)가 "파일 사용 중" 오류로 실패한다.
  그래서 워커는 로그를 큐(Queue, 프로세스 사이의 우편함)에 넣기만 하고, 메인 프로세스 한 곳에서만 파일에 쓴다.
"""
import logging
import logging.handlers
import time
from contextlib import contextmanager

LOG_FORMAT = "%(asctime)s | %(levelname)s | doc=%(doc)s | file=%(file)s | stage=%(stage)s | %(ms)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("dococr")


class _ContextDefaults(logging.Filter):
    """
    doc/file/stage/ms 값을 주지 않고 남긴 로그에도 "-" 를 채워 넣는다.
    이게 없으면 형식 문자열의 %(doc)s 를 못 찾아서 로그 한 줄이 통째로 오류가 난다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key in ("doc", "file", "stage", "ms"):
            if not hasattr(record, key):
                setattr(record, key, "-")
        return True


def setup_main_logging(log_dir, level: str, backup_days: int, log_queue=None):
    """
    메인(API 서버) 프로세스의 로그 설정.

    받는 값: 로그 폴더, 로그 레벨, 보관 일수, (선택) 워커가 로그를 보내 줄 큐
    돌려주는 값: QueueListener (워커 로그를 받아 파일/콘솔에 써 주는 일꾼). 종료 시 stop() 해야 한다.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(_ContextDefaults())

    # TimedRotatingFileHandler(when="midnight"): 자정마다 app.log 를 app.log.2026-11-27 처럼 날짜를 붙여 바꾸고 새 파일을 연다
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "app.log", when="midnight", backupCount=backup_days, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_ContextDefaults())

    logger.handlers.clear()
    logger.setLevel(level)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.propagate = False

    listener = None
    if log_queue is not None:
        # 워커가 큐에 넣은 로그를 꺼내서 위의 두 핸들러(콘솔/파일)로 넘겨준다
        listener = logging.handlers.QueueListener(log_queue, console, file_handler, respect_handler_level=True)
        listener.start()
    return listener


def setup_worker_logging(log_queue, level: str):
    """
    워커 프로세스의 로그 설정. 파일에 직접 쓰지 않고 큐에만 넣는다 (이유는 파일 맨 위 설명 참고).
    """
    logger.handlers.clear()
    logger.setLevel(level)
    handler = logging.handlers.QueueHandler(log_queue)
    handler.addFilter(_ContextDefaults())
    logger.addHandler(handler)
    logger.propagate = False


def log_ctx(doc_id: str | None = None, file_name: str | None = None, stage: str | None = None, ms: int | None = None) -> dict:
    """logger.info(..., extra=log_ctx(...)) 처럼 쓰는 도우미. 로그 한 줄에 doc/file/stage/ms 를 붙여 준다."""
    return {
        "doc": (doc_id or "-")[:8],  # doc_id(uuid)는 길어서 앞 8글자만 남긴다. 찾을 때는 이것으로도 충분하다
        "file": file_name or "-",
        "stage": stage or "-",
        "ms": f"{ms}ms" if ms is not None else "-",
    }


@contextmanager
def stage_log(stage: str, doc_id: str, file_name: str, detail: str = ""):
    """
    with stage_log("layout", doc_id, name): ... 처럼 감싸면
    - 끝났을 때 걸린 시간(ms)과 함께 "성공" 로그를 남기고
    - 오류가 나면 traceback(오류가 난 코드 위치 목록)까지 포함한 "실패" 로그를 남긴 뒤 오류를 다시 던진다.
    오류를 다시 던지는 이유: 로그만 남기고 삼켜 버리면 호출한 쪽이 실패한 줄 모르고 다음 단계로 넘어가기 때문.
    """
    start = time.perf_counter()
    try:
        yield
    except Exception as e:
        ms = int((time.perf_counter() - start) * 1000)
        # exc_info=True → traceback 을 로그에 함께 남긴다
        logger.error(f"실패 {type(e).__name__}: {e}", exc_info=True, extra=log_ctx(doc_id, file_name, stage, ms))
        raise
    else:
        ms = int((time.perf_counter() - start) * 1000)
        logger.info(f"성공 {detail}".strip(), extra=log_ctx(doc_id, file_name, stage, ms))
