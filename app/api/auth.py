"""단말 · 로그인 라우터 (휴대폰 API §2.1, REQ-B1-10)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.security import CLIENT_COOKIE, SESSION_COOKIE, require_client, require_session
from app.models import get_db, to_iso
from app.models.auth import Client, LoginSession, Tester
from app.services import auth as auth_service

router = APIRouter(tags=["auth"])


class PairRequest(BaseModel):
    pair_code: str = Field(pattern=r"^\d{6}$", examples=["483920"])
    device_name: str = Field(min_length=1, max_length=40, examples=["현장폰-1"])


class LoginRequest(BaseModel):
    tester_id: str = Field(examples=["ts_2"])
    pin: str = Field(pattern=r"^\d{4,6}$", examples=["1234"])


class ClientOut(BaseModel):
    client_id: str
    device_name: str
    paired_at: str
    last_seen_at: str | None = None

    @classmethod
    def of(cls, row: Client) -> ClientOut:
        seen = to_iso(row.last_seen_at) if row.last_seen_at else None
        return cls(client_id=f"cl_{row.id}", device_name=row.device_name, paired_at=to_iso(row.paired_at),
                   last_seen_at=seen)


class TesterOut(BaseModel):
    id: str
    name: str
    locked: bool | None = None  # 관리 API에서만
    active: bool | None = None

    @classmethod
    def of(cls, row: Tester, *, full: bool = False) -> TesterOut:
        extra = {"locked": auth_service.is_locked(row), "active": row.active} if full else {}
        return cls(id=f"ts_{row.id}", name=row.name, **extra)


class TesterList(BaseModel):
    items: list[TesterOut]


class LoginOut(BaseModel):
    tester: TesterOut
    expires_at: str


class MeOut(BaseModel):
    client: ClientOut
    tester: TesterOut


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, path="/", httponly=True, samesite="strict")


@router.get("/health", summary="휴대폰 API 상태 확인")
def mobile_health() -> dict[str, str]:
    """휴대폰 API 서버 동작 상태를 반환한다."""
    return {"status": "ok", "app": "mobile"}


@router.post("/clients/pair", response_model=ClientOut, summary="등록 코드로 단말 등록 (ktl_client 쿠키 발급)")
def pair(body: PairRequest, response: Response, db: Session = Depends(get_db)) -> ClientOut:
    client, token = auth_service.pair_client(db, body.pair_code, body.device_name)
    _set_cookie(response, CLIENT_COOKIE, token, auth_service.CLIENT_COOKIE_MAX_AGE)
    return ClientOut.of(client)


@router.get("/testers", response_model=TesterList, response_model_exclude_none=True, summary="시험자 목록 (이름만)")
def list_testers(_client: Client = Depends(require_client), db: Session = Depends(get_db)) -> TesterList:
    rows = db.scalars(select(Tester).where(Tester.active.is_(True)).order_by(Tester.id))
    return TesterList(items=[TesterOut.of(row) for row in rows])


@router.post("/sessions", response_model=LoginOut, response_model_exclude_none=True, summary="시험자 로그인 (5회 실패 시 잠금)")
def create_session(
    body: LoginRequest, response: Response, client: Client = Depends(require_client), db: Session = Depends(get_db)
) -> LoginOut:
    sess, tester, token = auth_service.login(db, client, body.tester_id, body.pin)
    _set_cookie(response, SESSION_COOKIE, token, int(auth_service.SESSION_TTL.total_seconds()))
    return LoginOut(tester=TesterOut.of(tester), expires_at=to_iso(sess.expires_at))


@router.delete("/sessions", status_code=204, summary="로그아웃")
def delete_session(
    request: Request, _client: Client = Depends(require_client), db: Session = Depends(get_db)
) -> Response:
    if token := request.cookies.get(SESSION_COOKIE):
        db.execute(delete(LoginSession).where(LoginSession.token_hash == auth_service.hash_token(token)))
        db.commit()
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@router.get("/sessions/me", response_model=MeOut, response_model_exclude_none=True, summary="현재 단말·시험자")
def me(request: Request, tester: Tester = Depends(require_session)) -> MeOut:
    return MeOut(client=ClientOut.of(request.state.client), tester=TesterOut.of(tester))
