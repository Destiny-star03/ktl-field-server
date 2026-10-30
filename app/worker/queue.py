"""판독 작업 큐 (REQ-B1-04): 별도 프로세스에서 판독, 1장 10초 제한, 실패는 그 사진만 failed.

구조: 서버 프로세스의 감독 스레드 N개가 각자 작업 프로세스 1개를 맡는다.
- 작업을 보내고 10초 안에 답이 없으면 그 프로세스를 강제 종료(terminate → kill)하고 새로 띄운다.
- 모듈 예외(ReadError 포함)는 작업 프로세스 안에서 잡혀 failed로 돌아온다.
- 작업 프로세스가 죽어도(EOF) 그 사진만 failed, 다음 작업은 새 프로세스가 받는다.
사진 상태가 바뀔 때마다 subscribe()로 등록한 함수를 부른다 (DB 저장 #4 · SSE #6에서 연결).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import multiprocessing as mp
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
import os
import queue
import threading
import time
from typing import Any

from app.worker.child import READY, run
from app.worker.values import to_api_values

logger = logging.getLogger(__name__)

READ_TIMEOUT_SECONDS = 10.0
# TODO(확인 필요): 실제 OCR 모델 로딩 시간을 재서 정한다. 이 시간은 1장 10초 제한과 별도.
STARTUP_TIMEOUT_SECONDS = 60.0
# TODO(확인 필요): 노트북 사양(코어 수·메모리)에 맞춰 정한다. KTL_READ_WORKERS로 바꿀 수 있다.
DEFAULT_WORKERS = 2
TIMEOUT_MESSAGE = "판독 시간 10초를 넘어 중단했습니다"
CRASH_MESSAGE = "판독 모듈 프로세스가 비정상 종료했습니다"


@dataclass(frozen=True)
class ReadJob:
    """사진 1장 판독 작업. reader가 None이면(`reader: none`) 판독 없이 바로 done."""

    photo_id: str
    image_path: str
    item_key: str
    reader: str | None
    expected_keys: tuple[str, ...] = ()


@dataclass
class ReadOutcome:
    """사진 상태 변경 알림. status: reading → done / failed. values는 API `Value` 모양(dict)."""

    photo_id: str
    status: str
    values: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    read_ms: int | None = None


Listener = Callable[[ReadJob, ReadOutcome], None]


class _WorkerProcess:
    """감독 스레드 하나가 맡는 작업 프로세스. 죽거나 멈추면 버리고 새로 띄운다."""

    def __init__(self, ctx: Any, preload: list[str], startup_timeout: float) -> None:
        self._ctx = ctx
        self._preload = preload
        self._startup_timeout = startup_timeout
        self._proc: BaseProcess | None = None
        self._conn: Connection | None = None

    def ensure_started(self) -> Connection:
        if self._proc is not None and self._proc.is_alive() and self._conn is not None:
            return self._conn
        self.kill()
        parent, child = self._ctx.Pipe()
        proc = self._ctx.Process(target=run, args=(child, self._preload), name="ktl-reader", daemon=True)
        proc.start()
        child.close()
        self._proc, self._conn = proc, parent
        try:
            ready = parent.poll(self._startup_timeout) and parent.recv() == READY
        except (EOFError, OSError):  # 시작 중에 죽음 (모듈 import 오류 · 네이티브 라이브러리 충돌 등)
            ready = False
        if not ready:
            self.kill()
            raise RuntimeError("판독 작업 프로세스를 시작하지 못했습니다")
        return parent

    def kill(self) -> None:
        proc, conn = self._proc, self._conn
        self._proc = self._conn = None
        if conn is not None:
            conn.close()
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
                proc.join(2)

    def stop(self) -> None:
        if self._conn is not None and self._proc is not None and self._proc.is_alive():
            try:
                self._conn.send(None)
                self._proc.join(2)
            except (OSError, EOFError):
                pass
        self.kill()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None


class ReadQueue:
    """판독 작업 큐. start() 후 submit()으로 넣는다. 작업 프로세스는 첫 작업 때 띄운다."""

    def __init__(
        self,
        workers: int | None = None,
        timeout: float = READ_TIMEOUT_SECONDS,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
        preload: list[str] | None = None,
    ) -> None:
        self.workers = max(1, workers or int(os.getenv("KTL_READ_WORKERS", str(DEFAULT_WORKERS))))
        self.timeout = timeout
        self._startup_timeout = startup_timeout
        self._preload = list(preload or [])
        self._ctx = mp.get_context("spawn")  # 스레드가 있는 서버에서 fork는 위험 · Windows와 동작을 맞춘다
        self._jobs: queue.Queue[ReadJob | None] = queue.Queue()
        self._listeners: list[Listener] = []
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._procs: list[_WorkerProcess] = []

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def start(self) -> None:
        if self._threads:
            return
        self._stopping.clear()
        for i in range(self.workers):
            proc = _WorkerProcess(self._ctx, self._preload, self._startup_timeout)
            t = threading.Thread(target=self._loop, args=(proc,), name=f"ktl-read-{i}", daemon=True)
            self._procs.append(proc)
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        """끝낸다. 판독 중인 사진은 마저 끝내고, 아직 시작 안 한 사진은 queued로 남긴다 (재시작 시 다시 넣기: #4)."""
        self._stopping.set()
        for _ in self._threads:
            self._jobs.put(None)
        for t in self._threads:
            t.join(self.timeout + 5)
        for proc in self._procs:
            proc.stop()
        self._threads.clear()
        self._procs.clear()

    def submit(self, job: ReadJob) -> None:
        """작업을 넣는다 (사진은 이미 queued 상태). 판독할 Reader가 없으면 바로 done."""
        if job.reader is None:
            self._emit(job, ReadOutcome(job.photo_id, "done", values=to_api_values([], job.expected_keys)))
            return
        self._jobs.put(job)

    def pending(self) -> int:
        return self._jobs.qsize()

    def _loop(self, proc: _WorkerProcess) -> None:
        while True:
            job = self._jobs.get()
            if job is None or self._stopping.is_set():
                return
            self._emit(job, ReadOutcome(job.photo_id, "reading"))
            try:
                outcome = self._read(proc, job)
            except Exception:  # noqa: BLE001 - 감독 스레드는 어떤 경우에도 살아 있어야 하고, 사진이 reading에 멈추면 안 된다
                logger.exception("판독 작업 처리 중 오류: %s", job.photo_id)
                proc.kill()
                outcome = ReadOutcome(job.photo_id, "failed", error=CRASH_MESSAGE)
            self._emit(job, outcome)

    def _read(self, proc: _WorkerProcess, job: ReadJob) -> ReadOutcome:
        try:
            conn = proc.ensure_started()
        except RuntimeError as exc:
            return ReadOutcome(job.photo_id, "failed", error=str(exc))
        start = time.monotonic()
        try:
            conn.send({"reader": job.reader, "image_path": job.image_path, "item_key": job.item_key})
            if not conn.poll(self.timeout):
                logger.warning("판독 %.0f초 초과로 작업 프로세스 종료: %s (pid %s)", self.timeout, job.photo_id, proc.pid)
                proc.kill()
                return ReadOutcome(job.photo_id, "failed", error=TIMEOUT_MESSAGE, read_ms=_ms(start))
            reply = conn.recv()
        except (EOFError, OSError):
            proc.kill()
            return ReadOutcome(job.photo_id, "failed", error=CRASH_MESSAGE, read_ms=_ms(start))
        if not reply.get("ok"):
            return ReadOutcome(job.photo_id, "failed", error=reply.get("error"), read_ms=reply.get("read_ms"))
        return ReadOutcome(
            job.photo_id,
            "done",
            values=to_api_values(reply["values"], job.expected_keys),
            warnings=reply["warnings"],
            read_ms=reply["read_ms"],
        )

    def _emit(self, job: ReadJob, outcome: ReadOutcome) -> None:
        for listener in list(self._listeners):
            try:
                listener(job, outcome)
            except Exception:  # noqa: BLE001 - 알림 받는 쪽 오류가 큐를 멈추지 않게
                logger.exception("판독 상태 알림 처리 중 오류: %s", job.photo_id)


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
