"""REQ-B1-02 분류 · 접수 건 API (/categories, /cases) — OpenAPI 응답 모양과 units × items 상태."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import re

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.main import create_admin_app, create_mobile_app
from app.models import BASE_DIR, load_settings
from app.services.cases import build_units, compute_progress, item_status

CATEGORIES_YAML = Path(__file__).parent / "fixtures" / "synthetic" / "categories.yaml"
ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
ADMIN_HEADERS = {"Host": "127.0.0.1:8766"}
NEW_CASE = {"receipt_no": "26-000123-01", "category": "sampling", "unit_count": 2, "options": {"flowmeter_type": "defender"}}


@pytest.fixture()
def clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, TestClient]]:
    monkeypatch.setenv("KTL_ADMIN_SIDECAR", "0")
    monkeypatch.setenv("KTL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("KTL_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("KTL_CATEGORIES_PATH", str(CATEGORIES_YAML))
    command.upgrade(Config(str(BASE_DIR / "alembic.ini")), "head")

    settings = load_settings()
    with TestClient(create_mobile_app(settings)) as mobile, TestClient(
        create_admin_app(settings), client=("127.0.0.1", 50000), headers=ADMIN_HEADERS
    ) as admin:
        yield mobile, admin


def test_categories_returns_phone_categories(clients: tuple[TestClient, TestClient]) -> None:
    mobile, _ = clients
    res = mobile.get("/api/v1/categories")
    assert res.status_code == 200
    items = res.json()["items"]
    assert [c["category"] for c in items] == ["sampling"]  # phone: false(가스)는 빠짐
    sampling = items[0]
    assert set(sampling) == {"category", "label", "options", "items", "export"}
    assert sampling["options"][0] == {"key": "flowmeter_type", "label": "기준 유량계", "choices": ["defender", "alicat"]}
    assert sampling["items"][1] == {"key": "plate", "label": "형식승인표", "reader": "none", "required": True}
    assert sampling["export"] == ["photo_first", "cert.approval_no", "prec_1.flow"]


def test_create_case_matches_contract(clients: tuple[TestClient, TestClient]) -> None:
    mobile, _ = clients
    res = mobile.post("/api/v1/cases", json=NEW_CASE)
    assert res.status_code == 201
    body = res.json()
    assert re.fullmatch(r"case_\d+", body["id"])
    assert body["receipt_no"] == "26-000123-01"
    assert body["category"] == "sampling"
    assert body["unit_count"] == 2
    assert body["status"] == "open"
    assert body["options"] == {"flowmeter_type": "defender"}
    assert isinstance(body["created_by"], str)
    assert ISO_WITH_OFFSET.match(body["created_at"])
    assert body["updated_at"] == body["created_at"]
    assert body["progress"] == {"total": 6, "confirmed": 0, "needs_check": 0}

    # units × items: 기기 2대 × 항목 3개, 모두 empty
    assert [u["index"] for u in body["units"]] == [1, 2]
    for unit in body["units"]:
        assert [i["key"] for i in unit["items"]] == ["cert", "plate", "prec_1"]
        for item in unit["items"]:
            assert item["photo_id"] is None
            assert item["status"] == "empty"
            assert item["required"] is True
    assert body["units"][0]["items"][0]["label"] == "필증 (정도검사 증명서)"

    detail = mobile.get(f"/api/v1/cases/{body['id']}")
    assert detail.status_code == 200
    assert detail.json() == body


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({**NEW_CASE, "receipt_no": "26-123-01"}, "receipt_no"),
        ({**NEW_CASE, "unit_count": 0}, "unit_count"),
        ({**NEW_CASE, "unit_count": 51}, "unit_count"),
        ({**NEW_CASE, "category": "nope"}, "category"),
        ({**NEW_CASE, "category": "gas", "options": {}}, "category"),  # 휴대폰에서 phone: false 분류
        ({**NEW_CASE, "options": {"flowmeter_type": "unknown"}}, "options.flowmeter_type"),
        ({**NEW_CASE, "options": {"color": "red"}}, "options.color"),
    ],
)
def test_create_case_validation(clients: tuple[TestClient, TestClient], payload: dict, field: str) -> None:
    mobile, _ = clients
    res = mobile.post("/api/v1/cases", json=payload)
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["code"] == "VALIDATION"
    assert field in err["detail"]["fields"]


def test_create_case_invalid_json(clients: tuple[TestClient, TestClient]) -> None:
    mobile, _ = clients
    res = mobile.post("/api/v1/cases", content=b"{bad", headers={"content-type": "application/json"})
    assert res.status_code == 400
    assert list(res.json()["error"]["detail"]["fields"]) == ["body"]


def test_admin_shares_cases_and_can_create_file_category(clients: tuple[TestClient, TestClient]) -> None:
    mobile, admin = clients
    created = mobile.post("/api/v1/cases", json=NEW_CASE).json()
    assert admin.get(f"/api/admin/v1/cases/{created['id']}").json()["id"] == created["id"]

    gas = admin.post("/api/admin/v1/cases", json={"receipt_no": "26-000123-02", "category": "gas", "unit_count": 1})
    assert gas.status_code == 201
    assert gas.json()["created_by"] == "admin"
    assert gas.json()["units"] == [{"index": 1, "items": []}]


def test_case_not_found(clients: tuple[TestClient, TestClient]) -> None:
    mobile, _ = clients
    for case_id in ("case_999", "abc", "case_0", "case_01"):
        res = mobile.get(f"/api/v1/cases/{case_id}")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"


def test_list_cases_filter_and_pagination(clients: tuple[TestClient, TestClient]) -> None:
    mobile, _ = clients
    ids = [
        mobile.post("/api/v1/cases", json={**NEW_CASE, "receipt_no": f"26-000123-{n:02d}"}).json()["id"]
        for n in range(1, 6)
    ]

    page1 = mobile.get("/api/v1/cases", params={"limit": 2}).json()
    assert [c["id"] for c in page1["items"]] == [ids[4], ids[3]]  # 최근 건부터
    assert set(page1["items"][0]) == {"id", "receipt_no", "category", "unit_count", "status", "progress", "updated_at"}
    page2 = mobile.get("/api/v1/cases", params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [c["id"] for c in page2["items"]] == [ids[2], ids[1]]
    page3 = mobile.get("/api/v1/cases", params={"limit": 2, "cursor": page2["next_cursor"]}).json()
    assert [c["id"] for c in page3["items"]] == [ids[0]]
    assert page3["next_cursor"] is None

    found = mobile.get("/api/v1/cases", params={"q": "123-03"}).json()["items"]
    assert [c["receipt_no"] for c in found] == ["26-000123-03"]
    assert mobile.get("/api/v1/cases", params={"q": "%"}).json()["items"] == []  # LIKE 와일드카드는 글자 그대로
    assert len(mobile.get("/api/v1/cases", params={"status": "open"}).json()["items"]) == 5
    assert mobile.get("/api/v1/cases", params={"status": "completed"}).json()["items"] == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"status": "closed"}, {"cursor": "xyz"}])
def test_list_cases_validation(clients: tuple[TestClient, TestClient], params: dict) -> None:
    mobile, _ = clients
    res = mobile.get("/api/v1/cases", params=params)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION"


def test_unknown_route_uses_error_format(clients: tuple[TestClient, TestClient]) -> None:
    mobile, _ = clients
    res = mobile.get("/api/v1/nope")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_openapi_lists_case_paths(clients: tuple[TestClient, TestClient]) -> None:
    mobile, admin = clients
    mobile_paths = mobile.get("/openapi.json").json()["paths"]
    assert {"/api/v1/categories", "/api/v1/cases", "/api/v1/cases/{case_id}"} <= set(mobile_paths)
    assert "201" in mobile_paths["/api/v1/cases"]["post"]["responses"]
    admin_paths = admin.get("/openapi.json").json()["paths"]
    assert {"/api/admin/v1/cases", "/api/admin/v1/cases/{case_id}"} <= set(admin_paths)


@pytest.mark.parametrize(
    ("photo_status", "values", "expected"),
    [
        (None, [], "empty"),
        ("deleted", ["auto"], "empty"),
        ("queued", [], "queued"),
        ("reading", [], "reading"),
        ("failed", [], "failed"),
        ("done", [], "done"),  # reader: none (사진만)
        ("done", ["auto", "confirmed"], "done"),
        ("done", ["auto", "needs_check"], "needs_check"),
        ("done", ["confirmed", "edited"], "confirmed"),
    ],
)
def test_item_status(photo_status: str | None, values: list[str], expected: str) -> None:
    assert item_status(photo_status, values) == expected


def test_build_units_uses_photo_slots() -> None:
    category = {"items": [{"key": "cert", "label": "필증", "required": True}, {"key": "env", "label": "환경", "required": False}]}
    units = build_units(category, 2, {(2, "cert"): ("ph_1", "needs_check"), (1, "env"): ("ph_2", "confirmed")})
    assert units[1].items[0].photo_id == "ph_1"
    assert units[1].items[0].status == "needs_check"
    assert units[0].items[1].required is False
    assert compute_progress(units).model_dump() == {"total": 4, "confirmed": 1, "needs_check": 1}
