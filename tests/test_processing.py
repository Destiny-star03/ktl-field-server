"""공식 관리 API 경로: 가짜 Selector로 업로드 → 병합 → CSV 다운로드 검증."""
import csv
import hashlib
import io
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.main import create_admin_app, create_mobile_app
from app.models import BASE_DIR, ProcessingRecord, load_settings
from app.services import processing
from app.services.processing import call_selector as call_selector_process

PREFIX = "/api/admin/v1"
CATEGORIES = Path(__file__).parent / "fixtures" / "synthetic" / "categories.yaml"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("KTL_ADMIN_SIDECAR", "0")
    monkeypatch.setenv("KTL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("KTL_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("KTL_CATEGORIES_PATH", str(CATEGORIES))
    command.upgrade(Config(str(BASE_DIR / "alembic.ini")), "head")
    settings = load_settings()
    app = create_admin_app(settings)
    with TestClient(app, client=("127.0.0.1", 50000), headers={"Host": "127.0.0.1:8766"}) as client:
        yield client, app, settings
    for path in settings.storage_dir.rglob("*"):
        if path.is_file():
            path.chmod(0o600)


@pytest.fixture
def fake_selector(monkeypatch):
    fakes = pytest.importorskip("ktl_contract.fakes")
    selector = fakes.FakeSelector("radon_merge")

    def call(action, paths, *, options=None):
        if action == "detect":
            return selector.detect(paths[0])
        if action == "series":
            frame = selector.series(paths)
            return {"columns": frame.columns.tolist(), "rows": [[str(row[0]), row[1]] for row in frame.values.tolist()], "unit": None}
        result = selector.run(paths, options or {})
        return {"table": {"columns": result.table.columns.tolist(), "rows": result.table.values.tolist()},
                "meta": result.meta, "warnings": []}
    monkeypatch.setattr(processing, "call_selector", call)


def create_case(client):
    response = client.post(PREFIX + "/cases", json={"receipt_no": "26-000123-01", "category": "radon", "unit_count": 1})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, case_id):
    response = client.post(f"{PREFIX}/cases/{case_id}/files", files={"file": ('synthetic,name.txt', b"SYNTHETIC DATA", "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


def merge(client, case_id, file_id):
    response = client.post(f"{PREFIX}/cases/{case_id}/merges", json={"selector": "radon_merge", "file_ids": [file_id],
                          "options": {"reference": file_id, "align": "sequence"}})
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    return client.get(f"{PREFIX}/merges/{response.json()['id']}").json()


@pytest.mark.parametrize("encoding", ["utf-8", "cp949"])
def test_upload_merge_export_download_and_persistence(environment, fake_selector, encoding):
    client, app, settings = environment
    case_id = create_case(client)
    file = upload(client, case_id)
    assert file["sha256"] == hashlib.sha256(b"SYNTHETIC DATA").hexdigest()
    assert set(file) == {"id", "case_id", "name", "sha256", "detected"}
    with app.state.session_factory() as db:
        path = Path(db.get(ProcessingRecord, file["id"]).payload["path"])
    assert path.read_bytes() == b"SYNTHETIC DATA" and path.name == file["name"]
    assert client.get(f"{PREFIX}/files/{file['id']}/series").status_code == 200
    result = merge(client, case_id, file["id"])
    assert result["status"] == "done" and result["meta"]["fake"] is True
    assert result["meta"]["options"]["reference"] == str(path)
    response = client.post(f"{PREFIX}/cases/{case_id}/exports", json={"kind": "merge_raw", "format": "csv", "encoding": encoding, "merge_id": result["id"]})
    assert response.status_code == 202 and response.json()["status"] == "queued"
    export_id = response.json()["id"]
    status = client.get(f"{PREFIX}/exports/{export_id}").json()
    assert status["status"] == "done" and len(status["preview"]["rows"]) == 12
    download = client.get(f"{PREFIX}/exports/{export_id}/download")
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    assert download.content.startswith(b"\xef\xbb\xbf") == (encoding == "utf-8")
    rows = list(csv.reader(io.StringIO(download.content.decode("utf-8-sig" if encoding == "utf-8" else encoding))))
    assert rows[0] == result["table"]["columns"]
    assert len(rows) == 13 and rows[1] == [str(value) for value in result["table"]["rows"][0]]
    with TestClient(create_admin_app(settings), client=("127.0.0.1", 50001), headers={"Host": "127.0.0.1:8766"}) as restarted:
        assert restarted.get(f"{PREFIX}/exports/{export_id}/download").content == download.content
    assert path.read_bytes() == b"SYNTHETIC DATA"


def test_foreign_case_ids_duplicates_unknown_format_and_reference(environment, fake_selector, monkeypatch):
    client, _, _ = environment
    first, second = create_case(client), create_case(client)
    file = upload(client, first)
    body = {"selector": "radon_merge", "file_ids": [file["id"]]}
    assert client.post(f"{PREFIX}/cases/{second}/merges", json=body).status_code == 404
    assert client.post(f"{PREFIX}/cases/{first}/merges", json={**body, "file_ids": [file["id"]] * 2}).status_code == 400
    assert client.post(f"{PREFIX}/cases/{first}/merges", json={**body, "options": {"reference": "file_missing"}}).status_code == 400
    monkeypatch.setattr(processing, "call_selector", lambda *args, **kwargs: None)
    unknown = upload(client, first)
    assert unknown["detected"] is None
    assert client.post(f"{PREFIX}/cases/{first}/merges", json={**body, "file_ids": [unknown["id"]]}).status_code == 400


def test_module_failure_or_modified_original_does_not_stop_server(environment, fake_selector, monkeypatch):
    client, app, _ = environment
    case_id = create_case(client)
    file = upload(client, case_id)
    with app.state.session_factory() as db:
        path = Path(db.get(ProcessingRecord, file["id"]).payload["path"])
    path.chmod(0o600)
    path.write_bytes(b"CHANGED SYNTHETIC")
    result = merge(client, case_id, file["id"])
    assert result["status"] == "failed" and "SHA-256" in result["error"]
    assert client.post(f"{PREFIX}/cases/{case_id}/exports", json={"kind": "merge_raw", "format": "csv", "merge_id": result["id"]}).status_code == 400
    assert client.get(PREFIX + "/health").status_code == 200
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("selector", 10)
    monkeypatch.setattr(processing.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="10초"):
        # fake monkeypatch를 우회해 실제 프로세스 경계의 제한 시간 처리를 확인한다.
        call_selector_process("detect", [])


def test_admin_routes_are_not_exposed_on_mobile(environment):
    _, _, settings = environment
    with TestClient(create_mobile_app(settings)) as mobile:
        assert mobile.get(PREFIX + "/exports/missing/download").status_code == 404


def test_real_parser_upload_and_series_through_worker(environment):
    pytest.importorskip("ktl_selectors")
    client, _, _ = environment
    case_id = create_case(client)
    fixture = CATEGORIES.parent / "radon-temperature.txt"
    response = client.post(f"{PREFIX}/cases/{case_id}/files", files={"file": (fixture.name, fixture.read_bytes(), "text/plain")})
    assert response.status_code == 201, response.text
    file = response.json()
    assert file["detected"]["kind"] == "radon_frd500"
    assert file["detected"]["serial"] == "SYNTHETIC-TEMPERATURE"
    assert file["detected"]["rows"] == 14
    series = client.get(f"{PREFIX}/files/{file['id']}/series")
    assert series.status_code == 200, series.text
    assert series.json()["columns"] == ["t", "radon", "temp", "humi", "barometer"]
    assert series.json()["rows"][2][0] == "2026-01-03T02:00:00"
    assert series.json()["rows"][2][2] == 20


@pytest.mark.parametrize("align,layout,column_count", [
    ("sequence", "radon_wide_v1", 5), ("hour", "radon_hour_v1", 3), (None, "radon_hour_v1", 3),
])
def test_real_run_returns_timed_values_and_downloadable_csv(environment, align, layout, column_count):
    pytest.importorskip("ktl_selectors")
    client, _, _ = environment
    case_id = create_case(client)
    file_ids = []
    for name in ("radon-chamber.csv", "radon-temperature.txt"):
        path = CATEGORIES.parent / name
        uploaded = client.post(f"{PREFIX}/cases/{case_id}/files", files={"file": (name, path.read_bytes())})
        assert uploaded.status_code == 201, uploaded.text
        file_ids.append(uploaded.json()["id"])
    options = {"reference": file_ids[0]}
    if align is not None:
        options["align"] = align
    response = client.post(f"{PREFIX}/cases/{case_id}/merges", json={"selector": "radon_merge", "file_ids": file_ids,
                           "options": options})
    assert response.status_code == 202, response.text
    result = client.get(f"{PREFIX}/merges/{response.json()['id']}").json()
    assert result["status"] == "done", result
    assert result["meta"]["layout"] == layout
    assert result["meta"]["reference_file_index"] == 1
    assert len(result["table"]["rows"]) == 13
    assert len(result["table"]["columns"]) == column_count
    assert [w["code"] for w in result["warnings"]] == ["DETAIL_LAYOUT"]
    exported = client.post(f"{PREFIX}/cases/{case_id}/exports", json={"kind": "merge_raw", "format": "csv", "encoding": "utf-8", "merge_id": result["id"]})
    assert exported.status_code == 202, exported.text
    export_id = exported.json()["id"]
    status = client.get(f"{PREFIX}/exports/{export_id}").json()
    assert status["status"] == "done" and len(status["preview"]["rows"]) == 13
    downloaded = client.get(f"{PREFIX}/exports/{export_id}/download")
    assert downloaded.status_code == 200
    records = list(csv.DictReader(io.StringIO(downloaded.content.decode("utf-8-sig"))))
    assert len(records) == 13
    chamber, device = result["meta"]["sources"]
    assert records[0][chamber["time_column"]] == "2026-01-03 02:00:00"
    assert float(records[0][chamber["value_column"]]) == 1
    assert records[12]["구분" if align == "sequence" else "측정시간"] == "평균"
    assert float(records[12][chamber["value_column"]]) == 6.5
    assert device["model_name"] == "FRD500" and device["serial_number"] == "SYNTHETIC-TEMPERATURE"
    assert device["value_column"] == "[2] SYNTHETIC-TEMPERATURE" and float(records[0][device["value_column"]]) == 20
    assert float(records[-1][device["value_column"]]) == 25.5


def test_real_run_failure_is_recorded_without_stopping_api(environment):
    pytest.importorskip("ktl_selectors")
    client, _, _ = environment
    case_id = create_case(client)
    path = CATEGORIES.parent / "radon-temperature.txt"
    file_id = client.post(f"{PREFIX}/cases/{case_id}/files", files={"file": (path.name, path.read_bytes())}).json()["id"]
    response = client.post(f"{PREFIX}/cases/{case_id}/merges", json={"selector": "radon_merge", "file_ids": [file_id], "options": {"align": "nearest"}})
    assert response.status_code == 202
    result = client.get(f"{PREFIX}/merges/{response.json()['id']}").json()
    assert result["status"] == "failed" and "hour" in result["error"]
    assert client.get(PREFIX + "/health").status_code == 200


def test_slow_detection_does_not_block_health(environment, monkeypatch):
    client, _, _ = environment
    case_id = create_case(client)
    entered, release = Event(), Event()
    def slow_detect(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        return {"kind": "radon_frd500", "rows": 14}
    monkeypatch.setattr(processing, "call_selector", slow_detect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(upload, client, case_id)
        try:
            assert entered.wait(timeout=2)
            start = time.monotonic()
            assert client.get(PREFIX + "/health").status_code == 200
            assert time.monotonic() - start < 1
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=5)["detected"]["rows"] == 14
