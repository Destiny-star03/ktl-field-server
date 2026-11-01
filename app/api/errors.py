"""공통 에러 형식 (02-API명세.md §1.1).

모든 오류 응답은 `{ "error": { "code", "message", "detail" } }` 한 가지 모양으로 낸다.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_DEFAULT_CODES = {400: "VALIDATION", 404: "NOT_FOUND", 500: "INTERNAL"}
_DEFAULT_MESSAGES = {
    400: "요청 형식이 올바르지 않습니다.",
    404: "찾을 수 없습니다.",
    500: "서버 내부 오류가 발생했습니다.",
}


class ApiError(Exception):
    """계약된 에러 코드로 응답할 때 쓰는 예외."""

    def __init__(self, status_code: int, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


def error_response(status_code: int, code: str, message: str, detail: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content={"error": body})


def validation_error(fields: dict[str, str], message: str = _DEFAULT_MESSAGES[400]) -> ApiError:
    """400 VALIDATION — `detail.fields`에 칸별 이유."""
    return ApiError(400, "VALIDATION", message, {"fields": fields})


def not_found(message: str = _DEFAULT_MESSAGES[404]) -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


async def _api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status_code, exc.code, exc.message, exc.detail)


async def _validation_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields: dict[str, str] = {}
    for err in exc.errors():
        # loc 예: ("body", "receipt_no") · ("query", "limit") — 위치 접두어는 빼고 칸 이름만
        loc = [str(part) for part in err.get("loc", ()) if part not in ("body", "query", "path")]
        if err.get("type") == "json_invalid":  # loc에 칸 이름 대신 문자 위치가 들어 있음
            loc = []
        fields[".".join(loc) or "body"] = err.get("msg", "형식 오류")
    return error_response(400, "VALIDATION", _DEFAULT_MESSAGES[400], {"fields": fields})


async def _http_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = exc.status_code
    code = _DEFAULT_CODES.get(status, "VALIDATION" if 400 <= status < 500 else "INTERNAL")
    return error_response(status, code, _DEFAULT_MESSAGES.get(status, str(exc.detail)))


async def _unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("처리되지 않은 예외", exc_info=exc)
    return error_response(500, "INTERNAL", _DEFAULT_MESSAGES[500])


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
