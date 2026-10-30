"""판독 결과 → API `Value`(02 §2.3) 변환. 자동 판독 값의 상태(auto · needs_check)를 정한다."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# 02 §1.2: 확인 필요 = 신뢰도 < 0.8 또는 형식 오류 (모듈은 형식 오류면 신뢰도를 0.5 이하로 낸다 — 02 §3.1)
NEEDS_CHECK_BELOW = 0.8


def to_api_values(raw_values: Iterable[dict[str, Any]], expected_keys: Iterable[str] = ()) -> list[dict[str, Any]]:
    """모듈이 돌려준 값에 status를 붙이고, 모듈이 못 읽은 칸(expected_keys 중 빠진 것)은 needs_check 빈 값으로 채운다."""
    values: list[dict[str, Any]] = []
    for raw in raw_values:
        confidence = float(raw.get("confidence", 1.0))
        values.append(
            {
                "key": raw["key"],
                "label": raw.get("label") or raw["key"],
                "value": raw["value"],
                "unit": raw.get("unit"),
                "confidence": confidence,
                "bbox": raw.get("bbox"),
                "status": "needs_check" if confidence < NEEDS_CHECK_BELOW else "auto",
            }
        )
    seen = {v["key"] for v in values}
    for key in expected_keys:
        if key not in seen:
            # TODO(확인 필요): 빠진 칸의 화면 표시 이름(label)은 categories.yaml에 없어 key를 그대로 쓴다.
            values.append(
                {"key": key, "label": key, "value": "", "unit": None, "confidence": 0.0, "bbox": None,
                 "status": "needs_check"}
            )
            seen.add(key)
    return values
