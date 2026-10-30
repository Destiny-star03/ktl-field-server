"""REQ-B1-04 판독 작업 큐 · REQ-B1-05 모듈 로더 테스트 (ktl_contract.fakes 가짜 모듈 사용).

주의: FakeReader는 경로에 'slow' · 'fail'이 있으면 반응하므로 테스트 함수 이름(tmp_path 폴더 이름)에 그 단어를 쓰지 않는다.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sys
import textwrap
import threading
import types

from fastapi.testclient import TestClient
import pytest

from app.main import create_mobile_app
from app.models import load_settings
from app.modules import expected_fields, get_reader, reader_name_for_item, reader_names
from app.worker import READ_TIMEOUT_SECONDS, ReadJob, ReadOutcome, ReadQueue
from app.worker.queue import CRASH_MESSAGE, TIMEOUT_MESSAGE
from app.worker.values import to_api_values

CATEGORY = {
    "category": "sampling",
    "items": [
        {"key": "cert", "label": "필증", "reader": "cert", "required": True},
        {"key": "plate", "label": "형식승인표", "reader": "none", "required": True},
        {"key": "prec_1", "label": "정밀오차 1", "reader": "flowmeter", "required": True},
    ],
    "export": ["photo_first", "cert.prev_receipt", "cert.serial", "prec_1.flow"],
}


class Recorder:
    """큐가 보내는 상태 알림을 모아 두고, 원하는 상태가 올 때까지 기다린다."""

    def __init__(self) -> None:
        self.events: list[ReadOutcome] = []
        self._cond = threading.Condition()

    def __call__(self, _job: ReadJob, outcome: ReadOutcome) -> None:
        with self._cond:
            self.events.append(outcome)
            self._cond.notify_all()

    def statuses(self, photo_id: str) -> list[str]:
        return [e.status for e in self.events if e.photo_id == photo_id]

    def wait_status(self, photo_id: str, status: str, timeout: float = 30) -> None:
        with self._cond:
            assert self._cond.wait_for(lambda: status in self.statuses(photo_id), timeout), f"{photo_id} {status} 안 옴"

    def wait(self, photo_id: str, timeout: float = 30) -> ReadOutcome:
        def final() -> ReadOutcome | None:
            return next((e for e in self.events if e.photo_id == photo_id and e.status in ("done", "failed")), None)

        with self._cond:
            assert self._cond.wait_for(lambda: final() is not None, timeout), f"{photo_id} 판독이 끝나지 않음"
            return final()  # type: ignore[return-value]


def make_photo(folder: Path, name: str) -> str:
    """가짜 사진 (내용은 아무 바이트 — 가짜 모듈은 해시만 쓴다)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(f"synthetic photo {name}".encode())
    return str(path)


def flow_job(photo_id: str, path: str) -> ReadJob:
    return ReadJob(photo_id, path, "prec_1", "flowmeter", expected_fields(CATEGORY, "prec_1"))


@pytest.fixture
def fake_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KTL_MODULES", "fake")  # 작업 프로세스(spawn)도 환경 변수를 물려받는다


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def read_queue(fake_modules: None, recorder: Recorder) -> Iterator[ReadQueue]:
    q = ReadQueue(workers=2, timeout=3, preload=["flowmeter"])
    q.subscribe(recorder)
    q.start()
    yield q
    q.stop()


# --- 모듈 로더 (REQ-B1-05) ---------------------------------------------------------------


def test_loader_maps_items_to_readers_from_categories() -> None:
    assert reader_name_for_item(CATEGORY, "cert") == "cert"
    assert reader_name_for_item(CATEGORY, "prec_1") == "flowmeter"
    assert reader_name_for_item(CATEGORY, "plate") is None  # reader: none
    assert reader_name_for_item(CATEGORY, "unknown") is None
    assert expected_fields(CATEGORY, "cert") == ["prev_receipt", "serial"]
    assert expected_fields(CATEGORY, "plate") == []
    assert reader_names({"categories": [CATEGORY, CATEGORY]}) == ["cert", "flowmeter"]


def test_loader_prefers_ktl_readers_and_falls_back_to_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    from ktl_contract.fakes import FakeReader

    sentinel = object()
    stub = types.ModuleType("ktl_readers")
    stub.get_reader = lambda name: sentinel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ktl_readers", stub)

    monkeypatch.setenv("KTL_MODULES", "auto")
    assert get_reader("cert") is sentinel

    monkeypatch.setenv("KTL_MODULES", "fake")
    reader = get_reader("cert")
    assert isinstance(reader, FakeReader) and reader.name == "cert"


