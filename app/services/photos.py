"""사진 업로드 (REQ-B1-03, 02-API명세.md §2.3): 원본 보관 → 즉시 queued, 같은 client_upload_id는 409 + 기존 사진."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any, BinaryIO
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.errors import ApiError, not_found, validation_error
from app.api.schemas import Photo
from app.models import Case as CaseRow
from app.models import local_now, to_iso
from app.models.photos import Photo as PhotoRow
from app.services import storage
from app.services.cases import CategoryLookup, format_case_id, format_photo_id, parse_case_id

RETAKE_REASON = "다시 찍기"  # 02 §2.3: 같은 칸에 새 사진을 올리면 이전 사진은 deleted(사유 "다시 찍기")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def to_photo(row: PhotoRow) -> Photo:
    return Photo(
        id=format_photo_id(row.id),
        case_id=format_case_id(row.case_id),
        unit_index=row.unit_index,
        item_key=row.item_key,
        reader=row.reader,
        status=row.status,  # type: ignore[arg-type]
        sha256=row.sha256,
        stored_name=row.stored_name,
        uploaded_by=row.uploaded_by,
        uploaded_at=to_iso(row.uploaded_at),
        values=list(row.values or []),  # type: ignore[arg-type]
        warnings=list(row.warnings or []),  # type: ignore[arg-type]
        error=row.error,
        read_ms=row.read_ms,
    )


def _duplicate(row: PhotoRow) -> ApiError:
    # TODO(확인 필요): 계약은 "409 + 기존 사진 반환"만 정함 — 에러 형식 안 detail.photo에 넣었다
    return ApiError(
        409,
        "DUPLICATE_UPLOAD",
        "이미 받은 사진입니다. 다시 보내지 않아도 됩니다.",
        {"photo": to_photo(row).model_dump(mode="json")},
    )


def _raise_duplicate(existing: PhotoRow, case_pk: int) -> None:
    if existing.case_id != case_pk:
        # 휴대폰이 UUID를 다른 건에 다시 쓴 것 — 다른 건 사진을 "이미 받음"으로 돌려주면 칸이 찬 것으로 잘못 보인다
        raise validation_error({"client_upload_id": "다른 접수 건에서 이미 쓴 업로드 ID입니다."})
    raise _duplicate(existing)


def _find_by_upload_id(db: Session, client_upload_id: str) -> PhotoRow | None:
    return db.scalars(select(PhotoRow).where(PhotoRow.client_upload_id == client_upload_id)).first()


def normalize_upload_id(raw: str) -> str:
    """대소문자·하이픈 표기가 달라도 같은 UUID면 같은 업로드로 본다."""
    try:
        return str(uuid.UUID(raw.strip()))
    except ValueError:
        raise validation_error({"client_upload_id": "UUID 형식이어야 합니다."}) from None


def _find_item(category: Mapping[str, Any] | None, item_key: str) -> Mapping[str, Any] | None:
    for item in (category or {}).get("items") or []:
        if item.get("key") == item_key:
            return item
    return None


def upload_photo(
    db: Session,
    storage_dir: Path,
    case_id: str,
    category_lookup: CategoryLookup,
    *,
    unit_index: int,
    item_key: str,
    client_upload_id: str,
    file: BinaryIO,
    actor: str,
) -> Photo:
    upload_id = normalize_upload_id(client_upload_id)
    case_pk = parse_case_id(case_id)
    # 재전송은 건 상태와 상관없이 먼저 확인한다 (완료 직전에 보낸 사진의 재시도도 409 + 기존)
    existing = _find_by_upload_id(db, upload_id)
    if existing is not None:
        _raise_duplicate(existing, case_pk)

    case = db.get(CaseRow, case_pk)
    if case is None:
        raise not_found("접수 건을 찾을 수 없습니다.")
    if case.status != "open":
        raise ApiError(409, "CASE_COMPLETED", "완료된 건에는 사진을 추가할 수 없습니다.")
    fields: dict[str, str] = {}
    if not 1 <= unit_index <= case.unit_count:
        fields["unit_index"] = f"1~{case.unit_count} 사이여야 합니다."
    item = _find_item(category_lookup(case.category), item_key)
    if item is None or not _SAFE_KEY.match(item_key):
        fields["item_key"] = "이 분류에 없는 촬영 항목입니다."
    if fields:
        raise validation_error(fields)

    now = local_now()
    try:
        stored = storage.store_original(
            file, storage_dir, format_case_id(case.id), f"{now:%Y%m%d_%H%M%S}_{item_key}"
        )
    except storage.FileTooLarge:
        raise ApiError(413, "FILE_TOO_LARGE", storage.TOO_LARGE_MESSAGE) from None
    except storage.NotAnImage:
        raise validation_error({"file": "사진 파일(JPEG 등)이 아닙니다."}) from None

    try:
        # 같은 칸의 이전 사진은 다시 찍기로 deleted (원본 파일은 그대로 둔다)
        db.execute(
            update(PhotoRow)
            .where(
                PhotoRow.case_id == case.id,
                PhotoRow.unit_index == unit_index,
                PhotoRow.item_key == item_key,
                PhotoRow.status != "deleted",
            )
            .values(status="deleted", deleted_at=now, deleted_reason=RETAKE_REASON)
        )
        row = PhotoRow(
            case_id=case.id,
            unit_index=unit_index,
            item_key=item_key,
            reader=str(item.get("reader") or "none"),  # type: ignore[union-attr]
            status="queued",
            client_upload_id=upload_id,
            sha256=stored.sha256,
            stored_name=stored.stored_name,
            original_path=stored.rel_path,
            size_bytes=stored.size_bytes,
            uploaded_by=actor,
            uploaded_at=now,
            values=[],
            warnings=[],
        )
        db.add(row)
        # 원본을 쓰는 동안 건이 완료됐을 수 있다 — 같은 트랜잭션에서 open일 때만 갱신해 다시 확인
        still_open = db.execute(
            update(CaseRow).where(CaseRow.id == case.id, CaseRow.status == "open").values(updated_at=now)
        )
        if still_open.rowcount != 1:
            raise ApiError(409, "CASE_COMPLETED", "완료된 건에는 사진을 추가할 수 없습니다.")
        db.commit()
    except IntegrityError:
        # 같은 client_upload_id가 동시에 두 번 들어온 경우 — 늦은 쪽 원본은 지우고 먼저 저장된 사진을 돌려준다
        db.rollback()
        storage.remove_original(storage_dir, stored.rel_path)
        existing = _find_by_upload_id(db, upload_id)
        if existing is None:
            raise
        _raise_duplicate(existing, case.id)
    except BaseException:
        db.rollback()
        storage.remove_original(storage_dir, stored.rel_path)
        raise
    # TODO(#5): 판독 작업 큐(ReadQueue.submit)에 넣는 것은 #5 병합 후 여기서 연결한다.
    #   그때 reader: none은 바로 done으로, 판독 결과 저장은 status가 deleted가 아닐 때만 (다시 찍기로 지워진 사진을 되살리지 않게)
    return to_photo(row)
