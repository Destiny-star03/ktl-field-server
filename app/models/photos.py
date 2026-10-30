"""사진 테이블 (REQ-B1-03, 02-API명세.md §2.3). 외부 ID `ph_<id>`."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, local_now

_TS = DateTime(timezone=True)


class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (
        # 칸(기기 × 항목)마다 최신 사진을 고를 때 쓴다
        Index("ix_photos_slot", "case_id", "unit_index", "item_key"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    unit_index: Mapped[int] = mapped_column(Integer)
    item_key: Mapped[str] = mapped_column(String(32))
    reader: Mapped[str] = mapped_column(String(32))  # categories.yaml items[].reader ("none"이면 판독 없음)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # 휴대폰이 만든 UUID — 같은 값 재전송은 저장하지 않는다 (재시도 안전)
    client_upload_id: Mapped[str] = mapped_column(String(36), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    stored_name: Mapped[str] = mapped_column(String(128))
    original_path: Mapped[str] = mapped_column(String(256))  # storage_dir 기준 상대 경로 (읽기 전용 원본)
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(_TS, default=local_now)
    # 판독 결과 (REQ-B1-04에서 채운다). API `Value` · `Warning` 모양 dict 목록
    values: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String(512))
    read_ms: Mapped[int | None] = mapped_column(Integer)
    deleted_at: Mapped[datetime | None] = mapped_column(_TS)
    deleted_reason: Mapped[str | None] = mapped_column(String(200))
