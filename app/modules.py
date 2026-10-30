"""categories.yaml 기준 Reader/Selector 모듈 로더 (REQ-B1-05)."""
from __future__ import annotations

from collections.abc import Mapping
import importlib
import logging
import os
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

# KTL_MODULES: "auto"(기본) = ktl_readers/ktl_selectors가 설치돼 있으면 그쪽 get_reader/get_selector,
#              없으면 ktl_contract.fakes. "fake" = 항상 가짜 모듈 (관문 2 · 테스트용).
MODULES_MODE_ENV = "KTL_MODULES"
NO_READER = "none"  # categories.yaml에서 판독하지 않는 촬영 항목 (예: 형식승인표)


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


def reader_name_for_item(category: Mapping[str, Any], item_key: str) -> str | None:
    """분류의 촬영 항목 key → Reader 이름. `reader: none`이거나 없는 항목이면 None (판독 안 함)."""
    for item in category.get("items") or []:
        if item.get("key") == item_key:
            name = item.get("reader")
            return None if not name or name == NO_READER else str(name)
    return None


def reader_names(categories: Mapping[str, Any]) -> list[str]:
    """categories.yaml에 나오는 Reader 이름 전체 (판독 작업 프로세스가 미리 만들어 둘 목록)."""
    names: list[str] = []
    for category in categories.get("categories") or []:
        for item in category.get("items") or []:
            name = item.get("reader")
            if name and name != NO_READER and name not in names:
                names.append(str(name))
    return names


def expected_fields(category: Mapping[str, Any], item_key: str) -> list[str]:
    """`export`의 `항목.칸`에서 그 촬영 항목 사진이 채워야 할 칸 key 목록을 뽑는다 (02 §3.4).

    모듈이 일부 칸만 돌려주면 빠진 칸은 서버가 needs_check로 채운다 (02 §3.2).
    """
    prefix = f"{item_key}."
    return [col[len(prefix):] for col in category.get("export") or [] if str(col).startswith(prefix)]


def _modules_mode() -> str:
    return os.getenv(MODULES_MODE_ENV, "auto").strip().lower()


def _import_optional(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:  # 패키지는 있는데 그 안의 의존성이 없음 → 숨기지 않는다
            raise
        return None


def get_reader(name: str) -> Reader:
    """이름에 해당하는 Reader. ktl_readers가 있으면 그 get_reader(아직 없는 Reader는 가짜로 대체), 없으면 FakeReader.

    무거운 OCR 의존성이 서버 프로세스에 올라오지 않도록 판독 작업 프로세스 안에서만 부른다.
    """
    if _modules_mode() != "fake":
        readers = _import_optional("ktl_readers")
        if readers is not None:
            return readers.get_reader(name)
    return FakeReader(name=name)


def get_selector(name: str) -> Selector:
    """이름에 해당하는 Selector. ktl_selectors가 있으면 그 get_selector, 없으면 FakeSelector."""
    if _modules_mode() != "fake":
        selectors = _import_optional("ktl_selectors")
        if selectors is not None:
            return selectors.get_selector(name)
    return FakeSelector(name=name)
