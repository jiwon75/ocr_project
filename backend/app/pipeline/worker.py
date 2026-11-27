"""
[이 파일이 하는 일]
OCR 작업을 "별도 프로세스(워커)"에서 실행하고, 시간 제한(PROCESS_TIMEOUT_SEC)을 넘으면 그 프로세스를 강제로 끝낸다.

  API 서버(메인 프로세스)                       워커 프로세스 (WORKER_COUNT 개)
  ┌───────────────────────────┐   작업 큐    ┌────────────────────────────────┐
  │ WorkerPool.submit(job) ───┼────────────▶│ 시작할 때 모델을 한 번만 불러옴   │
  │ WorkerSlot 스레드:         │             │ 작업을 받아 run_document 실행    │
  │  - 결과를 기다리다가        │◀────────────┼ 끝나면 "done" 보고               │
  │  - 시간 초과면 kill()       │   결과 큐   └────────────────────────────────┘
  │  - 새 워커를 다시 띄움       │
  └───────────────────────────┘

[왜 스레드가 아니라 프로세스인지]
파이썬 스레드는 밖에서 강제로 멈출 방법이 없다. OCR 모델이 계산하는 도중에는 "그만" 신호를 확인하지도 않는다.
그래서 스레드로는 30초 제한을 "실제로 작업을 멈추는" 방식으로 지킬 수 없다.
프로세스는 운영체제가 강제로 끝낼 수 있으므로(kill), 시간이 넘으면 작업이 정말로 멈춘다.

[모델 로딩과의 관계]
모델은 워커가 시작할 때 한 번만 불러오고, 그 워커가 처리하는 모든 파일에 재사용한다.
단, 시간 초과로 워커를 끝내면 그 워커가 들고 있던 모델도 함께 사라지므로, 새 워커가 모델을 다시 불러온다(약 15초).
그동안 다른 워커가 없으면 health 는 not_ready 가 되고, 새 파일은 대기(queued) 상태로 기다린다.

[나중에 Celery/RQ 로 바꾸려면]
이 파일만 바꾸면 된다. API 는 pool.submit() 만 부르고, 실제 작업은 runner.run_document() 가 한다.
"""
import importlib
import multiprocessing as mp
import queue
import threading
import time
import traceback
import uuid

from app.logger import log_ctx, logger, setup_worker_logging


def _import_string(path: str):
    """'app.pipeline.engines:load_engines' → 그 함수. 테스트가 가짜 엔진 함수를 문자열로 넘길 수 있게 하기 위해서다."""
    module, _, attr = path.partition(":")
    return getattr(importlib.import_module(module), attr)


def worker_main(settings, engine_factory: str, task_q, result_q, log_q) -> None:
    """
    워커 프로세스의 시작점. (별도 프로세스에서 실행된다)
    1) 로그를 큐로 보내도록 설정  2) 모델을 한 번 불러옴  3) 작업을 하나씩 받아 처리
    """
    setup_worker_logging(log_q, settings.LOG_LEVEL)
    from app.pipeline import runner  # 워커 안에서만 쓰는 무거운 모듈은 여기서 불러온다

    started = time.perf_counter()
    try:
        # 모델을 워커 시작 때 한 번만 불러온다. 작업마다 불러오면 매번 십수 초씩 걸리기 때문
        engines = _import_string(engine_factory)(settings)
    except Exception:
        logger.error("모델 로딩 실패", exc_info=True, extra=log_ctx(stage="model_load"))
        result_q.put(("load_failed", None, traceback.format_exc()))
        return
    logger.info("모델 로딩 완료", extra=log_ctx(stage="model_load", ms=int((time.perf_counter() - started) * 1000)))
    result_q.put(("ready", None, None))

    while True:
        task = task_q.get()
        if task is None:  # 종료 신호
            break
        try:
            if task["kind"] == "document":
                runner.run_document(task["payload"], engines, settings)
            elif task["kind"] == "retry":
                runner.run_block_retry(task["payload"], engines, settings)
            result_q.put(("done", task["job_id"], None))
        except Exception:
            # run_document 는 스스로 실패를 기록하지만, 혹시 새어 나온 오류도 워커가 죽지 않게 잡는다
            logger.error("작업 처리 중 오류", exc_info=True, extra=log_ctx(task["payload"].get("doc_id"), task["payload"].get("file_name"), task["kind"]))
            result_q.put(("error", task["job_id"], traceback.format_exc()))