def test_values_get_status_and_missing_fields_need_check() -> None:
    values = to_api_values(
        [
            {"key": "flow", "label": "유량", "value": "1.002", "unit": "L/min", "confidence": 0.93, "bbox": [1, 2, 3, 4]},
            {"key": "reading_no", "label": "측정 번호", "value": "01 of 003", "confidence": 0.71},
        ],
        expected_keys=["flow", "reading_no", "extra"],
    )
    assert [(v["key"], v["status"]) for v in values] == [
        ("flow", "auto"),
        ("reading_no", "needs_check"),
        ("extra", "needs_check"),
    ]
    assert values[2]["value"] == "" and values[2]["confidence"] == 0.0


# --- 작업 큐 (REQ-B1-04) -----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["fake", "auto"])
def test_fake_reader_end_to_end(
    mode: str, monkeypatch: pytest.MonkeyPatch, recorder: Recorder, tmp_path: Path
) -> None:
    """fake: ktl_contract.fakes 직접 · auto: 설치된 ktl_readers.get_reader (아직 없는 Reader는 가짜로 대체)."""
    monkeypatch.setenv("KTL_MODULES", mode)
    q = ReadQueue(workers=1, timeout=5)
    q.subscribe(recorder)
    q.start()
    try:
        q.submit(flow_job("ph_1", make_photo(tmp_path, "ph_1.jpg")))
        outcome = recorder.wait("ph_1")
    finally:
        q.stop()

    assert recorder.statuses("ph_1") == ["reading", "done"]
    assert outcome.error is None and isinstance(outcome.read_ms, int)
    by_key = {v["key"]: v for v in outcome.values}
    assert by_key["flow"]["value"] == "1.002" and by_key["flow"]["unit"] == "L/min"
    assert {v["status"] for v in outcome.values} <= {"auto", "needs_check"}
    for v in outcome.values:  # API Value 모양 (OpenAPI components.schemas.Value 필수 칸)
        assert {"key", "label", "value", "status"} <= v.keys()


def test_item_without_reader_is_done_at_once(read_queue: ReadQueue, recorder: Recorder, tmp_path: Path) -> None:
    read_queue.submit(ReadJob("ph_2", make_photo(tmp_path, "ph_2.jpg"), "plate", None))
    assert recorder.wait("ph_2", timeout=1).status == "done"
    assert recorder.statuses("ph_2") == ["done"]


def test_read_error_marks_photo(read_queue: ReadQueue, recorder: Recorder, tmp_path: Path) -> None:
    read_queue.submit(flow_job("ph_3", make_photo(tmp_path, "ph_3_fail.jpg")))
    outcome = recorder.wait("ph_3")
    assert outcome.status == "failed"
    assert outcome.error == "화면을 찾지 못했습니다 (가짜 모듈)"
    assert outcome.values == []


