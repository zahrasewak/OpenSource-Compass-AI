"""Migration checks that run without PostgreSQL.

These do NOT validate the migration against a real PostgreSQL server; that is done
by the CI job `migrations-postgres` (see .github/workflows/ci.yml) once it has run.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

import app.db.models  # noqa: F401
from app.db.base import Base

BACKEND = Path(__file__).resolve().parents[1]


def _cfg(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def sqlite_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'm.db').as_posix()}"


def test_upgrade_matches_models_and_downgrade_is_clean(sqlite_url):
    cfg = _cfg(sqlite_url)
    command.upgrade(cfg, "head")
    engine = create_engine(sqlite_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"migration drifted from models: {diff}"
    assert set(inspect(engine).get_table_names()) >= set(Base.metadata.tables)
    command.downgrade(cfg, "base")
    assert inspect(create_engine(sqlite_url)).get_table_names() == ["alembic_version"]


def test_postgres_offline_sql_uses_postgres_types():
    buf = io.StringIO()
    cfg = _cfg("postgresql+psycopg://u:p@localhost/db")
    with redirect_stdout(buf):
        command.upgrade(cfg, "head", sql=True)
    sql = buf.getvalue()
    assert "JSONB" in sql
    assert "CREATE TABLE users" in sql and "CREATE TABLE execution_results" in sql
    assert "CURRENT_TIMESTAMP)" not in sql.replace("DEFAULT now()", "")  # no SQLite-style defaults
    assert "REFERENCES users (id) ON DELETE CASCADE" in sql


def test_running_migrations_in_process_does_not_silence_application_loggers(sqlite_url):
    """alembic's fileConfig() disables existing loggers by default, which would mute compass.* warnings (found in Stage 13)."""
    import logging

    logger = logging.getLogger("compass.auth")
    command.upgrade(_cfg(sqlite_url), "head")
    assert logger.disabled is False
