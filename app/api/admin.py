"""관리 전용 API 라우터 (127.0.0.1:8766/api/admin/v1 §2.5)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import ClientOut, TesterList, TesterOut
from app.api.errors import not_found
from app.models import get_db, to_iso
from app.models.auth import Client, Tester
from app.services import auth as auth_service

router = APIRouter(tags=["admin"])


class PairCodeOut(BaseModel):
    code: str
    expires_at: str
    qr_svg: str


class ClientList(BaseModel):
    items: list[ClientOut]


class TesterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    pin: str = Field(pattern=r"^\d{4,6}$")


class TesterPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    pin: str | None = Field(default=None, pattern=r"^\d{4,6}$")
    unlock: bool | None = None
    active: bool | None = None


@router.get("/health", summary="관리 API 상태 확인")
def admin_health() -> dict[str, str]:
    """관리 API 서버 동작 상태를 반환한다."""
    return {"status": "ok", "app": "admin"}


@router.post("/pair-codes", status_code=201, response_model=PairCodeOut, summary="단말 등록 코드 발급 (5분 만료)")
def create_pair_code(request: Request, db: Session = Depends(get_db)) -> PairCodeOut:
    row = auth_service.issue_pair_code(db)
    svg = auth_service.pair_qr_svg(row.code, request.app.state.settings.mobile_port)
    return PairCodeOut(code=row.code, expires_at=to_iso(row.expires_at), qr_svg=svg)


@router.get("/clients", response_model=ClientList, summary="등록 단말 목록")
def list_clients(db: Session = Depends(get_db)) -> ClientList:
    rows = db.scalars(select(Client).where(Client.revoked_at.is_(None)).order_by(Client.id))
    return ClientList(items=[ClientOut.of(row) for row in rows])


@router.delete("/clients/{client_id}", status_code=204, summary="단말 해제 (이후 요청 403)")
def revoke_client(client_id: str, db: Session = Depends(get_db)) -> Response:
    pk = auth_service.parse_id(client_id, "cl")
    client = db.get(Client, pk) if pk is not None else None
    if client is None or client.revoked_at is not None:
        raise not_found("단말을 찾을 수 없습니다.")
    client.revoked_at = auth_service.local_now()
    auth_service.end_sessions(db, client_id=client.id)
    db.commit()
    return Response(status_code=204)


@router.get("/testers", response_model=TesterList, summary="시험자 목록 (잠금·활성 포함)")
def list_testers(db: Session = Depends(get_db)) -> TesterList:
    return TesterList(items=[TesterOut.of(row, full=True) for row in db.scalars(select(Tester).order_by(Tester.id))])


@router.post("/testers", status_code=201, response_model=TesterOut, summary="시험자 추가")
def create_tester(body: TesterCreate, db: Session = Depends(get_db)) -> TesterOut:
    tester = Tester(name=body.name, pin_hash=auth_service.hash_pin(body.pin), failed_count=0, active=True)
    db.add(tester)
    db.commit()
    return TesterOut.of(tester, full=True)


@router.patch("/testers/{tester_id}", response_model=TesterOut, summary="시험자 수정 (이름, PIN 재설정, 잠금 해제, 비활성)")
def update_tester(tester_id: str, body: TesterPatch, db: Session = Depends(get_db)) -> TesterOut:
    pk = auth_service.parse_id(tester_id, "ts")
    tester = db.get(Tester, pk) if pk is not None else None
    if tester is None:
        raise not_found("시험자를 찾을 수 없습니다.")
    if body.name is not None:
        tester.name = body.name
    if body.pin is not None:
        tester.pin_hash = auth_service.hash_pin(body.pin)
    if body.unlock or body.pin is not None:  # PIN을 새로 정하면 잠금도 푼다
        tester.failed_count, tester.locked_until = 0, None
    if body.active is not None:
        tester.active = body.active
    if body.pin is not None or body.active is False:  # 다시 활성화해도 예전 로그인은 살아나지 않게
        auth_service.end_sessions(db, tester_id=tester.id)
    db.commit()
    return TesterOut.of(tester, full=True)
