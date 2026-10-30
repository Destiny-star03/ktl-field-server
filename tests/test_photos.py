"""REQ-B1-03 사진 업로드 — 202 queued 즉시 · 같은 client_upload_id는 409 + 기존 · 원본 읽기 전용 + SHA-256."""
from __future__ import annotations

from collections.abc import Iterator
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.main import create_admin_app, create_mobile_app
from app.models import BASE_DIR, load_settings
from app.services import storage

CATEGORIES_YAML = Path(__file__).parent / "fixtures" / "synthetic" / "categories.yaml"
ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
ADMIN_HEADERS = {"Host": "127.0.0.1:8766"}
NEW_CASE = {"receipt_no": "26-000123-01", "category": "sampling", "unit_count": 2, "options": {}}
# 가짜 JPEG: SOI 마커 + 임의 바이트 (실제 사진 아님)
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-test-image" * 50


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, TestClient, Path]]:
    storage_dir = tmp_path / "storage"
    monkeypatch.setenv("KTL_ADMIN_SIDECAR", "0")
    monkeypatch.setenv("KTL_STORAGE_DIR", str(storage_dir))
    monkeypatch.setenv("KTL_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("KTL_CATEGORIES_PATH", str(CATEGORIES_YAML))
    command.upgrade(Config(str(BASE_DIR / "alembic.ini")), "head")

    settings = load_settings()
    with TestClient(create_mobile_app(settings)) as mobile, TestClient(
        create_admin_app(settings), client=("127.0.0.1", 50000), headers=ADMIN_HEADERS
    ) as admin:
        code = admin.post("/api/admin/v1/pair-codes").json()["code"]
        mobile.post("/api/v1/clients/pair", json={"pair_code": code, "device_name": "테스트폰"})
        tester = admin.post("/api/admin/v1/testers", json={"name": "시험자A", "pin": "1234"}).json()
        mobile.post("/api/v1/sessions", json={"tester_id": tester["id"], "pin": "1234"})
        yield mobile, admin, storage_dir
    _make_writable(storage_dir)  # Windows에서 tmp_path 정리가 읽기 전용 파일에 막히지 않게


def _make_writable(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)


def _upload(
    client: TestClient,
    case_id: str,
    *,
    upload_id: str | None = None,
    unit_index: int = 1,
    item_key: str = "prec_1",
    data: bytes = FAKE_JPEG,
    prefix: str = "/api/v1",
):
    return client.post(
        f"{prefix}/cases/{case_id}/photos",
        files={"file": ("IMG_0001.jpg", data, "image/jpeg")},
        data={"unit_index": str(unit_index), "item_key": item_key, "client_upload_id": upload_id or str(uuid.uuid4())},
    )


def _new_case(client: TestClient) -> str:
    return client.post("/api/v1/cases", json=NEW_CASE).json()["id"]


def test_upload_returns_202_queued_with_contract_shape(env) -> None:
    mobile, _, storage_dir = env
    case_id = _new_case(mobile)
    res = _upload(mobile, case_id)
    assert res.status_code == 202
    body = res.json()
    assert set(body) == {
        "id", "case_id", "unit_index", "item_key", "reader", "status", "sha256", "stored_name",
        "uploaded_by", "uploaded_at", "values", "warnings", "error", "read_ms",
    }
    assert re.fullmatch(r"ph_\d+", body["id"])
    assert body["case_id"] == case_id
    assert (body["unit_index"], body["item_key"], body["reader"]) == (1, "prec_1", "flowmeter")
    assert body["status"] == "queued"
    assert body["sha256"] == hashlib.sha256(FAKE_JPEG).hexdigest()
    assert re.fullmatch(r"\d{8}_\d{6}_prec_1\.jpg", body["stored_name"])
    assert body["uploaded_by"] == "ts_1"
    assert ISO_WITH_OFFSET.match(body["uploaded_at"])
    assert (body["values"], body["warnings"], body["error"], body["read_ms"]) == ([], [], None, None)

    # 칸 상태가 queued로 바뀐다
    case = mobile.get(f"/api/v1/cases/{case_id}").json()
    slot = case["units"][0]["items"][2]
    assert (slot["photo_id"], slot["status"]) == (body["id"], "queued")


def test_original_is_read_only_and_hash_matches(env) -> None:
    mobile, _, storage_dir = env
    case_id = _new_case(mobile)
    body = _upload(mobile, case_id).json()
    path = storage_dir / "originals" / case_id / body["stored_name"]
    assert path.read_bytes() == FAKE_JPEG
    assert hashlib.sha256(path.read_bytes()).hexdigest() == body["sha256"]
    assert not os.access(path, os.W_OK)
    with pytest.raises(PermissionError):
        path.open("r+b")
    # 임시 파일이 남지 않는다
    assert [p.name for p in path.parent.iterdir()] == [body["stored_name"]]


def test_same_client_upload_id_returns_409_with_existing_photo(env) -> None:
    mobile, _, storage_dir = env
    case_id = _new_case(mobile)
    upload_id = str(uuid.uuid4())
    first = _upload(mobile, case_id, upload_id=upload_id).json()

    # 다른 내용 · 대문자 표기로 다시 보내도 같은 업로드 — 저장하지 않는다
    res = _upload(mobile, case_id, upload_id=upload_id.upper(), data=FAKE_JPEG + b"x")
    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "DUPLICATE_UPLOAD"
    assert err["detail"]["photo"] == first
    assert len(list((storage_dir / "originals" / case_id).iterdir())) == 1
    assert len(mobile.get(f"/api/v1/cases/{case_id}").json()["units"][0]["items"]) == 3


def test_new_photo_in_same_slot_replaces_previous(env) -> None:
    mobile, _, storage_dir = env
    case_id = _new_case(mobile)
    first = _upload(mobile, case_id).json()
    second = _upload(mobile, case_id).json()
    assert second["id"] != first["id"]
    # 같은 초에 올려도 기존 원본을 덮어쓰지 않는다
    assert second["stored_name"] != first["stored_name"]
    assert len(list((storage_dir / "originals" / case_id).iterdir())) == 2
    slot = mobile.get(f"/api/v1/cases/{case_id}").json()["units"][0]["items"][2]
    assert slot["photo_id"] == second["id"]


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"unit_index": 3}, "unit_index"),
        ({"item_key": "nope"}, "item_key"),
        ({"upload_id": "not-a-uuid"}, "client_upload_id"),
        ({"data": b"plain text, not an image"}, "file"),
    ],
)
def test_upload_validation_errors(env, kwargs: dict, field: str) -> None:
    mobile, _, storage_dir = env
    case_id = _new_case(mobile)
    res = _upload(mobile, case_id, **kwargs)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION"
    assert field in res.json()["error"]["detail"]["fields"]
    assert not (storage_dir / "originals" / case_id).exists() or not any((storage_dir / "originals" / case_id).iterdir())


