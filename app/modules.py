"""categories.yaml 기준 Reader/Selector 모듈 로더."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.models import load_settings

logger = logging.getLogger(__name__)

try:
    from ktl_contract import Reader, Selector
    from ktl_contract.fakes import FakeReader, FakeSelector
except ModuleNotFoundError:  # pragma: no cover - ktl-contract 미설치 클린 환경 폴백
    Reader = Any  # type: ignore[misc,assignment]
    Selector = Any  # type: ignore[misc,assignment]

    class FakeReader:  # type: ignore[no-redef]
        """ktl-contract 미설치 환경을 위한 최소 가짜 Reader 폴백."""

        def __init__(self, name: str) -> None:
            self.name = name

    class FakeSelector:  # type: ignore[no-redef]
        """ktl-contract 미설치 환경을 위한 최소 가짜 Selector 폴백."""

        def __init__(self, name: str) -> None:
            self.name = name


_EMPTY_CATEGORIES: dict[str, Any] = {"version": 1, "categories": []}
_cache: dict[Path, tuple[float, dict[str, Any]]] = {}


def load_categories(yaml_path: Path | None = None) -> dict[str, Any]:
    """categories.yaml 파일을 읽어 파싱된 딕셔너리를 반환한다.

    파일 수정 시각이 바뀌면 다시 읽는다 (칸 매핑은 이 파일만 고쳐서 바꾼다 — 02 §3.4).
    """
    path = (yaml_path or load_settings().categories_path).resolve()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        logger.warning("categories.yaml을 찾을 수 없습니다: %s (KTL_CATEGORIES_PATH로 지정)", path)
        return _EMPTY_CATEGORIES
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or not isinstance(data.get("categories"), list):
        raise ValueError(f"categories.yaml 형식 오류: {path}")
    _cache[path] = (mtime, data)
    return data


def find_category(name: str, yaml_path: Path | None = None) -> dict[str, Any] | None:
    """분류 key(`sampling` 등)에 해당하는 categories.yaml 항목을 찾는다."""
    for category in load_categories(yaml_path)["categories"]:
        if category.get("category") == name:
            return category
    return None


def get_reader(name: str) -> Reader:
    """이름에 해당하는 Reader 모듈(기본: FakeReader)을 반환한다."""
    return FakeReader(name=name)


def get_selector(name: str) -> Selector:
    """이름에 해당하는 Selector 모듈(기본: FakeSelector)을 반환한다."""
    return FakeSelector(name=name)
