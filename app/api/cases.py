"""접수 건 라우터 (§2.2 — 휴대폰·관리 API 공용 /cases)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_actor, get_category_lookup, is_phone_app
from app.api.schemas import Case, CaseCreate, CaseList
from app.models import get_db
from app.services import cases as case_service
from app.services.cases import CategoryLookup

router = APIRouter(tags=["cases"])


@router.get("/cases", response_model=CaseList, summary="접수 건 목록")
def list_cases(
    status: Literal["open", "completed"] | None = None,
    q: str | None = Query(default=None, description="접수번호 일부"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    db: Session = Depends(get_db),
    lookup: CategoryLookup = Depends(get_category_lookup),
) -> CaseList:
    items, next_cursor = case_service.list_cases(db, lookup, status=status, q=q, limit=limit, cursor=cursor)
    return CaseList(items=items, next_cursor=next_cursor)


@router.post("/cases", response_model=Case, status_code=201, summary="새 접수 건")
def create_case(
    body: CaseCreate,
    db: Session = Depends(get_db),
    lookup: CategoryLookup = Depends(get_category_lookup),
    actor: str = Depends(get_actor),
    phone_only: bool = Depends(is_phone_app),
) -> Case:
    return case_service.create_case(db, body, lookup(body.category), actor, phone_only=phone_only)


@router.get("/cases/{case_id}", response_model=Case, summary="접수 건 상세 (기기별·항목별 상태)")
def get_case(
    case_id: str,
    db: Session = Depends(get_db),
    lookup: CategoryLookup = Depends(get_category_lookup),
) -> Case:
    return case_service.get_case(db, case_id, lookup)
