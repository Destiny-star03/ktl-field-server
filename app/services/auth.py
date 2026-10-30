"""단말 등록 · 시험자 PIN 로그인 규칙 (REQ-B1-10, 02-API명세.md §1 · §2.1 · §2.5)."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import os
import re
import secrets
import socket
from typing import Any

import segno
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api.errors import ApiError, validation_error
from app.models import local_now
from app.models.auth import Client, LoginSession, PairCode, Tester

PAIR_CODE_TTL = timedelta(minutes=5)
CLIENT_COOKIE_MAX_AGE = 90 * 24 * 3600
SESSION_TTL = timedelta(hours=12)
MAX_PIN_FAILURES = 5
PIN_LOCK = timedelta(minutes=10)
# TODO(확인 필요): 틀린 등록 코드를 몇 번까지 허용할지 — 넘으면 살아 있는 코드를 모두 만료시킨다
MAX_PAIR_FAILURES = 10
_ID_RE = re.compile(r"^([a-z]+)_([1-9]\d{0,17})$")  # SQLite INTEGER 범위 안
_PIN_ITERATIONS = 100_000


def _write(db: Session, stmt: Any) -> int:
    """조건부 UPDATE/DELETE. 필요한 곳에서 refresh하므로 ORM 동기화는 끈다 (naive·aware 시각 비교 오류 방지)."""
    return db.execute(stmt, execution_options={"synchronize_session": False}).rowcount


def as_local(dt: datetime) -> datetime:
    """DB에서 읽은 naive 시각(로컬 벽시계)을 오프셋 있는 로컬 시각으로."""
    return dt.astimezone()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    """(쿠키에 넣을 원문, DB에 넣을 해시)."""
    return (token := secrets.token_urlsafe(32)), hash_token(token)


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), _PIN_ITERATIONS).hex()
    return f"pbkdf2_sha256${_PIN_ITERATIONS}${salt}${digest}"


def verify_pin(pin: str, stored: str) -> bool:
    _algo, iterations, salt, digest = stored.split("$")
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iterations)).hex()
    return hmac.compare_digest(candidate, digest)


def parse_id(external_id: str, prefix: str) -> int | None:
    """`ts_2` → 2. 형식이 다르면 None."""
    m = _ID_RE.match(external_id)
    return int(m.group(2)) if m and m.group(1) == prefix else None


def is_locked(tester: Tester) -> bool:
    return tester.locked_until is not None and as_local(tester.locked_until) > local_now()


# ── 등록 코드 · 단말 ──────────────────────────────────────────────


def issue_pair_code(db: Session) -> PairCode:
    now = local_now()
    # 다 쓴·만료된 코드와 만료된 세션은 발급할 때 같이 치운다
    _write(db, delete(PairCode).where(PairCode.used_at.is_not(None) | (PairCode.expires_at <= now)))
    _write(db, delete(LoginSession).where(LoginSession.expires_at <= now))
    active = set(db.scalars(select(PairCode.code)))
    code = f"{secrets.randbelow(1_000_000):06d}"
    while code in active:
        code = f"{secrets.randbelow(1_000_000):06d}"
    row = PairCode(code=code, expires_at=now + PAIR_CODE_TTL)
    db.add(row)
    db.commit()
    return row


def pair_client(db: Session, code: str, device_name: str) -> tuple[Client, str]:
    """1회용 코드를 소비하고 단말을 만든다. 반환: (단말, 쿠키 토큰 원문)."""
    now = local_now()
    row = db.scalars(select(PairCode).where(PairCode.code == code, PairCode.used_at.is_(None))).first()
    # TODO(확인 필요): 없는 코드·이미 쓴 코드의 오류 코드가 계약에 없어 400 VALIDATION으로 응답함
    if row is None:
        # 틀린 코드가 MAX_PAIR_FAILURES번 들어오면 살아 있는 코드를 모두 만료시킨다 (6자리 무차별 대입 막기)
        alive = PairCode.used_at.is_(None) & (PairCode.expires_at > now)
        _write(db, update(PairCode).where(alive).values(failed_attempts=PairCode.failed_attempts + 1))
        _write(db, update(PairCode).where(alive, PairCode.failed_attempts >= MAX_PAIR_FAILURES).values(expires_at=now))
        db.commit()
        raise validation_error({"pair_code": "등록 코드가 맞지 않습니다."})
    if as_local(row.expires_at) <= now:
        raise ApiError(410, "PAIR_CODE_EXPIRED", "등록 코드가 만료되었습니다. 노트북에서 새 코드를 발급하세요.")
    # 조건부 UPDATE로 소비: 같은 코드로 동시에 들어와도 한 요청만 성공한다
    if _write(db, update(PairCode).where(PairCode.id == row.id, PairCode.used_at.is_(None)).values(used_at=now)) != 1:
        db.rollback()
        raise validation_error({"pair_code": "등록 코드가 맞지 않습니다."})
    token, token_hash = new_token()
    client = Client(device_name=device_name, token_hash=token_hash, paired_at=now)
    db.add(client)
    db.commit()
    return client, token


def pair_qr_svg(code: str, port: int) -> str:
    """QR 내용은 `http://<IP>:8765/pair?code=483920` (03-UI시나리오 B-1).

    TODO(확인 필요): 에그 Wi-Fi에서 노트북 IP를 고정할지 — 그 전에는 KTL_PUBLIC_HOST, 없으면 자동 감지.
    """
    host = os.getenv("KTL_PUBLIC_HOST")
    if not host:  # UDP connect는 패킷을 보내지 않고 기본 경로의 로컬 주소만 정한다 (오프라인에서도 동작)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.connect(("10.255.255.255", 1))
                host = sock.getsockname()[0]
            except OSError:
                host = "127.0.0.1"
    return segno.make(f"http://{host}:{port}/pair?code={code}", error="m").svg_inline(scale=4)


def end_sessions(db: Session, *, client_id: int | None = None, tester_id: int | None = None) -> None:
    """단말 해제·시험자 비활성·PIN 재설정 때 로그인을 끊는다 (호출한 쪽이 commit)."""
    col, value = (LoginSession.client_id, client_id) if client_id is not None else (LoginSession.tester_id, tester_id)
    _write(db, delete(LoginSession).where(col == value))


# ── 시험자 로그인 ─────────────────────────────────────────────────


def login(db: Session, client: Client, tester_id: str, pin: str) -> tuple[LoginSession, Tester, str]:
    """PIN 확인 → 세션 생성. 5회 연속 실패하면 10분 잠금(423)."""
    tid = parse_id(tester_id, "ts")
    tester = db.get(Tester, tid) if tid is not None else None
    # TODO(확인 필요): PIN 틀림·없는 시험자의 오류 코드가 계약에 없어 401 SESSION_REQUIRED로 응답함
    if tester is None or not tester.active:
        raise ApiError(401, "SESSION_REQUIRED", "시험자를 찾을 수 없습니다.")

    now = local_now()
    me = Tester.id == tester.id
    _write(db, update(Tester).where(me, Tester.locked_until <= now).values(failed_count=0, locked_until=None))
    # PIN을 확인하기 전에 시도 1회를 조건부 UPDATE로 먼저 차지한다.
    # 동시에 여러 요청이 와도 PIN 확인은 잠금 전 최대 MAX_PIN_FAILURES번만 일어난다.
    claimed = _write(
        db,
        update(Tester)
        .where(me, Tester.locked_until.is_(None), Tester.failed_count < MAX_PIN_FAILURES)
        .values(failed_count=Tester.failed_count + 1),
    )
    db.commit()
    db.refresh(tester)
    if claimed != 1:
        raise _locked_error(tester, now)

    if not verify_pin(pin, tester.pin_hash):
        if tester.failed_count >= MAX_PIN_FAILURES:
            _write(db, update(Tester).where(me, Tester.locked_until.is_(None)).values(locked_until=now + PIN_LOCK))
            db.commit()
            db.refresh(tester)
            raise _locked_error(tester, now)
        remaining = MAX_PIN_FAILURES - tester.failed_count
        raise ApiError(401, "SESSION_REQUIRED", f"PIN이 맞지 않습니다 (남은 횟수 {remaining})", {"remaining": remaining})

    _write(db, update(Tester).where(me).values(failed_count=0))
    token, token_hash = new_token()
    sess = LoginSession(token_hash=token_hash, client_id=client.id, tester_id=tester.id, expires_at=now + SESSION_TTL)
    db.add(sess)
    db.commit()
    return sess, tester, token


def _locked_error(tester: Tester, now: datetime) -> ApiError:
    # locked_until이 아직 없으면 다른 요청이 5회째 실패를 기록하는 중 → 잠금 전체 시간
    until = as_local(tester.locked_until) if tester.locked_until is not None else now + PIN_LOCK
    return ApiError(
        423,
        "PIN_LOCKED",
        f"PIN {MAX_PIN_FAILURES}회 실패로 10분간 잠겼습니다. 노트북 담당에게 잠금 해제를 요청하세요.",
        {"retry_after": max(1, int((until - now).total_seconds()))},
    )