class WorkerSlot:
    """
    워커 프로세스 하나 + 그것을 감시하는 스레드 하나.
    감시 스레드가 작업을 꺼내 워커에 넘기고, 시간을 재다가 넘으면 워커를 끝내고 새로 띄운다.
    """

    def __init__(self, index: int, pool: "WorkerPool"):
        self.index = index
        self.pool = pool
        self.process = None
        self.task_q = None
        self.result_q = None
        self.state = "starting"  # starting | ready | busy | load_failed | stopped
        self.thread = threading.Thread(target=self._loop, name=f"worker-slot-{index}", daemon=True)

    # ---- 워커 프로세스 관리 ----
    def _start_process(self) -> bool:
        """
        워커 프로세스를 새로 띄우고 모델 로딩이 끝날 때까지 기다린다.
        큐도 매번 새로 만든다. 강제 종료된 프로세스가 쓰던 큐는 내부 잠금이 걸린 채로 망가져 있을 수 있기 때문.
        """
        ctx = self.pool.mp_ctx
        self.state = "starting"
        self.task_q, self.result_q = ctx.Queue(), ctx.Queue()
        self.process = ctx.Process(
            target=worker_main,
            args=(self.pool.settings, self.pool.engine_factory, self.task_q, self.result_q, self.pool.log_queue),
            name=f"ocr-worker-{self.index}",
            daemon=True,  # 메인 프로세스가 끝나면 워커도 같이 끝나게 한다 (고아 프로세스 방지)
        )
        self.process.start()
        deadline = time.monotonic() + self.pool.settings.WORKER_START_TIMEOUT_SEC
        while time.monotonic() < deadline and not self.pool.stopping:
            try:
                kind, _, _ = self.result_q.get(timeout=0.5)
            except queue.Empty:
                if not self.process.is_alive():
                    break
                continue
            if kind == "ready":
                self.state = "ready"
                return True
            break  # load_failed
        self._kill()
        self.state = "load_failed"
        logger.error("워커를 시작하지 못했습니다 (모델 로딩 실패 또는 시간 초과)", extra=log_ctx(stage="model_load"))
        return False

    def _kill(self) -> None:
        """워커 프로세스를 강제로 끝낸다. 시간 초과 시 "실제로 작업을 멈추는" 부분이 바로 여기다."""
        if self.process is not None and self.process.is_alive():
            self.process.kill()
        if self.process is not None:
            self.process.join(timeout=5)

    def _ensure_process(self) -> bool:
        if self.process is not None and self.process.is_alive() and self.state in ("ready", "busy"):
            return True
        return self._start_process()

    # ---- 감시 스레드 ----
    def _loop(self) -> None:
        self._ensure_process()  # 서버 시작 직후 바로 모델을 불러오기 시작한다 (첫 요청이 오기 전에)
        while not self.pool.stopping:
            try:
                job = self.pool.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                if not self._ensure_process():
                    self.pool.on_unavailable(job)
                    continue
                self._run_job(job)
            finally:
                self.pool.finish(job)
        self.state = "stopped"

    def _run_job(self, job: dict) -> None:
        """
        작업을 워커에 넘기고, 시간 제한 안에 끝나는지 지켜본다.
        시간은 "워커가 작업을 받은 순간"부터 잰다. 대기열에서 기다린 시간까지 세면, 앞 파일 때문에 뒤 파일이 억울하게 실패하기 때문.
        """
        settings = self.pool.settings
        self.state = "busy"
        self.task_q.put(job)
        deadline = time.monotonic() + settings.PROCESS_TIMEOUT_SEC
        while True:
            try:
                kind, job_id, _ = self.result_q.get(timeout=0.2)
                if job_id == job["job_id"]:
                    self.state = "ready"
                    return
                continue
            except queue.Empty:
                pass
            if not self.process.is_alive():
                # 워커가 혼자 죽었다 (메모리 부족 등). 이 파일만 실패시키고 새 워커를 띄운다
                logger.error("워커가 비정상 종료되었습니다", extra=log_ctx(job["payload"].get("doc_id"), job["payload"].get("file_name"), job["kind"]))
                self.pool.on_crash(job)
                self._start_process()
                return
            if time.monotonic() > deadline:
                # 시간 초과: 워커를 강제로 끝내서 작업을 실제로 멈춘다. 그 뒤 실패를 기록하고 새 워커를 띄운다
                logger.error(
                    f"시간 제한 {settings.PROCESS_TIMEOUT_SEC}초 초과 → 워커 강제 종료",
                    extra=log_ctx(job["payload"].get("doc_id"), job["payload"].get("file_name"), job["kind"], settings.PROCESS_TIMEOUT_SEC * 1000),
                )
                self._kill()
                self.pool.on_timeout(job)
                self._start_process()
                return

    def stop(self) -> None:
        try:
            if self.task_q is not None and self.process is not None and self.process.is_alive():
                self.task_q.put(None)
                self.process.join(timeout=3)
        except Exception:
            pass
        self._kill()


