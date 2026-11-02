import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401  (register tables)
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app


SUITE_URL = os.environ.get("COMPASS_TEST_SUITE_DATABASE_URL")


@pytest.fixture
def db_session():
    """In-memory SQLite session by default.

    Set COMPASS_TEST_SUITE_DATABASE_URL=postgresql+psycopg://... to run the SAME tests against a real PostgreSQL database.
    DESTRUCTIVE: every table is dropped and recreated around each test, so point it at a scratch database only.
    """
    if SUITE_URL:
        engine = create_engine(SUITE_URL)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
        session.rollback()
    if SUITE_URL:
        Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app, raise_server_exceptions=False)
