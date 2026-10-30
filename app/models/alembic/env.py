"""Alembic 마이그레이션 실행 환경 설정."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.models import Base, create_db_engine, load_settings
import app.models.auth  # noqa: F401  (테이블을 metadata에 등록)
import app.models.photos  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """오프라인 모드로 마이그레이션을 실행한다."""
    url = load_settings().database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """온라인 모드로 마이그레이션을 실행한다."""
    connectable = create_db_engine(load_settings().database_url)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
