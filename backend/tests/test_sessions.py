"""Session authentication: pairing codes, short-lived access tokens, rotating refresh tokens, revocation, logout."""

import logging
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select, text

from app.cli import main as cli_main
from app.core.config import Settings, get_settings
from app.core.ratelimit import AuthRateLimiter
from app.core.security import issue_access_token, issue_token
from app.db.models import AuthSession, PairingCode, User
from app.services import sessions as svc

SECRET = "s" * 48
ME = "/api/v1/auth/me"


def settings(**kw) -> Settings:
    return Settings(auth_secret=SECRET, _env_file=None, **kw)


@pytest.fixture
def env(client):
    client.app.dependency_overrides[get_settings] = lambda: settings()
    # These tests make many deliberate failures from one address; rate limiting is covered in tests/test_auth_ratelimit.py.
    client.app.state.auth_limiter = AuthRateLimiter(enabled=False)
    return client


def make_user(db, gid=1, name="alice") -> User:
    u = User(github_user_id=gid, username=name)
    db.add(u)
    db.commit()
    return u


def code_for(db, user, **kw) -> str:
    return svc.create_pairing_code(db, user.id, settings(), **kw)


def signed_in(client, db, user=None, label="test-browser"):
    user = user or make_user(db)
    r = client.post("/api/v1/auth/exchange", json={"code": code_for(db, user), "client_label": label})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def past(db, model_row, field, **delta):
    setattr(model_row, field, datetime.now(timezone.utc) - timedelta(**delta))
    db.commit()


# ---------------------------------------------------------------- pairing + exchange


