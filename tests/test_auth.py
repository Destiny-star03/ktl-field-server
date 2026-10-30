"""REQ-B1-10 단말 등록(코드 5분 만료) · 시험자 PIN(5회 잠금) · 해제 단말 403 · 관리 API."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import re

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.main import create_admin_app, create_mobile_app
from app.models import BASE_DIR, load_settings, local_now
from app.services import auth as auth_service

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
A, M = "/api/admin/v1", "/api/v1"
Clients = tuple[TestClient, TestClient]


@pytest.fixture()
def clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Clients]:
    monkeypatch.setenv("KTL_ADMIN_SIDECAR", "0")
    monkeypatch.setenv("KTL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("KTL_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("KTL_PUBLIC_HOST", "192.0.2.10")
    command.upgrade(Config(str(BASE_DIR / "alembic.ini")), "head")
    settings = load_settings()
    with TestClient(create_mobile_app(settings)) as mobile, TestClient(
        create_admin_app(settings), client=("127.0.0.1", 50000), headers={"Host": "127.0.0.1:8766"}
    ) as admin:
        yield mobile, admin


def _later(monkeypatch: pytest.MonkeyPatch, delta: timedelta) -> None:
    now = local_now() + delta
    monkeypatch.setattr(auth_service, "local_now", lambda: now)


def _parallel(fn: Callable[[], int], n: int) -> list[int]:
    with ThreadPoolExecutor(max_workers=n) as pool:
        return sorted(pool.map(lambda _: fn(), range(n)))


def _pair(mobile: TestClient, admin: TestClient, code: str | None = None) -> int:
    code = code or admin.post(f"{A}/pair-codes").json()["code"]
    return mobile.post(f"{M}/clients/pair", json={"pair_code": code, "device_name": "현장폰-1"}).status_code


def _ready(mobile: TestClient, admin: TestClient) -> str:
    """단말 등록 + 시험자(PIN 1234) 추가 → 시험자 ID."""
    assert _pair(mobile, admin) == 200
    return admin.post(f"{A}/testers", json={"name": "시험자B", "pin": "1234"}).json()["id"]


def _login(mobile: TestClient, tid: str, pin: str = "1234") -> int:
    return mobile.post(f"{M}/sessions", json={"tester_id": tid, "pin": pin}).status_code


def test_pair_code(clients: Clients, monkeypatch: pytest.MonkeyPatch) -> None:
    mobile, admin = clients
    body = admin.post(f"{A}/pair-codes").json()
    assert re.fullmatch(r"\d{6}", body["code"]) and ISO.match(body["expires_at"]) and body["qr_svg"].startswith("<svg")
    res = mobile.post(f"{M}/clients/pair", json={"pair_code": body["code"], "device_name": "현장폰-1"})
    assert re.fullmatch(r"cl_\d+", res.json()["client_id"]) and ISO.match(res.json()["paired_at"])
    cookie = res.headers["set-cookie"]
    assert all(a in cookie for a in ("ktl_client=", "HttpOnly", "SameSite=strict", "Max-Age=7776000"))
    assert _pair(mobile, admin, body["code"]) == 400  # 1회용
    assert len(admin.get(f"{A}/clients").json()["items"]) == 1

    code = admin.post(f"{A}/pair-codes").json()["code"]
    _later(monkeypatch, timedelta(minutes=5, seconds=1))
    res = mobile.post(f"{M}/clients/pair", json={"pair_code": code, "device_name": "현장폰-2"})
    assert res.status_code == 410 and res.json()["error"]["code"] == "PAIR_CODE_EXPIRED"


def test_pair_code_guessing_and_parallel_use(clients: Clients) -> None:
    mobile, admin = clients
    code = admin.post(f"{A}/pair-codes").json()["code"]
    assert _parallel(lambda: _pair(mobile, admin, code), 8) == [200] + [400] * 7  # 동시에 써도 한 번만

    code = admin.post(f"{A}/pair-codes").json()["code"]
    for _ in range(auth_service.MAX_PAIR_FAILURES):
        _pair(mobile, admin, f"{(int(code) + 1) % 1_000_000:06d}")
    assert _pair(mobile, admin, code) == 410  # 틀린 코드가 많이 들어오면 살아 있는 코드도 만료


def test_unpaired_401_and_revoked_403(clients: Clients) -> None:
    mobile, admin = clients
    for path in ("/testers", "/sessions/me", "/cases"):
        assert mobile.get(f"{M}{path}").json()["error"]["code"] == "CLIENT_UNKNOWN", path

    tid = _ready(mobile, admin)
    assert mobile.get(f"{M}/cases").json()["error"]["code"] == "SESSION_REQUIRED"
    assert _login(mobile, tid) == 200 and mobile.get(f"{M}/cases").status_code == 200

    assert admin.delete(f"{A}/clients/cl_1").status_code == 204
    for path in ("/testers", "/sessions/me", "/cases"):
        assert mobile.get(f"{M}{path}").json()["error"]["code"] == "CLIENT_REVOKED", path
    assert admin.delete(f"{A}/clients/cl_1").status_code == 404
    assert admin.delete(f"{A}/clients/cl_{'9' * 20}").status_code == 404  # SQLite 범위 밖도 500 아님


def test_login_me_logout(clients: Clients) -> None:
    mobile, admin = clients
    tid = _ready(mobile, admin)
    assert mobile.get(f"{M}/testers").json() == {"items": [{"id": tid, "name": "시험자B"}]}
    res = mobile.post(f"{M}/sessions", json={"tester_id": tid, "pin": "1234"})
    assert res.json()["tester"] == {"id": tid, "name": "시험자B"} and ISO.match(res.json()["expires_at"])
    assert "ktl_session=" in res.headers["set-cookie"] and "Max-Age=43200" in res.headers["set-cookie"]
    me = mobile.get(f"{M}/sessions/me").json()
    assert me["client"]["client_id"] == "cl_1" and ISO.match(me["client"]["last_seen_at"])
    assert mobile.delete(f"{M}/sessions").status_code == 204
    assert mobile.get(f"{M}/sessions/me").json()["error"]["code"] == "SESSION_REQUIRED"


def test_pin_locks_after_5_failures(clients: Clients, monkeypatch: pytest.MonkeyPatch) -> None:
    mobile, admin = clients
    tid = _ready(mobile, admin)
    for remaining in (4, 3, 2, 1):
        res = mobile.post(f"{M}/sessions", json={"tester_id": tid, "pin": "0000"})
        assert res.status_code == 401 and res.json()["error"]["detail"] == {"remaining": remaining}
    err = mobile.post(f"{M}/sessions", json={"tester_id": tid, "pin": "0000"}).json()["error"]
    assert err["code"] == "PIN_LOCKED" and 590 <= err["detail"]["retry_after"] <= 600
    assert _login(mobile, tid) == 423  # 잠긴 동안은 맞는 PIN도 거부
    assert admin.get(f"{A}/testers").json()["items"][0]["locked"] is True

    res = admin.patch(f"{A}/testers/{tid}", json={"unlock": True})
    assert res.json() == {"id": tid, "name": "시험자B", "locked": False, "active": True}
    assert _login(mobile, tid) == 200

    for _ in range(5):
        _login(mobile, tid, "0000")
    _later(monkeypatch, timedelta(minutes=10, seconds=1))  # 10분 지나면 풀림
    assert _login(mobile, tid) == 200


def test_parallel_wrong_pins_still_lock(clients: Clients) -> None:
    mobile, admin = clients
    tid = _ready(mobile, admin)
    codes = _parallel(lambda: _login(mobile, tid, "0000"), 12)
    assert codes.count(401) <= 4 and codes.count(423) >= 8  # PIN 확인은 잠금 전 최대 5번
    assert _login(mobile, tid) == 423


def test_admin_tester_management(clients: Clients) -> None:
    mobile, admin = clients
    tid = _ready(mobile, admin)
    assert admin.post(f"{A}/testers", json={"name": "시험자C", "pin": "12"}).status_code == 400
    _login(mobile, tid)
    assert admin.patch(f"{A}/testers/{tid}", json={"pin": "567890", "name": "시험자B2"}).json()["name"] == "시험자B2"
    assert mobile.get(f"{M}/sessions/me").status_code == 401  # PIN 재설정 → 로그인 끊김
    assert _login(mobile, tid) == 401 and _login(mobile, tid, "567890") == 200

    admin.patch(f"{A}/testers/{tid}", json={"active": False})
    assert mobile.get(f"{M}/testers").json() == {"items": []}
    admin.patch(f"{A}/testers/{tid}", json={"active": True})
    assert mobile.get(f"{M}/sessions/me").status_code == 401  # 다시 활성화해도 예전 로그인은 안 살아남
    assert admin.patch(f"{A}/testers/ts_{'9' * 20}", json={"unlock": True}).status_code == 404


def test_contract_details(clients: Clients) -> None:
    mobile, _ = clients
    assert mobile.get("/openapi.json").json()["components"]["securitySchemes"] == {
        "client": {"type": "apiKey", "in": "cookie", "name": "ktl_client"},
        "session": {"type": "apiKey", "in": "cookie", "name": "ktl_session"},
    }
    stored = auth_service.hash_pin("1234")
    assert "1234" not in stored and auth_service.verify_pin("1234", stored) and not auth_service.verify_pin("0", stored)
    assert auth_service.parse_id("ts_01", "ts") is None and auth_service.parse_id("cl_3", "ts") is None
