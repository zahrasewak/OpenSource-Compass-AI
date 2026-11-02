from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.cli import main as cli_main
from app.core.config import Settings, get_settings
from app.core.security import AuthConfigError, InvalidToken, issue_token, verify_token
from app.db.models import User

SECRET = "x" * 48
OTHER = "y" * 48


def settings(secret=SECRET, **kw) -> Settings:
    return Settings(auth_secret=secret, _env_file=None, **kw)


@pytest.fixture
def auth_client(client):
    client.app.dependency_overrides[get_settings] = lambda: settings()
    return client


@pytest.fixture
def user(db_session):
    u = User(github_user_id=42, username="octocat")
    db_session.add(u)
    db_session.commit()
    return u


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_valid_token(auth_client, user):
    r = auth_client.get("/api/v1/auth/me", headers=bearer(issue_token(user.id, settings())))
    assert r.status_code == 200
    assert r.json() == {"id": user.id, "github_user_id": 42, "username": "octocat"}


def test_missing_token(auth_client):
    r = auth_client.get("/api/v1/auth/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("bad", ["garbage", "a.b.c", ""])
def test_malformed_token(auth_client, bad):
    assert auth_client.get("/api/v1/auth/me", headers=bearer(bad)).status_code == 401


def test_expired_token(auth_client, user):
    t = issue_token(user.id, settings(), ttl=timedelta(seconds=-5))
    assert auth_client.get("/api/v1/auth/me", headers=bearer(t)).status_code == 401


def test_wrong_signature(auth_client, user):
    t = issue_token(user.id, settings(OTHER))
    assert auth_client.get("/api/v1/auth/me", headers=bearer(t)).status_code == 401


def test_wrong_audience_and_issuer(auth_client, user):
    for kw in ({"auth_audience": "other"}, {"auth_issuer": "other"}):
        t = issue_token(user.id, settings(**kw))
        assert auth_client.get("/api/v1/auth/me", headers=bearer(t)).status_code == 401


def test_alg_none_rejected(auth_client, user):
    now = datetime.now(timezone.utc)
    t = jwt.encode({"sub": str(user.id), "iss": "opensource-compass", "aud": "compass-api",
                    "iat": now, "exp": now + timedelta(hours=1)}, key=None, algorithm="none")
    assert auth_client.get("/api/v1/auth/me", headers=bearer(t)).status_code == 401


def test_token_missing_required_claim(auth_client, user):
    t = jwt.encode({"sub": str(user.id)}, SECRET, algorithm="HS256")
    assert auth_client.get("/api/v1/auth/me", headers=bearer(t)).status_code == 401


def test_deleted_user(auth_client, user, db_session):
    t = issue_token(user.id, settings())
    db_session.delete(user)
    db_session.commit()
    assert auth_client.get("/api/v1/auth/me", headers=bearer(t)).status_code == 401


def test_client_cannot_choose_user(auth_client, user):
    h = {**bearer(issue_token(user.id, settings())), "X-User-Id": "999"}
    assert auth_client.get("/api/v1/auth/me?user_id=999", headers=h).json()["id"] == user.id


def test_unconfigured_secret_fails_closed(client, user):
    client.app.dependency_overrides[get_settings] = lambda: settings(secret=None)
    r = client.get("/api/v1/auth/me", headers=bearer("anything"))
    assert r.status_code == 503 and r.json()["error"]["code"] == "auth_unavailable"


def test_short_secret_rejected():
    with pytest.raises(AuthConfigError):
        issue_token(1, settings("short"))
    with pytest.raises(AuthConfigError):
        verify_token("x", settings("short"))


def test_verify_roundtrip_and_error_type():
    assert verify_token(issue_token(7, settings()), settings()) == 7
    with pytest.raises(InvalidToken):
        verify_token("nope", settings())


def test_cli_issue_token(tmp_path, monkeypatch, capsys):
    from sqlalchemy import create_engine
    import app.db.models  # noqa: F401
    from app.db.base import Base
    from app.db import session as session_mod

    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    Base.metadata.create_all(create_engine(url))
    monkeypatch.setenv("COMPASS_DATABASE_URL", url)
    monkeypatch.setenv("COMPASS_AUTH_SECRET", SECRET)
    for f in (get_settings, session_mod.get_engine, session_mod.get_sessionmaker):
        f.cache_clear()
    try:
        assert cli_main(["issue-token", "--github-id", "5", "--username", "bob"]) == 0
        token = capsys.readouterr().out.strip()
        assert verify_token(token, settings()) == 1
        monkeypatch.delenv("COMPASS_AUTH_SECRET")
        get_settings.cache_clear()
        assert cli_main(["issue-token", "--github-id", "5", "--username", "bob"]) == 2
    finally:
        for f in (get_settings, session_mod.get_engine, session_mod.get_sessionmaker):
            f.cache_clear()
