"""관리 API의 원본 보관 → Selector 실행 → CSV 내보내기."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
from uuid import uuid4
from starlette.concurrency import run_in_threadpool

from app.api.errors import ApiError, not_found, validation_error
from app.models import Case, ProcessingRecord
from app.services.cases import parse_case_id

SELECTOR_TIMEOUT_SECONDS = 10


def require_case(db, case_id, *, writable=False):
    case = db.get(Case, parse_case_id(case_id))
    if case is None:
        raise not_found("접수 건을 찾을 수 없습니다.")
    if writable and case.status == "completed":
        raise ApiError(409, "CASE_COMPLETED", "완료된 접수 건은 변경할 수 없습니다.")
    return case


def require_record(db, record_id, kind, *, case_id=None):
    record = db.get(ProcessingRecord, record_id)
    if record is None or record.kind != kind or (case_id is not None and record.case_id != case_id):
        raise not_found("요청한 파일 또는 작업을 찾을 수 없습니다.")
    return record


def create_record(db, case, kind, payload):
    prefix = {"file": "file", "merge": "mg", "export": "ex"}[kind]
    record = ProcessingRecord(id=f"{prefix}_{uuid4().hex}", case_id=case.id, kind=kind, payload=payload)
    db.add(record)
    db.commit()
    return record


def public_record(record):
    keys = {"file": ("name", "sha256", "detected"),
            "merge": ("selector", "status", "table", "meta", "warnings", "error"),
            "export": ("kind", "format", "status", "file_name", "preview", "skipped_unconfirmed")}[record.kind]
    return {"id": record.id, "case_id": f"case_{record.case_id}",
            **{key: record.payload.get(key) for key in keys}}


def call_selector(action, paths, *, options=None):
    # Kill a timed-out worker without stopping the server.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "app.worker.selector_job"],
            input=json.dumps({"selector": "radon_merge", "action": action, "paths": paths, "options": options or {}}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", env=env,
            cwd=Path(__file__).resolve().parents[2], timeout=SELECTOR_TIMEOUT_SECONDS, check=True)
        result = json.loads(completed.stdout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("모듈 실행이 10초를 초과했습니다.") from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError("모듈 프로세스가 올바른 결과를 반환하지 않았습니다.") from exc
    if not result["ok"]:
        raise ValueError(result["error"])
    return result["result"]


def verified_path(record):
    path = Path(record.payload["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != record.payload["sha256"]:
        raise ValueError("보관된 원본 파일의 SHA-256이 변경되었습니다.")
    return str(path)


async def store_upload(db, case, upload, storage_dir):
    name = PureWindowsPath(upload.filename or "upload.txt").name
    if (name in {"", ".", ".."} or PureWindowsPath(name).is_reserved()
            or any(char in name for char in '<>:"|?*\x00') or name.endswith((" ", "."))):
        raise validation_error({"file": "파일명이 올바르지 않습니다."})
    folder = (storage_dir / "originals" / uuid4().hex).resolve()
    folder.mkdir(parents=True)
    path = folder / name
    digest = hashlib.sha256()
    with path.open("xb") as stream:
        while chunk := await upload.read(1024 * 1024):
            digest.update(chunk)
            stream.write(chunk)
    path.chmod(0o444)
    record = create_record(db, case, "file", {"name": name, "path": str(path),
                                            "sha256": digest.hexdigest(), "detected": None})
    try:
        detected = await run_in_threadpool(call_selector, "detect", [str(path)])
    except ValueError as exc:
        raise validation_error({"file": str(exc)}) from exc
    record.payload = {**record.payload, "detected": detected}
    db.commit()
    return record


def prepare_merge(db, case, body):
    if len(set(body.file_ids)) != len(body.file_ids):
        raise validation_error({"file_ids": "같은 파일 ID를 중복 지정할 수 없습니다."})
    for file_id in body.file_ids:
        file = require_record(db, file_id, "file", case_id=case.id)
        kind = (file.payload.get("detected") or {}).get("kind", "")
        if not kind.startswith("radon_"):
            raise validation_error({"file_ids": "판별되지 않은 라돈 파일이 있습니다."})
    options = dict(body.options)
    if options.get("reference") is not None and options["reference"] not in body.file_ids:
        raise validation_error({"options.reference": "기준기는 선택한 파일 ID 중 하나여야 합니다."})
    return create_record(db, case, "merge", {"selector": body.selector, "file_ids": body.file_ids,
                         "options": options, "status": "queued", "table": None, "meta": {}, "warnings": [], "error": None})


def process_merge(factory, merge_id):
    with factory() as db:
        record = require_record(db, merge_id, "merge")
        payload = {**record.payload, "status": "running"}
        record.payload = payload
        db.commit()
        try:
            files = [require_record(db, file_id, "file", case_id=record.case_id) for file_id in payload["file_ids"]]
            paths = [verified_path(file) for file in files]
            options = dict(payload["options"])
            if options.get("reference") is not None:
                options["reference"] = paths[payload["file_ids"].index(options["reference"])]
            result = call_selector("run", paths, options=options)
            record.payload = {**payload, **result, "status": "done"}
        except Exception as exc:
            record.payload = {**payload, "status": "failed", "error": str(exc)}
        db.commit()


def prepare_export(db, case, body):
    merge = require_record(db, body.merge_id, "merge", case_id=case.id)
    if merge.payload["status"] != "done":
        raise validation_error({"merge_id": "완료된 병합 결과만 CSV로 내보낼 수 있습니다."})
    return create_record(db, case, "export", {"kind": body.kind, "format": body.format,
                         "encoding": body.encoding, "merge_id": body.merge_id, "status": "queued",
                         "file_name": None, "preview": None, "skipped_unconfirmed": 0})


def process_export(factory, export_id, storage_dir):
    with factory() as db:
        record = require_record(db, export_id, "export")
        payload = {**record.payload, "status": "running"}
        record.payload = payload
        db.commit()
        try:
            merge = require_record(db, payload["merge_id"], "merge", case_id=record.case_id)
            table = merge.payload["table"]
            header_rows = merge.payload.get("meta", {}).get("csv_header_rows", [])
            if (not isinstance(header_rows, list) or any(not isinstance(row, list)
                    or len(row) != len(table["columns"]) or any(not isinstance(value, str) for value in row)
                    for row in header_rows)):
                raise ValueError("CSV 상단 정보 행의 칼럼 수 또는 값 형식이 올바르지 않습니다.")
            csv_rows = header_rows + [table["columns"]] + table["rows"]
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, lineterminator="\r\n")
            writer.writerows(csv_rows)
            encoding = "utf-8-sig" if payload["encoding"] == "utf-8" else "cp949"
            content = stream.getvalue().encode(encoding)
            folder = (storage_dir / "exports").resolve()
            folder.mkdir(parents=True, exist_ok=True)
            name = f"case_{record.case_id}_radon_{record.id}.csv"
            path = folder / name
            with path.open("xb") as output:
                output.write(content)
            record.payload = {**payload, "status": "done", "file_name": name, "path": str(path),
                              "preview": {"columns": csv_rows[0], "rows": csv_rows[1:21]}}
        except Exception as exc:
            record.payload = {**payload, "status": "failed", "error": str(exc)}
        db.commit()
