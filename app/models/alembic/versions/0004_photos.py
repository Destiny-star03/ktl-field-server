"""photos table

Revision ID: 0004_photos
Revises: 0003_auth
Create Date: 2026-10-31 10:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004_photos"
down_revision: str | None = "0003_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("unit_index", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.String(32), nullable=False),
        sa.Column("reader", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("client_upload_id", sa.String(36), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("stored_name", sa.String(128), nullable=False),
        sa.Column("original_path", sa.String(256), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(64), nullable=False),
        _ts("uploaded_at"),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(512), nullable=True),
        sa.Column("read_ms", sa.Integer(), nullable=True),
        _ts("deleted_at", nullable=True),
        sa.Column("deleted_reason", sa.String(200), nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_photos_case_id", "photos", ["case_id"])
    op.create_index("ix_photos_status", "photos", ["status"])
    op.create_index("ix_photos_slot", "photos", ["case_id", "unit_index", "item_key"])


def downgrade() -> None:
    for index in ("ix_photos_slot", "ix_photos_status", "ix_photos_case_id"):
        op.drop_index(index, table_name="photos")
    op.drop_table("photos")