def test_upload_unknown_case_404(env) -> None:
    mobile, _, _ = env
    res = _upload(mobile, "case_999")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_completed_case_rejects_upload_409(env, tmp_path: Path) -> None:
    mobile, _, _ = env
    case_id = _new_case(mobile)
    upload_id = str(uuid.uuid4())
    first = _upload(mobile, case_id, upload_id=upload_id).json()
    with sqlite3.connect(tmp_path / "test.db") as conn:  # 건 완료 API는 다른 이슈 — DB로 직접 완료 처리
        conn.execute("UPDATE cases SET status = 'completed'")
    res = _upload(mobile, case_id)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "CASE_COMPLETED"
    # 완료 전에 보낸 사진의 재전송은 여전히 409 DUPLICATE_UPLOAD + 기존 사진
    res = _upload(mobile, case_id, upload_id=upload_id)
    assert res.json()["error"]["code"] == "DUPLICATE_UPLOAD"
    assert res.json()["error"]["detail"]["photo"]["id"] == first["id"]


def test_file_too_large_413(env, monkeypatch: pytest.MonkeyPatch) -> None:
    mobile, _, storage_dir = env
    monkeypatch.setattr(storage, "MAX_PHOTO_BYTES", len(FAKE_JPEG) - 1)
    case_id = _new_case(mobile)
    res = _upload(mobile, case_id)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert not any((storage_dir / "originals" / case_id).iterdir())


