"""단말 · 시험자 · 등록 코드 · 로그인 세션 테이블 (REQ-B1-10). 토큰과 PIN은 해시만 저장한다 (NFR-04)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, local_now

_TS = DateTime(timezone=True)


class Tester(Base):  # 외부 ID `ts_<id>`
    __tablename__ = "testers"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    pin_hash: Mapped[str] = mapped_column(String(128))
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(_TS)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(_TS, default=local_now)


class Client(Base):  # 등록된 단말. 외부 ID `cl_<id>`
    __tablename__ = "clients"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_name: Mapped[str] = mapped_column(String(40))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    paired_at: Mapped[datetime] = mapped_column(_TS, default=local_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(_TS)
    revoked_at: Mapped[datetime | None] = mapped_column(_TS)


class PairCode(Base):  # 1회용 등록 코드 (6자리, 5분 만료)
    __tablename__ = "pair_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(6), index=True)
    expires_at: Mapped[datetime] = mapped_column(_TS)
    used_at: Mapped[datetime | None] = mapped_column(_TS)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)  # 살아 있는 동안 들어온 틀린 코드 수


class LoginSession(Base):  # 시험자 로그인 (`ktl_session` 쿠키, 12시간)
    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    tester_id: Mapped[int] = mapped_column(ForeignKey("testers.id"))
    expires_at: Mapped[datetime] = mapped_column(_TS)
