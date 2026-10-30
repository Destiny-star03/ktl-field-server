"""판독 작업 프로세스 본체. 서버와 Pipe로만 주고받고, 모듈은 이 프로세스 안에서만 불러온다.

모듈이 예외를 내면 그 사진만 실패로 돌려주고, 무한 대기하면 서버 쪽 감독 스레드가 이 프로세스를 강제 종료한다.
"""
from __future__ import annotations

from multiprocessing.connection import Connection
from numbers import Integral, Real
import time
from typing import Any

READY = "ready"


class _BadResult(Exception):
    """모듈 결과가 계약(02 §3.1) 모양이 아님."""


def _plain_value(v: Any) -> dict[str, Any]:
    """Value → 기본 타입 dict. numpy 숫자(np.float32 신뢰도 · np.int64 bbox)도 float/int로 바꾼다
    (서버 프로세스가 numpy 없이 받을 수 있게)."""
    key, value, unit = getattr(v, "key", None), getattr(v, "value", None), getattr(v, "unit", None)
    if not isinstance(key, str) or not isinstance(value, str):
        raise _BadResult("key·value는 문자열이어야 합니다")
    confidence = getattr(v, "confidence", 1.0)
    if not isinstance(confidence, Real) or not 0.0 <= float(confidence) <= 1.0:
        raise _BadResult(f"{key} 신뢰도는 0~1이어야 합니다")
    bbox = getattr(v, "bbox", None)
    if bbox is not None:
        if len(bbox) != 4 or not all(isinstance(n, Integral) for n in bbox):
            raise _BadResult(f"{key} bbox는 정수 4개여야 합니다")
        bbox = [int(n) for n in bbox]
    return {"key": key, "label": str(getattr(v, "label", "") or key), "value": value,
            "unit": None if unit is None else str(unit), "confidence": float(confidence), "bbox": bbox}


def _plain_result(result: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    values, warnings = getattr(result, "values", None), getattr(result, "warnings", [])
    if not isinstance(values, list) or not isinstance(warnings, list):
        raise _BadResult("values·warnings는 목록이어야 합니다")
    return (
        [_plain_value(v) for v in values],
        [{"code": str(w.code), "message": str(w.message)} for w in warnings],
    )


def read_one(readers: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """작업 하나를 판독해 서버로 보낼 dict를 만든다. 모듈 예외는 여기서 모두 잡는다."""
    from ktl_contract import ReadError

    from app.modules import get_reader

    start = time.monotonic()
    try:
        name = job["reader"]
        if name not in readers:
            readers[name] = get_reader(name)
        values, warnings = _plain_result(readers[name].read(job["image_path"], job["item_key"]))
        return {"ok": True, "values": values, "warnings": warnings, "read_ms": _elapsed_ms(start)}
    except _BadResult as exc:
        return {"ok": False, "error": f"판독 모듈 결과 형식 오류: {exc}", "read_ms": _elapsed_ms(start)}
    except ReadError as exc:
        return {"ok": False, "error": str(exc) or "사진을 읽지 못했습니다", "read_ms": _elapsed_ms(start)}
    except Exception as exc:  # noqa: BLE001 - 모듈 버그가 서버로 번지지 않게 전부 잡는다
        return {"ok": False, "error": f"판독 모듈 오류 ({type(exc).__name__}): {exc}", "read_ms": _elapsed_ms(start)}


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def run(conn: Connection, preload: list[str]) -> None:
    """작업 프로세스 진입점. None을 받으면 끝낸다.

    preload의 Reader를 미리 만들어 둔 뒤 READY를 보낸다 (모델 로딩 시간이 1장 10초 제한에 들어가지 않게).
    """
    from app.modules import get_reader

    readers: dict[str, Any] = {}  # Reader는 프로세스 수명 동안 재사용 (OCR 모델 로딩 1회)
    for name in preload:
        try:
            readers[name] = get_reader(name)
        except Exception:  # noqa: BLE001 - 만들지 못한 Reader는 작업 때 다시 시도해 그 사진만 실패
            pass
    conn.send(READY)
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            return
        if job is None:
            return
        conn.send(read_one(readers, job))