def test_timeout_only_that_photo_and_server_keeps_working(
    fake_modules: None, recorder: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """완료 기준: slow 파일(12초 대기) → 10초 제한으로 그 사진만 failed, 다른 사진·서버는 정상."""
    monkeypatch.setenv("KTL_ADMIN_SIDECAR", "0")
    monkeypatch.setenv("KTL_STORAGE_DIR", str(tmp_path / "storage"))
    q = ReadQueue(workers=2, preload=["flowmeter"])
    assert q.timeout == READ_TIMEOUT_SECONDS == 10
    q.subscribe(recorder)
    settings = load_settings()
    q.start()
    try:
        with TestClient(create_mobile_app(settings, read_queue=q)) as client:
            q.submit(flow_job("ph_slow", make_photo(tmp_path, "ph_slow.jpg")))
            q.submit(flow_job("ph_ok", make_photo(tmp_path, "ph_ok.jpg")))

            assert recorder.wait("ph_ok").status == "done"  # 옆 작업 프로세스는 그동안 정상 판독
            assert client.get("/api/v1/health").status_code == 200  # 판독이 멈춰 있어도 API 응답

            outcome = recorder.wait("ph_slow", timeout=30)
            assert outcome.status == "failed" and outcome.error == TIMEOUT_MESSAGE
            assert outcome.read_ms is not None  # 12초 대기를 다 기다리지 않고 10초에 강제 종료
            assert READ_TIMEOUT_SECONDS * 1000 <= outcome.read_ms < 11_500

            for i in range(3):  # 강제 종료된 작업 프로세스는 새로 떠서 다음 사진을 받는다
                q.submit(flow_job(f"ph_after_{i}", make_photo(tmp_path, f"ph_after_{i}.jpg")))
            assert [recorder.wait(f"ph_after_{i}").status for i in range(3)] == ["done"] * 3
            assert client.get("/api/v1/health").status_code == 200
    finally:
        q.stop()


STUB_READERS = '''
import os
import time
from ktl_contract import ReadResult, Value

class _Broken:
    def __init__(self, name):
        if name == "dies_on_load":
            os._exit(4)
        self.name = name

    def read(self, image_path, item):
        if item == "numpy":
            import numpy as np
            return ReadResult(values=[Value(key="flow", label="유량", value="1.000",
                                            confidence=np.float32(0.9), bbox=[np.int64(n) for n in (1, 2, 3, 4)])])
        if item == "sleep":
            time.sleep(1.5)
        if item == "boom":
            raise ValueError("모듈 버그")
        if item == "crash":
            os._exit(3)
        if item == "badtype":
            return ReadResult(values=[Value(key="flow", label="유량", value=1.0)])
        return ReadResult(values=[Value(key="flow", label="유량", value="2.000", confidence=0.9)])

def get_reader(name):
    return _Broken(name)
'''


@pytest.fixture
def stub_readers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """일부러 고장 낸 ktl_readers를 작업 프로세스가 불러오게 한다."""
    stub_dir = tmp_path / "stub"
    (stub_dir / "ktl_readers").mkdir(parents=True)
    (stub_dir / "ktl_readers" / "__init__.py").write_text(textwrap.dedent(STUB_READERS), encoding="utf-8")
    monkeypatch.syspath_prepend(str(stub_dir))  # spawn 작업 프로세스는 sys.path를 물려받는다
    monkeypatch.setenv("KTL_MODULES", "auto")


def run_stub(recorder: Recorder, tmp_path: Path, items: list[str], preload: list[str] | None = None) -> list[ReadOutcome]:
    q = ReadQueue(workers=1, timeout=5, preload=preload)
    q.subscribe(recorder)
    q.start()
    try:
        photo = make_photo(tmp_path / "img", "ph.jpg")
        for item in items:
            q.submit(ReadJob(f"ph_{item}", photo, item, "stub"))
        return [recorder.wait(f"ph_{i}") for i in items]
    finally:
        q.stop()


def test_module_bugs_and_crash_marks_only_that_photo(stub_readers: None, recorder: Recorder, tmp_path: Path) -> None:
    """ReadError가 아닌 예외 · 결과 형식 오류 · 프로세스 강제 종료도 그 사진만 failed."""
    boom, badtype, crash, ok = run_stub(recorder, tmp_path, ["boom", "badtype", "crash", "ok"])

    assert boom.status == "failed" and "ValueError" in (boom.error or "")
    assert badtype.status == "failed" and "형식 오류" in (badtype.error or "")
    assert crash.status == "failed" and crash.error == CRASH_MESSAGE
    assert ok.status == "done" and ok.values[0]["value"] == "2.000"  # 죽은 뒤 새 프로세스로 계속


def test_worker_dying_on_startup_does_not_leave_photo_reading(
    stub_readers: None, recorder: Recorder, tmp_path: Path
) -> None:
    """작업 프로세스가 Reader를 만들다 죽어도 사진이 reading에 멈추지 않고 failed가 된다."""
    (outcome,) = run_stub(recorder, tmp_path, ["ok"], preload=["dies_on_load"])
    assert outcome.status == "failed" and "시작하지 못했습니다" in (outcome.error or "")


def test_numpy_numbers_from_module_are_accepted(stub_readers: None, recorder: Recorder, tmp_path: Path) -> None:
    """실제 OCR 모듈이 흔히 내는 np.float32 신뢰도 · np.int64 bbox도 기본 타입으로 바꿔 받는다."""
    pytest.importorskip("numpy")
    (outcome,) = run_stub(recorder, tmp_path, ["numpy"])
    assert outcome.status == "done", outcome.error
    value = outcome.values[0]
    assert type(value["confidence"]) is float
    assert value["bbox"] == [1, 2, 3, 4] and all(type(n) is int for n in value["bbox"])


def test_stop_leaves_unstarted_photos_queued(stub_readers: None, recorder: Recorder, tmp_path: Path) -> None:
    """끝낼 때 판독 중인 사진만 마저 끝내고, 남은 사진은 처리하지 않는다 (종료가 N×10초로 늘지 않게)."""
    q = ReadQueue(workers=1, timeout=5)
    q.subscribe(recorder)
    q.start()
    photo = make_photo(tmp_path / "img", "ph.jpg")
    for photo_id in ("ph_first", "ph_rest_0", "ph_rest_1"):
        q.submit(ReadJob(photo_id, photo, "sleep", "stub"))
    recorder.wait_status("ph_first", "reading")
    q.stop()
    assert recorder.statuses("ph_first") == ["reading", "done"]
    assert recorder.statuses("ph_rest_0") == recorder.statuses("ph_rest_1") == []


def test_zero_workers_setting_still_runs_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KTL_READ_WORKERS", "0")
    assert ReadQueue().workers == 1


def test_app_lifespan_owns_read_queue(fake_modules: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KTL_ADMIN_SIDECAR", "0")
    monkeypatch.setenv("KTL_STORAGE_DIR", str(tmp_path / "storage"))
    app = create_mobile_app(load_settings())
    with TestClient(app):
        assert isinstance(app.state.read_queue, ReadQueue)
        assert app.state.read_queue.pending() == 0
