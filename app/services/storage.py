"""원본 보관소 (REQ-B1-03 · REQ-COM-03): 받은 그대로 저장 → SHA-256 기록 → 읽기 전용.

원본은 서버가 다시 쓰지 않는다. 회전 보정·썸네일 등 사본은 `derived/`에 따로 만든다 (REQ-B1-12).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import BinaryIO

MAX_PHOTO_BYTES = 20 * 1024 * 1024  # 02 §1.1: 413 FILE_TOO_LARGE — 사진 20 MB 초과
TOO_LARGE_MESSAGE = "사진이 20 MB를 넘습니다."
_CHUNK = 1024 * 1024
_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


class FileTooLarge(Exception):
    pass


class NotAnImage(Exception):
    pass


@dataclass(frozen=True)
class StoredOriginal:
    stored_name: str
    rel_path: str  # storage_dir 기준, `/` 구분
    sha256: str
    size_bytes: int


def detect_image_ext(head: bytes) -> str | None:
    """파일 앞부분(매직 바이트)으로 확장자를 정한다. 파일명은 믿지 않는다 (REQ-COM-06)."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
        return ".heic"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    # TODO(확인 필요): 현장 휴대폰이 보내는 형식이 JPEG 말고 더 있는지 (HEIC를 Reader가 읽을 수 있는지 포함)
    return None


def _make_read_only(path: Path) -> None:
    # Windows에서는 읽기 전용 속성, POSIX에서는 쓰기 권한 제거
    os.chmod(path, _READ_ONLY)


def remove_original(storage_dir: Path, rel_path: str) -> None:
    """저장 직후 DB 기록에 실패한 원본만 지운다 (다른 경우에는 원본을 지우지 않는다)."""
    path = storage_dir / rel_path
    try:
        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
        path.unlink()
    except FileNotFoundError:
        pass


def store_original(src: BinaryIO, storage_dir: Path, folder: str, base_name: str) -> StoredOriginal:
    """`src`를 끝까지 읽어 `originals/<folder>/<base_name><ext>`에 저장하고 읽기 전용으로 만든다.

    - 20 MB를 넘으면 FileTooLarge, 이미지가 아니면 NotAnImage (둘 다 파일을 남기지 않는다)
    - 같은 이름이 있으면 `_2`, `_3` …을 붙인다 (`xb`로 열어 동시 업로드에도 기존 원본을 덮어쓰지 않는다)
    """
    first = src.read(_CHUNK)
    ext = detect_image_ext(first[:16])
    if ext is None:
        raise NotAnImage
    target_dir = storage_dir / "originals" / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    out, final = _open_new(target_dir, base_name, ext)
    digest = hashlib.sha256()
    size = 0
    try:
        with out:
            chunk = first
            while chunk:
                size += len(chunk)
                if size > MAX_PHOTO_BYTES:
                    raise FileTooLarge
                digest.update(chunk)
                out.write(chunk)
                chunk = src.read(_CHUNK)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        final.unlink(missing_ok=True)
        raise
    _make_read_only(final)
    return StoredOriginal(
        stored_name=final.name,
        rel_path=final.relative_to(storage_dir).as_posix(),
        sha256=digest.hexdigest(),
        size_bytes=size,
    )


def _open_new(target_dir: Path, base_name: str, ext: str) -> tuple[BinaryIO, Path]:
    for n in range(1, 1000):
        path = target_dir / (f"{base_name}{ext}" if n == 1 else f"{base_name}_{n}{ext}")
        try:
            return open(path, "xb"), path
        except FileExistsError:
            continue
    raise RuntimeError("저장 파일 이름을 정하지 못했습니다")
