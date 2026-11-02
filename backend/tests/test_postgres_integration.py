"""Integration checks against a REAL database server. Skipped unless COMPASS_TEST_DATABASE_URL is set.

    COMPASS_TEST_DATABASE_URL=postgresql+psycopg://compass:compass@localhost:5432/compass_test pytest tests/test_postgres_integration.py -v

Use a scratch database: the test applies every migration to it (and leaves the schema in place). These tests exist because
SQLite cannot show how the migrations or concurrent sessions behave on PostgreSQL (JSONB, constraint names, row-level
atomicity of the conditional UPDATEs that make pairing codes and refresh tokens single-use).

STATUS: written in Stage 13 and NOT RUN against PostgreSQL (none is available on the development machine). It was only
exercised against a file-backed SQLite database to check that the test logic itself is sound. Do not report PostgreSQL as
validated until it has been run there; record the result in docs/VALIDATION_STATUS.md.
"""

import os
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

import app.db.models  # noqa: F401
from app.core.config import Settings
from app.core.errors import AppError
from app.db.base import Base
from app.db.models import AuthSession, User
from app.services import sessions as svc

URL = os.environ.get("COMPASS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set COMPASS_TEST_DATABASE_URL to run integration checks against a real database")

BACKEND = Path(__file__).resolve().parents[1]
SETTINGS = Settings(auth_secret="s" * 48, _env_file=None)


def _head() -> str:
    """The newest migration revision, read from the migration scripts (never a hard-coded number that can go stale)."""
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


HEAD = _head()
THREADS = 8


@pytest.fixture(scope="module")
def engine():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", URL)
    command.upgrade(cfg, "head")
    eng = create_engine(URL, pool_size=THREADS + 2, max_overflow=0) if not URL.startswith("sqlite") else create_engine(
        URL, connect_args={"timeout": 30})
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def user_id(factory):
    with factory() as db:
        count = db.scalar(select(func.count()).select_from(User)) or 0
        u = User(github_user_id=900_000 + count, username=f"it-{count}-{os.getpid()}")
        db.add(u)
        db.commit()
        return u.id


def test_schema_is_at_head_and_matches_the_models(engine):
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar_one() == HEAD
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == [], "migrations drifted from the models"
    assert {"auth_sessions", "pairing_codes"} <= set(inspect(engine).get_table_names())


@pytest.mark.skipif(bool(URL) and URL.startswith("sqlite"), reason="PostgreSQL-specific")
def test_json_columns_are_jsonb(engine):
    with engine.connect() as conn:
        types = conn.execute(text(
            "select table_name, column_name, data_type from information_schema.columns "
            "where table_schema = 'public' and data_type in ('json', 'jsonb')")).all()
    assert types, "expected JSON columns"
    assert {t[2] for t in types} == {"jsonb"}, [t for t in types if t[2] != "jsonb"]


def _race(n, fn):
    """Run fn(i) in n threads released together; return (results, errors)."""
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        barrier.wait()
        try:
            results.append(fn(i))
        except AppError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    return results, errors


def test_a_pairing_code_can_be_exchanged_exactly_once_under_concurrency(factory, user_id):
    with factory() as db:
        code = svc.create_pairing_code(db, user_id, SETTINGS)

    def attempt(_):
        with factory() as db:
            return svc.exchange_pairing_code(db, SETTINGS, code, "race")

    won, lost = _race(THREADS, attempt)
    assert len(won) == 1, f"{len(won)} concurrent exchanges succeeded for one single-use code"
    assert len(lost) == THREADS - 1 and all(e.status_code == 401 for e in lost)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user_id, AuthSession.client_label == "race")) == 1


def test_a_refresh_token_can_be_spent_exactly_once_under_concurrency(factory, user_id):
    with factory() as db:
        pair = svc.exchange_pairing_code(db, SETTINGS, svc.create_pairing_code(db, user_id, SETTINGS), "spend")

    def attempt(_):
        with factory() as db:
            return svc.refresh_session(db, SETTINGS, pair.refresh_token)

    won, lost = _race(THREADS, attempt)
    assert len(won) == 1, f"{len(won)} concurrent refreshes succeeded for one refresh token (double spend)"
    assert all(e.status_code == 401 for e in lost)
    # Replaying the spent token afterwards ends the session, which also kills the newest access token's session.
    with factory() as db:
        with pytest.raises(AppError):
            svc.refresh_session(db, SETTINGS, pair.refresh_token)
        row = db.scalar(select(AuthSession).where(AuthSession.client_label == "spend", AuthSession.user_id == user_id))
        assert row.revoked_at is not None


def test_concurrent_identical_match_requests_store_exactly_one_row(factory):
    """The unique (user, issue, model, input-hash) constraint must make racing identical requests idempotent: the same
    match_id comes back to everyone, nobody gets a 500, and exactly one row exists. (SQLite only simulates this race.)"""
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from app.api.deps import get_github_client
    from app.core.config import get_settings
    from app.db.models import Recommendation
    from app.db.session import get_db
    from app.main import create_app
    from app.schemas.profile import ProfileIn
    from app.services.profile import create_profile
    from tests.github_mock import FakeGitHub
    from tests.seed import PROFILE, SETTINGS, make_user, seed_issue, seed_repo

    tag = uuid.uuid4().hex[:8]
    with factory() as db:
        headers, user = make_user(db, 1_000_000 + int(tag, 16) % 900_000_000, f"racer-{tag}")
        create_profile(db, user.id, ProfileIn.model_validate(PROFILE))
        repo = seed_repo(db, f"race-{tag}", "api", "Python", topics=("fastapi",))
        seed_issue(db, repo, 1, "FastAPI loader fails", "Machine learning pipeline crashes. " + "x" * 100, labels=("good first issue",))
        user_id = user.id
    path = f"/api/v1/issues/race-{tag}/api/1/match"
    barrier = threading.Barrier(THREADS)

    def attempt(_):
        app = create_app()

        def db_for_request():
            with factory() as session:
                yield session

        app.dependency_overrides[get_db] = db_for_request
        app.dependency_overrides[get_settings] = lambda: SETTINGS
        app.dependency_overrides[get_github_client] = lambda: FakeGitHub({}).client
        client = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        r = client.post(path, json={"persist": True}, headers=headers)
        return r.status_code, (r.json()["match"]["match_id"] if r.status_code == 200 else r.text[:200])

    with ThreadPoolExecutor(THREADS) as pool:
        results = list(pool.map(attempt, range(THREADS)))
    assert all(code == 200 for code, _ in results), results
    assert len({mid for _, mid in results}) == 1, results
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Recommendation).where(Recommendation.user_id == user_id)) == 1