def test_exchange_gives_short_lived_tokens_and_creates_a_session(env, db_session):
    user = make_user(db_session)
    r = env.post("/api/v1/auth/exchange", json={"code": code_for(db_session, user), "client_label": "my laptop"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 900
    assert r.headers["cache-control"] == "no-store"
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"], audience="compass-api")
    assert claims["sub"] == str(user.id) and claims["typ"] == "access" and claims["exp"] - claims["iat"] == 900
    session = db_session.scalar(select(AuthSession))
    assert session.sid == claims["sid"] and session.client_label == "my laptop" and session.user_id == user.id
    assert env.get(ME, headers=bearer(body["access_token"])).json()["id"] == user.id


def test_secrets_are_stored_only_as_hashes(env, db_session):
    user = make_user(db_session)
    code = code_for(db_session, user)
    tokens = env.post("/api/v1/auth/exchange", json={"code": code}).json()
    for table in ("auth_sessions", "pairing_codes"):
        dump = " ".join(str(v) for row in db_session.execute(text(f"SELECT * FROM {table}")) for v in row)
        for secret in (code, tokens["refresh_token"], tokens["access_token"]):
            assert secret not in dump
    assert len(db_session.scalar(select(AuthSession)).refresh_hash) == 64


def test_pairing_code_is_single_use(env, db_session):
    user = make_user(db_session)
    code = code_for(db_session, user)
    assert env.post("/api/v1/auth/exchange", json={"code": code}).status_code == 200
    again = env.post("/api/v1/auth/exchange", json={"code": code})
    assert again.status_code == 401 and again.json()["error"]["code"] == "invalid_pairing_code"
    assert db_session.query(AuthSession).count() == 1


def test_expired_unknown_and_malformed_codes_fail_identically(env, db_session):
    user = make_user(db_session)
    expired = code_for(db_session, user)
    past(db_session, db_session.scalar(select(PairingCode)), "expires_at", minutes=1)
    answers = []
    for code in (expired, "x" * 43, "y" * 20):
        r = env.post("/api/v1/auth/exchange", json={"code": code})
        answers.append((r.status_code, r.json()["error"]["code"], r.json()["error"]["message"]))
    assert len(set(answers)) == 1 and answers[0][:2] == (401, "invalid_pairing_code")  # no oracle for "expired" vs "unknown"
    # too-short / odd strings are rejected like any other wrong code: the API does not reveal what a valid code looks like
    for odd in ("short", " ", "é" * 10):
        r = env.post("/api/v1/auth/exchange", json={"code": odd})
        assert (r.status_code, r.json()["error"]["code"]) == (401, "invalid_pairing_code")
    for bad in ({}, {"code": "x" * 30, "user_id": 5}, {"code": 123}, {"code": "x" * 201}):  # structurally invalid
        assert env.post("/api/v1/auth/exchange", json=bad).status_code == 422


def test_each_code_signs_in_its_own_user(env, db_session):
    a, b = make_user(db_session, 1, "alice"), make_user(db_session, 2, "bob")
    ta, tb = signed_in(env, db_session, a), signed_in(env, db_session, b)
    assert env.get(ME, headers=bearer(ta["access_token"])).json()["username"] == "alice"
    assert env.get(ME, headers=bearer(tb["access_token"])).json()["username"] == "bob"


def test_exchange_fails_closed_without_a_server_secret(client, db_session):
    user = make_user(db_session)
    code = svc.create_pairing_code(db_session, user.id, settings())
    client.app.dependency_overrides[get_settings] = lambda: Settings(auth_secret=None, _env_file=None)
    r = client.post("/api/v1/auth/exchange", json={"code": code})
    assert r.status_code == 503 and r.json()["error"]["code"] == "auth_unavailable"


# ---------------------------------------------------------------- access tokens: missing, invalid, expired, revoked


def test_missing_invalid_and_tampered_access_tokens(env, db_session):
    tokens = signed_in(env, db_session)
    assert env.get(ME).status_code == 401
    assert env.get(ME, headers=bearer("garbage")).status_code == 401
    tampered = tokens["access_token"][:-3] + ("AAA" if not tokens["access_token"].endswith("AAA") else "BBB")
    assert env.get(ME, headers=bearer(tampered)).status_code == 401
    other_secret = jwt.encode(jwt.decode(tokens["access_token"], SECRET, algorithms=["HS256"], audience="compass-api"), "z" * 48, algorithm="HS256")
    assert env.get(ME, headers=bearer(other_secret)).status_code == 401


def test_expired_access_token_is_rejected(env, db_session):
    user = make_user(db_session)
    signed_in(env, db_session, user)
    sid = db_session.scalar(select(AuthSession)).sid
    expired = issue_access_token(user.id, sid, settings(), ttl=timedelta(seconds=-5))
    r = env.get(ME, headers=bearer(expired))
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_token_for_an_unknown_or_foreign_session_is_rejected(env, db_session):
    a, b = make_user(db_session, 1, "alice"), make_user(db_session, 2, "bob")
    signed_in(env, db_session, a)
    sid_a = db_session.scalar(select(AuthSession)).sid
    assert env.get(ME, headers=bearer(issue_access_token(a.id, "0" * 32, settings()))).status_code == 401  # no such session
    assert env.get(ME, headers=bearer(issue_access_token(b.id, sid_a, settings()))).status_code == 401  # a's session, b's identity


def test_malformed_session_claims_are_rejected(env, db_session):
    user = make_user(db_session)
    now = datetime.now(timezone.utc)
    base = {"sub": str(user.id), "iss": "opensource-compass", "aud": "compass-api", "iat": now, "exp": now + timedelta(minutes=5)}
    for extra in ({"sid": 5, "typ": "access"}, {"sid": "abc", "typ": "refresh"}, {"sid": "abc"}):
        assert env.get(ME, headers=bearer(jwt.encode({**base, **extra}, SECRET, algorithm="HS256"))).status_code == 401


def test_revoking_the_session_kills_its_access_tokens_immediately(env, db_session):
    tokens = signed_in(env, db_session)
    assert env.get(ME, headers=bearer(tokens["access_token"])).status_code == 200
    svc.revoke(db_session, db_session.scalar(select(AuthSession)), "admin_revoke")
    assert env.get(ME, headers=bearer(tokens["access_token"])).status_code == 401  # long before its own expiry
    assert env.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_absolute_session_lifetime_is_enforced(env, db_session):
    tokens = signed_in(env, db_session)
    session = db_session.scalar(select(AuthSession))
    past(db_session, session, "expires_at", seconds=1)
    assert env.get(ME, headers=bearer(tokens["access_token"])).status_code == 401
    assert env.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_deleting_the_user_ends_the_session(env, db_session):
    user = make_user(db_session)
    tokens = signed_in(env, db_session, user)
    db_session.execute(text("DELETE FROM auth_sessions"))
    db_session.delete(user)
    db_session.commit()
    assert env.get(ME, headers=bearer(tokens["access_token"])).status_code == 401


# ---------------------------------------------------------------- refresh rotation + reuse detection


def refresh(env, token):
    return env.post("/api/v1/auth/refresh", json={"refresh_token": token})


def test_refresh_rotates_the_refresh_token_and_issues_a_working_access_token(env, db_session):
    first = signed_in(env, db_session)
    r = refresh(env, first["refresh_token"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]  # rotation (same-second access tokens may be identical)
    assert env.get(ME, headers=bearer(second["access_token"])).status_code == 200
    assert refresh(env, second["refresh_token"]).status_code == 200  # the new token works, and can be rotated again
    session = db_session.scalar(select(AuthSession))
    assert session.last_refreshed_at is not None and session.revoked_at is None


def test_reusing_a_rotated_refresh_token_revokes_the_whole_session(env, db_session, caplog):
    first = signed_in(env, db_session)
    second = refresh(env, first["refresh_token"]).json()
    with caplog.at_level(logging.WARNING, logger="compass.auth"):
        stolen = refresh(env, first["refresh_token"])  # the old token again: a copy exists somewhere
    assert stolen.status_code == 401 and stolen.json()["error"]["code"] == "invalid_refresh_token"
    session = db_session.scalar(select(AuthSession))
    assert session.revoked_at is not None and session.revoked_reason == "refresh_reuse"
    # the legitimate client is signed out too: its new refresh token and access token are dead
    assert refresh(env, second["refresh_token"]).status_code == 401
    assert env.get(ME, headers=bearer(second["access_token"])).status_code == 401
    assert any("reuse detected" in r.message for r in caplog.records)


def test_unknown_expired_and_revoked_refresh_tokens_fail_identically(env, db_session):
    tokens = signed_in(env, db_session)
    answers = {(refresh(env, "x" * 43).status_code, refresh(env, "x" * 43).json()["error"]["code"])}
    session = db_session.scalar(select(AuthSession))
    past(db_session, session, "refresh_expires_at", seconds=1)
    r = refresh(env, tokens["refresh_token"])
    answers.add((r.status_code, r.json()["error"]["code"]))
    assert answers == {(401, "invalid_refresh_token")}
    assert env.post("/api/v1/auth/refresh", json={"refresh_token": "short"}).status_code == 401  # no format oracle
    assert env.post("/api/v1/auth/refresh", json={}).status_code == 422


def test_refresh_lifetime_never_exceeds_the_session_lifetime(env, db_session):
    tokens = signed_in(env, db_session)
    session = db_session.scalar(select(AuthSession))
    session.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.commit()
    body = refresh(env, tokens["refresh_token"]).json()
    assert body["refresh_expires_in"] <= 3600
    db_session.refresh(session)
    assert svc._aware(session.refresh_expires_at) <= svc._aware(session.expires_at)


def test_one_users_tokens_cannot_affect_another_users_session(env, db_session):
    a, b = make_user(db_session, 1, "alice"), make_user(db_session, 2, "bob")
    ta, tb = signed_in(env, db_session, a), signed_in(env, db_session, b)
    env.post("/api/v1/auth/logout", json={"refresh_token": ta["refresh_token"]})
    assert env.get(ME, headers=bearer(ta["access_token"])).status_code == 401
    assert env.get(ME, headers=bearer(tb["access_token"])).status_code == 200


# ---------------------------------------------------------------- logout


def test_logout_with_the_bearer_token(env, db_session):
    tokens = signed_in(env, db_session)
    r = env.post("/api/v1/auth/logout", headers=bearer(tokens["access_token"]))
    assert r.status_code == 204 and r.headers["cache-control"] == "no-store"
    assert env.get(ME, headers=bearer(tokens["access_token"])).status_code == 401
    assert refresh(env, tokens["refresh_token"]).status_code == 401
    assert db_session.scalar(select(AuthSession)).revoked_reason == "logout"


def test_logout_still_works_after_the_access_token_expired(env, db_session):
    user = make_user(db_session)
    tokens = signed_in(env, db_session, user)
    assert env.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert refresh(env, tokens["refresh_token"]).status_code == 401


def test_logout_is_idempotent_and_gives_no_oracle(env, db_session):
    tokens = signed_in(env, db_session)
    for _ in range(2):
        assert env.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert env.post("/api/v1/auth/logout", json={"refresh_token": "u" * 43}).status_code == 204  # unknown token: same reply
    assert env.post("/api/v1/auth/logout").status_code == 401  # but no credentials at all is an error
    assert env.post("/api/v1/auth/logout", headers=bearer("garbage")).status_code == 401


def test_logout_all_revokes_every_session_of_the_caller_only(env, db_session):
    a, b = make_user(db_session, 1, "alice"), make_user(db_session, 2, "bob")
    a1, a2, tb = signed_in(env, db_session, a, "laptop"), signed_in(env, db_session, a, "desktop"), signed_in(env, db_session, b)
    assert env.post("/api/v1/auth/logout-all", headers=bearer(a1["access_token"])).status_code == 204
    assert env.get(ME, headers=bearer(a1["access_token"])).status_code == 401
    assert env.get(ME, headers=bearer(a2["access_token"])).status_code == 401
    assert env.get(ME, headers=bearer(tb["access_token"])).status_code == 200
    assert env.post("/api/v1/auth/logout-all").status_code == 401


# ---------------------------------------------------------------- legacy tokens


def test_legacy_tokens_work_in_development_but_not_in_production_or_when_disabled(client, db_session):
    user = make_user(db_session)
    legacy = issue_token(user.id, settings())
    for cfg, expected in ((settings(), 200), (settings(environment="production"), 401), (settings(auth_allow_legacy_tokens=False), 401)):
        client.app.dependency_overrides[get_settings] = lambda c=cfg: c
        assert client.get(ME, headers=bearer(legacy)).status_code == expected


def test_session_tokens_work_in_production(client, db_session):
    prod = settings(environment="production")
    client.app.dependency_overrides[get_settings] = lambda: prod
    user = make_user(db_session)
    tokens = client.post("/api/v1/auth/exchange", json={"code": svc.create_pairing_code(db_session, user.id, prod)}).json()
    assert client.get(ME, headers=bearer(tokens["access_token"])).status_code == 200


# ---------------------------------------------------------------- no leakage


def test_secrets_never_appear_in_logs_or_error_bodies(env, db_session, caplog):
    user = make_user(db_session)
    code = code_for(db_session, user)
    with caplog.at_level(logging.DEBUG):
        tokens = env.post("/api/v1/auth/exchange", json={"code": code}).json()
        bad = env.post("/api/v1/auth/exchange", json={"code": code})  # reuse of the code
        stolen = tokens["refresh_token"]
        refresh(env, stolen)
        reuse = refresh(env, stolen)
        env.get(ME, headers=bearer(tokens["access_token"]))
    for secret in (code, tokens["refresh_token"], tokens["access_token"]):
        assert secret not in caplog.text
    for response in (bad, reuse):
        assert code not in response.text and stolen not in response.text


def test_only_token_endpoints_return_tokens(env, db_session):
    tokens = signed_in(env, db_session)
    for r in (env.get(ME, headers=bearer(tokens["access_token"])),):
        assert "token" not in r.text.lower()


# ---------------------------------------------------------------- CLI


def test_cli_issues_pairing_codes_and_revokes_sessions(tmp_path, monkeypatch, capsys):
    from sqlalchemy import create_engine

    import app.db.models  # noqa: F401
    from app.db import session as session_mod
    from app.db.base import Base

    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    Base.metadata.create_all(create_engine(url))
    monkeypatch.setenv("COMPASS_DATABASE_URL", url)
    monkeypatch.setenv("COMPASS_AUTH_SECRET", SECRET)
    for f in (get_settings, session_mod.get_engine, session_mod.get_sessionmaker):
        f.cache_clear()
    try:
        assert cli_main(["issue-pairing-code", "--github-id", "9", "--username", "carol"]) == 0
        out = capsys.readouterr()
        code = out.out.strip()
        assert len(code) >= 30 and "one-time" in out.err and code not in out.err
        with session_mod.get_sessionmaker()() as db:
            pair = svc.exchange_pairing_code(db, settings(), code, "cli-test")
            assert svc.exchange_pairing_code.__name__ and pair.refresh_token
        assert cli_main(["revoke-sessions", "--github-id", "9"]) == 0
        assert "revoked 1 session" in capsys.readouterr().out
        assert cli_main(["issue-token", "--github-id", "9", "--username", "carol"]) == 0
        assert "deprecated" in capsys.readouterr().err
    finally:
        for f in (get_settings, session_mod.get_engine, session_mod.get_sessionmaker):
            f.cache_clear()


# ---------------------------------------------------------------- persistent ("Keep me signed in") sessions get tighter limits


def exchange_with(env, db_session, user, **body):
    return env.post("/api/v1/auth/exchange", json={"code": code_for(db_session, user), **body})


def _secs(session, field):
    return (getattr(session, field) if getattr(session, field).tzinfo else getattr(session, field).replace(tzinfo=timezone.utc)
            ) - datetime.now(timezone.utc)


def test_persistent_sessions_use_the_tighter_idle_and_absolute_limits(env, db_session):
    user = make_user(db_session)
    r = exchange_with(env, db_session, user, persistent=True)
    assert r.status_code == 200
    body = r.json()
    s = db_session.scalar(select(AuthSession))
    assert s.persistent is True
    assert timedelta(hours=71, minutes=59) < _secs(s, "refresh_expires_at") <= timedelta(hours=72)  # idle: 3 days
    assert timedelta(days=13, hours=23) < _secs(s, "expires_at") <= timedelta(days=14)  # absolute: 14 days
    assert abs(body["refresh_expires_in"] - 72 * 3600) < 5


def test_default_sessions_keep_the_standard_limits_and_old_clients_are_unaffected(env, db_session):
    user = make_user(db_session)
    assert exchange_with(env, db_session, user).status_code == 200  # no "persistent" field, as sent by older extension builds
    s = db_session.scalar(select(AuthSession))
    assert s.persistent is False
    assert timedelta(hours=167, minutes=59) < _secs(s, "refresh_expires_at") <= timedelta(hours=168)
    assert timedelta(days=29, hours=23) < _secs(s, "expires_at") <= timedelta(days=30)


def test_refreshing_a_persistent_session_keeps_it_persistent_and_bounded(env, db_session):
    user = make_user(db_session)
    first = exchange_with(env, db_session, user, persistent=True).json()
    second = refresh(env, first["refresh_token"]).json()
    s = db_session.scalar(select(AuthSession))
    assert s.persistent is True
    assert abs(second["refresh_expires_in"] - 72 * 3600) < 5  # each rotation restarts the 3-day idle window
    past(db_session, s, "refresh_expires_at", seconds=1)  # idle for more than 3 days: the persisted token is dead...
    dead = refresh(env, second["refresh_token"])
    assert dead.status_code == 401 and dead.json()["error"]["code"] == "invalid_refresh_token"


def test_a_persistent_session_never_outlives_its_absolute_cap(env, db_session):
    user = make_user(db_session)
    tokens = exchange_with(env, db_session, user, persistent=True).json()
    s = db_session.scalar(select(AuthSession))
    s.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)  # 1 hour left of the 14 days
    db_session.commit()
    near_end = refresh(env, tokens["refresh_token"]).json()
    assert near_end["refresh_expires_in"] <= 3600  # clipped to the session's end, not a fresh 72 h
    past(db_session, s, "expires_at", seconds=1)
    assert refresh(env, near_end["refresh_token"]).status_code == 401


def test_persistent_limits_cannot_exceed_the_general_limits(client, db_session):
    cfg = Settings(auth_secret=SECRET, _env_file=None, auth_refresh_ttl_hours=24, auth_session_max_days=2,
                   auth_persistent_refresh_ttl_hours=72, auth_persistent_session_max_days=14)
    client.app.dependency_overrides[get_settings] = lambda: cfg
    client.app.state.auth_limiter = AuthRateLimiter(enabled=False)
    user = make_user(db_session)
    r = client.post("/api/v1/auth/exchange", json={"code": svc.create_pairing_code(db_session, user.id, cfg), "persistent": True})
    assert r.status_code == 200 and r.json()["refresh_expires_in"] <= 24 * 3600  # an operator's stricter general cap wins


def test_persistence_cannot_be_changed_after_the_fact_by_the_client(env, db_session):
    user = make_user(db_session)
    tokens = exchange_with(env, db_session, user).json()
    r = env.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"], "persistent": True})
    assert r.status_code == 422  # not a field of the refresh request; the flag is fixed when the session is created
    assert db_session.scalar(select(AuthSession)).persistent is False


def test_persistent_must_be_a_boolean(env, db_session):
    user = make_user(db_session)
    assert env.post("/api/v1/auth/exchange", json={"code": code_for(db_session, user), "persistent": "yes please"}).status_code == 422
