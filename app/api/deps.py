"""라우터 공용 의존성 — 앱(휴대폰·관리)마다 `app.state`에 둔 설정을 꺼낸다."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import Request

from app.models import ServerSettings
from app.modules import find_category


def get_settings(request: Request) -> ServerSettings:
    return request.app.state.settings


def is_phone_app(request: Request) -> bool:
    """휴대폰 앱이면 True — 휴대폰에 보이는 분류(phone: true)만 다룬다."""
    return bool(getattr(request.app.state, "phone_only", False))


def get_actor(request: Request) -> str:
    """요청한 사람 (이력·created_by용).

    TODO(REQ-B1-10): 휴대폰은 ktl_session 쿠키의 시험자 ID(`ts_N`)로 바꾼다. 로그인 구현 전까지는 앱 기본값.
    """
    return request.app.state.default_actor


def get_category_lookup(request: Request) -> Callable[[str], Mapping[str, Any] | None]:
    path = get_settings(request).categories_path

    def lookup(name: str) -> Mapping[str, Any] | None:
        return find_category(name, path)

    return lookup
