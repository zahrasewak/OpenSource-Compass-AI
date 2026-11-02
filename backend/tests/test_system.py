from datetime import datetime, timezone

import pytest
from fastapi import APIRouter
from sqlalchemy.exc import IntegrityError

from app.db.models import ExecutionJob, Repository, User, UserSkill
from app.db.session import get_db
from app.main import create_app
from fastapi.testclient import TestClient


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert "X-Request-ID" in r.headers


def test_ready_with_database(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["database"] == "up"


def test_ready_reports_unavailable_when_db_down():
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

    app = create_app()
    app.dependency_overrides[get_db] = lambda: Broken()
    r = TestClient(app).get("/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["database"] == "down"


def test_unknown_route_uses_error_envelope(client):
    r = client.get("/nope")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "http_404" and err["request_id"]


def test_unhandled_error_does_not_leak_details():
    app = create_app()
    router = APIRouter()

    @router.get("/explode")
    def explode():
        raise RuntimeError("secret-db-password")

    app.include_router(router)
    r = TestClient(app, raise_server_exceptions=False).get("/explode")
    assert r.status_code == 500
    assert "secret" not in r.text
    assert r.json()["error"]["code"] == "internal_error"


def test_user_skill_unique_and_range(db_session):
    user = User(github_user_id=1, username="a")
    db_session.add(user)
    db_session.commit()
    db_session.add(UserSkill(user_id=user.id, skill_name="python", proficiency=0.5))
    db_session.commit()
    db_session.add(UserSkill(user_id=user.id, skill_name="python", proficiency=0.6))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.add(UserSkill(user_id=user.id, skill_name="go", proficiency=1.5))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_execution_job_status_constraint(db_session):
    user = User(github_user_id=2, username="b")
    repo = Repository(owner="o", name="r")
    db_session.add_all([user, repo])
    db_session.commit()
    db_session.add(
        ExecutionJob(user_id=user.id, repository_id=repo.id, commit_sha="a" * 40,
                     execution_mode="test_runner", status="BOGUS")
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
