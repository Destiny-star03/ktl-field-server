"""§2.5 관리 API: CSV/TXT 업로드·병합·CSV 내보내기."""
from typing import Literal
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_settings
from app.api.errors import not_found, validation_error
from app.models import ServerSettings, get_db
from app.services import processing as service

router = APIRouter(tags=["admin"])
Status = Literal["queued", "running", "done", "failed"]


class DataFile(BaseModel):
    id: str
    case_id: str
    name: str
    sha256: str
    detected: dict | None


class MergeBody(BaseModel):
    selector: Literal["radon_merge"]
    file_ids: list[str] = Field(min_length=1)
    options: dict = Field(default_factory=dict)


class Merge(BaseModel):
    id: str
    case_id: str
    selector: str
    status: Status
    table: dict | None
    meta: dict
    warnings: list[dict]
    error: str | None


class ExportBody(BaseModel):
    kind: Literal["merge_raw"]
    format: Literal["csv"]
    encoding: Literal["cp949", "utf-8"] = "cp949"
    merge_id: str = Field(min_length=1)
    include_photos: Literal[False] = False


class Export(BaseModel):
    id: str
    case_id: str
    kind: str
    format: str
    status: Status
    file_name: str | None
    preview: dict | None
    skipped_unconfirmed: int


@router.post("/cases/{case_id}/files", response_model=DataFile, status_code=201)
async def upload_file(case_id: str, file: UploadFile = File(...), db: Session = Depends(get_db),
                      settings: ServerSettings = Depends(get_settings)):
    case = service.require_case(db, case_id, writable=True)
    record = await service.store_upload(db, case, file, settings.storage_dir)
    return service.public_record(record)


@router.get("/files/{file_id}/series")
def series(file_id: str, db: Session = Depends(get_db)):
    record = service.require_record(db, file_id, "file")
    try:
        return service.call_selector("series", [service.verified_path(record)])
    except (ValueError, OSError) as exc:
        raise validation_error({"file_id": str(exc)}) from exc


@router.post("/cases/{case_id}/merges", response_model=Merge, status_code=202)
def merge(case_id: str, body: MergeBody, tasks: BackgroundTasks, request: Request, db: Session = Depends(get_db)):
    case = service.require_case(db, case_id, writable=True)
    record = service.prepare_merge(db, case, body)
    tasks.add_task(service.process_merge, request.app.state.session_factory, record.id)
    return service.public_record(record)


@router.get("/merges/{merge_id}", response_model=Merge)
def merge_status(merge_id: str, db: Session = Depends(get_db)):
    return service.public_record(service.require_record(db, merge_id, "merge"))


@router.post("/cases/{case_id}/exports", response_model=Export, status_code=202)
def export(case_id: str, body: ExportBody, tasks: BackgroundTasks, request: Request,
           db: Session = Depends(get_db), settings: ServerSettings = Depends(get_settings)):
    case = service.require_case(db, case_id, writable=True)
    record = service.prepare_export(db, case, body)
    tasks.add_task(service.process_export, request.app.state.session_factory, record.id, settings.storage_dir)
    return service.public_record(record)


@router.get("/exports/{export_id}", response_model=Export)
def export_status(export_id: str, db: Session = Depends(get_db)):
    return service.public_record(service.require_record(db, export_id, "export"))


@router.get("/exports/{export_id}/download")
def download(export_id: str, db: Session = Depends(get_db)):
    payload = service.require_record(db, export_id, "export").payload
    if payload["status"] != "done":
        raise validation_error({"export_id": "CSV가 아직 준비되지 않았습니다. 내보내기 상태를 확인하세요."})
    if not Path(payload["path"]).is_file():
        raise not_found("생성된 CSV 파일을 찾을 수 없습니다.")
    return FileResponse(payload["path"], media_type="application/octet-stream", filename=payload["file_name"])