def test_upload_requires_login(env) -> None:
    mobile, _, _ = env
    case_id = _new_case(mobile)
    mobile.cookies.clear()
    assert _upload(mobile, case_id).status_code == 401


def test_admin_api_can_upload_too(env) -> None:
    mobile, admin, _ = env
    case_id = _new_case(mobile)
    res = _upload(admin, case_id, prefix="/api/admin/v1")
    assert res.status_code == 202
    assert res.json()["uploaded_by"] == "admin"


def test_openapi_documents_upload(env) -> None:
    mobile, _, _ = env
    op = mobile.get("/openapi.json").json()["paths"]["/api/v1/cases/{case_id}/photos"]["post"]
    assert "202" in op["responses"]
    schema_ref = op["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    assert schema_ref.startswith("#/components/schemas/")


@pytest.mark.parametrize("chunked", [False, True])
def test_oversized_body_is_cut_before_parsing(env, chunked: bool) -> None:
    """20 MB를 넘는 본문은 multipart를 끝까지 받지 않고 413 (디스크를 채우지 않게)."""
    mobile, admin, storage_dir = env
    case_id = _new_case(mobile)
    big = b"\xff\xd8\xff\xe0" + b"\0" * (storage.MAX_PHOTO_BYTES + 128 * 1024)
    boundary = "testboundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.jpg\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode() + big + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    step = 1 << 20
    for client, prefix in ((mobile, "/api/v1"), (admin, "/api/admin/v1")):
        # chunked: Content-Length 없이 1 MB씩 — 미들웨어가 받은 양을 세어 끊는다
        content = (body[i : i + step] for i in range(0, len(body), step)) if chunked else body
        res = client.post(f"{prefix}/cases/{case_id}/photos", content=content, headers=headers)
        assert res.status_code == 413, res.text
        assert res.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert not (storage_dir / "originals" / case_id).exists()


def test_upload_id_reused_on_other_case_is_rejected(env) -> None:
    mobile, _, _ = env
    case_a, case_b = _new_case(mobile), _new_case(mobile)
    upload_id = str(uuid.uuid4())
    assert _upload(mobile, case_a, upload_id=upload_id).status_code == 202
    res = _upload(mobile, case_b, upload_id=upload_id)
    assert res.status_code == 400
    assert "client_upload_id" in res.json()["error"]["detail"]["fields"]
    assert "photo" not in res.json()["error"].get("detail", {})


def test_case_completed_while_storing_is_rejected(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """원본을 쓰는 동안 건이 완료되면 사진을 남기지 않고 409 CASE_COMPLETED."""
    mobile, _, storage_dir = env
    case_id = _new_case(mobile)
    real_store = storage.store_original

    def store_then_complete(*args, **kwargs):
        stored = real_store(*args, **kwargs)
        with sqlite3.connect(tmp_path / "test.db") as conn:
            conn.execute("UPDATE cases SET status = 'completed'")
        return stored

    monkeypatch.setattr(storage, "store_original", store_then_complete)
    res = _upload(mobile, case_id)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "CASE_COMPLETED"
    assert not any((storage_dir / "originals" / case_id).iterdir())
    with sqlite3.connect(tmp_path / "test.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0


def test_admin_rejects_non_loopback_before_size_check(env) -> None:
    _, _, _ = env
    settings = load_settings()
    with TestClient(create_admin_app(settings), client=("192.168.8.20", 50000), headers=ADMIN_HEADERS) as outsider:
        res = outsider.post(
            "/api/admin/v1/cases/case_1/photos",
            content=b"x",
            headers={"Content-Length": str(100 * 1024 * 1024), "Content-Type": "multipart/form-data; boundary=b"},
        )
    assert res.status_code == 403