class WorkerPool:
    """
    워커 슬롯 여러 개와 작업 대기열을 관리한다. API 는 이 클래스의 submit()/is_busy()/ready_count 만 쓴다.
    """

    def __init__(self, settings, engine_factory: str = "app.pipeline.engines:load_engines", log_queue=None):
        self.settings = settings
        self.engine_factory = engine_factory
        # spawn: 워커를 "깨끗한 새 파이썬"으로 시작한다. Windows 는 이 방식만 지원하고, 다른 OS 에서도 같은 동작을 보장하려고 명시한다
        self.mp_ctx = mp.get_context("spawn")
        self.log_queue = log_queue if log_queue is not None else self.mp_ctx.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.stopping = False
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self.slots = [WorkerSlot(i, self) for i in range(max(1, settings.WORKER_COUNT))]

    def start(self) -> None:
        """감시 스레드를 시작한다. 모델 로딩은 백그라운드에서 진행되므로 이 함수는 바로 끝난다 (서버가 먼저 떠서 health 에 응답할 수 있게)."""
        for slot in self.slots:
            slot.thread.start()

    def stop(self) -> None:
        self.stopping = True
        for _ in self.slots:
            self.jobs.put(None)
        for slot in self.slots:
            slot.stop()
            slot.thread.join(timeout=5)

    @property
    def ready_count(self) -> int:
        return sum(1 for s in self.slots if s.state in ("ready", "busy"))

    @property
    def states(self) -> list[str]:
        return [s.state for s in self.slots]

    def is_busy(self, doc_id: str) -> bool:
        """이 문서의 작업이 대기 중이거나 실행 중인지. 같은 문서를 동시에 두 번 처리하면 결과 파일이 서로 덮어써지므로 막는 데 쓴다."""
        with self._lock:
            return doc_id in self._busy

    def submit(self, kind: str, payload: dict) -> str:
        """작업을 대기열에 넣는다. kind: "document"(전체 처리) | "retry"(블록 하나)"""
        job = {"job_id": uuid.uuid4().hex, "kind": kind, "payload": payload}
        with self._lock:
            self._busy.add(payload["doc_id"])
        self.jobs.put(job)
        return job["job_id"]

    def finish(self, job: dict) -> None:
        with self._lock:
            self._busy.discard(job["payload"]["doc_id"])

    # ---- 실패 처리 (메인 프로세스에서 실행) ----
    def on_timeout(self, job: dict) -> None:
        from app.pipeline import runner

        if job["kind"] == "document":
            runner.fail_document_timeout(self.settings, job["payload"])
        else:
            runner.fail_retry_timeout(self.settings, job["payload"])

    def on_crash(self, job: dict) -> None:
        from app.pipeline import runner

        if job["kind"] == "document":
            runner.fail_document_crash(self.settings, job["payload"])
        else:
            runner.fail_retry(self.settings, job["payload"], "error", "워커가 비정상 종료되었습니다")

    def on_unavailable(self, job: dict) -> None:
        from app.pipeline import runner

        if job["kind"] == "document":
            runner.fail_document_crash(self.settings, job["payload"], "worker_unavailable")
        else:
            runner.fail_retry(self.settings, job["payload"], "error", "OCR 모델을 불러오지 못했습니다")
