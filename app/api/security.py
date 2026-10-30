"""휴대폰 API 쿠키 인증 (02-API명세.md §1: `ktl_client` + `ktl_session`).

미등록 단말 401 CLIENT_UNKNOWN · 해제 단말 403 CLIENT_REVOKED · 로그인 안 함·만료 401 SESSION_REQUIRED
"""
from __future__ import annotations

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.models import get_db, local_now
from app.models.auth import Client, LoginSession, Tester
from app.services.auth import as_local, hash_token

CLIENT_COOKIE = "ktl_client"
SESSION_COOKIE = "ktl_session"
# 계약 components.securitySchemes의 이름(client · session) 그대로 /docs에 나오게
_client_scheme = APIKeyCookie(name=CLIENT_COOKIE, scheme_name="client", auto_error=False)
_session_scheme = APIKeyCookie(name=SESSION_COOKIE, scheme_name="session", auto_error=False)


def require_client(
    request: Request, token: str | None = Security(_client_scheme), db: Session = Depends(get_db)
) -> Client:
    client = db.scalars(select(Client).where(Client.token_hash == hash_token(token))).first() if token else None
    if client is None:
        raise ApiError(401, "CLIENT_UNKNOWN", "등록되지 않은 단말입니다. 노트북에서 등록 코드를 받아 등록하세요.")
    if client.revoked_at is not None:
        raise ApiError(403, "CLIENT_REVOKED", "해제된 단말입니다. 노트북 담당에게 다시 등록을 요청하세요.")
    client.last_seen_at = local_now()
    db.commit()
    request.state.client = client
    return client


def require_session(
    request: Request,
    token: str | None = Security(_session_scheme),
    client: Client = Depends(require_client),
    db: Session = Depends(get_db),
) -> Tester:
    query = select(LoginSession).where(LoginSession.token_hash == hash_token(token or ""))
    sess = db.scalars(query).first() if token else None
    tester = db.get(Tester, sess.tester_id) if sess is not None else None
    if sess is None or tester is None or not tester.active or sess.client_id != client.id:
        raise ApiError(401, "SESSION_REQUIRED", "시험자 로그인이 필요합니다.")
    if as_local(sess.expires_at) <= local_now():
        raise ApiError(401, "SESSION_REQUIRED", "로그인이 만료되었습니다. 다시 로그인하세요.")
    request.state.tester = tester
    return tester
