"""사진 업로드·조회·수정 라우터 (§2.3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_category_lookup, get_settings
from app.api.schemas import Photo
from app.models import ServerSettings, get_db
from app.services import photos as photo_service
from app.services.cases import CategoryLookup

router = APIRouter(tags=["photos"])


@router.post(
    "/cases/{case_id}/photos",
    response_model=Photo,
    status_code=202,
    summary="사진 업로드 → 원본 보관 후 즉시 queued. 같은 client_upload_id 재전송은 409 + 기존 사진",
)
def upload_photo(
    case_id: str,
    file: UploadFile = File(description="최대 20 MB, 원본 해상도"),
    unit_index: int = Form(ge=1),
    item_key: str = Form(examples=["prec_1"]),
    client_upload_id: str = Form(json_schema_extra={"format": "uuid"}),
    db: Session = Depends(get_db),
    settings: ServerSettings = Depends(get_settings),
    lookup: CategoryLookup = Depends(get_category_lookup),
    actor: str = Depends(get_actor),
) -> Photo:
    return photo_service.upload_photo(
        db,
        settings.storage_dir,
        case_id,
        lookup,
        unit_index=unit_index,
        item_key=item_key,
        client_upload_id=client_upload_id,
        file=file.file,
        actor=actor,
    )
