"""clients · testers · pair codes · login sessions

Revision ID: 0003_auth
Revises: 0002_cases
Create Date: 2026-10-30 10:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_auth"
down_revision: str | None = "0002_cases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "testers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("pin_hash", sa.String(128), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        _ts("locked_until", nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        _ts("created_at"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("device_name", sa.String(40), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        _ts("paired_at"),
        _ts("last_seen_at", nullable=True),
        _ts("revoked_at", nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "pair_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(6), nullable=False, index=True),
        _ts("expires_at"),
        _ts("used_at", nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
    )
    op.create_table(
        "login_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("tester_id", sa.Integer(), sa.ForeignKey("testers.id"), nullable=False),
        _ts("expires_at"),
    )


def downgrade() -> None:
    for table in ("login_sessions", "pair_codes", "clients", "testers"):
        op.drop_table(table)
