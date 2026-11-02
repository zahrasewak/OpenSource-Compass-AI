"""Rate limiting of /auth/exchange, /auth/refresh and /auth/logout, and the information the endpoints reveal.

The limiter is in-memory and per process (see app/core/ratelimit.py); these tests cover that behaviour, not multi-process
sharing, which is documented as a limitation. Rate limiting is NOT what makes pairing codes secure (192-bit random,
single use, short lived); the tests for that live in tests/test_sessions.py.
"""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from sqlalchemy.orm import sessionmaker

from app.core.ratelimit import AuthRateLimiter, DatabaseAuthRateLimiter, SlidingWindow, client_address
from app.db.models import AuthSession, AuthThrottleEvent, PairingCode, User
from app.services import sessions as svc

SECRET = "s" * 48
EXCHANGE = "/api/v1/auth/exchange"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
WRONG = "WRONG-" + "x" * 30


class Clock:
    def __init__(self) -> None:
        self.t = 10_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def settings(**kw) -> Settings:
    return Settings(auth_secret=SECRET, _env_file=None, **kw)


@pytest.fixture
def clock():
    return Clock()


def make_limiter(kind, db_session, clock, **kw):
    """The same behaviour is required of both backends, so every test below runs against each of them."""
    if kind == "database":
        factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
        return DatabaseAuthRateLimiter(factory, clock=clock, **kw)
    return AuthRateLimiter(clock=clock, **kw)


@pytest.fixture(params=["memory", "database"])
def backend(request):
    return request.param


@pytest.fixture
def env(client, clock, backend, db_session):
    client.app.dependency_overrides[get_settings] = lambda: settings()
    client.app.state.auth_limiter = make_limiter(
        backend, db_session, clock, attempts_per_minute=20, max_failures=5, failure_window_seconds=900, global_attempts_per_minute=600)
    return client


def make_user(db, gid=1, name="alice") -> User:
    u = User(github_user_id=gid, username=name)
    db.add(u)
    db.commit()
    return u


def new_code(db, user) -> str:
    return svc.create_pairing_code(db, user.id, settings())


def exchange(client, code, **headers):
    return client.post(EXCHANGE, json={"code": code}, headers=headers)


# ---------------------------------------------------------------- repeated guesses


def test_repeated_wrong_codes_lock_the_client_out_even_for_a_valid_code(env, db_session, clock):
    user = make_user(db_session)
    valid = new_code(db_session, user)
    for _ in range(5):
        r = exchange(env, WRONG)
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_pairing_code"
    locked = exchange(env, valid)  # a VALID code during the lockout is refused too, and is not looked up or consumed
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(locked.headers["retry-after"]) <= 900
    assert locked.json()["error"]["details"]["retry_after_seconds"] == int(locked.headers["retry-after"])
    assert db_session.scalar(select(PairingCode)).used_at is None  # still unused
    assert db_session.query(AuthSession).count() == 0
    clock.advance(901)  # the failures age out
    ok = exchange(env, valid)
    assert ok.status_code == 200 and db_session.query(AuthSession).count() == 1


def test_the_lockout_gives_the_same_answer_for_valid_and_invalid_codes(env, db_session):
    user = make_user(db_session)
    valid = new_code(db_session, user)
    for _ in range(5):
        exchange(env, WRONG)
    a, b = exchange(env, valid), exchange(env, WRONG)
    strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
    assert a.status_code == b.status_code == 429 and strip(a) == strip(b)  # a blocked client learns nothing about validity


def test_the_lockout_expires_gradually_not_all_at_once(env, clock):
    for _ in range(5):
        exchange(env, WRONG)
        clock.advance(100)  # failures spread over 500 s
    assert exchange(env, WRONG).status_code == 429
    clock.advance(401)  # the oldest failure (age 901 s) has expired, the others have not
    assert exchange(env, WRONG).status_code == 401  # one slot opened...
    assert exchange(env, WRONG).status_code == 429  # ...and is used up again


def test_failures_are_counted_per_client_address(env, db_session):
    user = make_user(db_session)
    env.app.dependency_overrides[get_settings] = lambda: settings(trusted_proxy_hops=1)
    for _ in range(5):
        exchange(env, WRONG, **{"X-Forwarded-For": "203.0.113.7"})
    assert exchange(env, WRONG, **{"X-Forwarded-For": "203.0.113.7"}).status_code == 429
    other = exchange(env, new_code(db_session, user), **{"X-Forwarded-For": "198.51.100.9"})  # another client is unaffected
    assert other.status_code == 200


def test_x_forwarded_for_is_ignored_unless_a_proxy_is_trusted(env, clock):
    for i in range(5):  # a client cannot dodge the lockout by inventing addresses
        assert exchange(env, WRONG, **{"X-Forwarded-For": f"203.0.113.{i}"}).status_code == 401
    assert exchange(env, WRONG, **{"X-Forwarded-For": "203.0.113.99"}).status_code == 429


def test_a_spoofed_prefix_does_not_help_when_a_proxy_is_trusted(env):
    env.app.dependency_overrides[get_settings] = lambda: settings(trusted_proxy_hops=1)
    for i in range(5):  # the proxy appends the real address LAST; whatever the client prepends is ignored
        assert exchange(env, WRONG, **{"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"}).status_code == 401
    assert exchange(env, WRONG, **{"X-Forwarded-For": "10.9.9.9, 203.0.113.7"}).status_code == 429


# ---------------------------------------------------------------- legitimate use


def test_legitimate_exchanges_are_not_throttled_and_do_not_count_as_failures(env, db_session):
    user = make_user(db_session)
    for _ in range(10):
        assert exchange(env, new_code(db_session, user)).status_code == 200
    assert exchange(env, WRONG).status_code == 401  # one typo after ten successes is just a typo


def test_a_mistyped_code_then_the_right_one_still_works(env, db_session):
    user = make_user(db_session)
    valid = new_code(db_session, user)
    for _ in range(4):
        assert exchange(env, WRONG).status_code == 401
    assert exchange(env, valid).status_code == 200  # 4 failures < 5: no lockout


def test_attempts_per_minute_cap_applies_to_every_request_and_resets(env, db_session, clock):
    user = make_user(db_session)
    for _ in range(20):
        assert exchange(env, new_code(db_session, user)).status_code == 200
    capped = exchange(env, new_code(db_session, user))
    assert capped.status_code == 429 and int(capped.headers["retry-after"]) <= 60
    clock.advance(61)
    assert exchange(env, new_code(db_session, user)).status_code == 200


def test_global_ceiling_protects_the_database_from_many_clients(client, db_session, clock, backend):
    client.app.dependency_overrides[get_settings] = lambda: settings(trusted_proxy_hops=1)
    client.app.state.auth_limiter = make_limiter(backend, db_session, clock, attempts_per_minute=5, max_failures=50, global_attempts_per_minute=12)
    codes = [exchange(client, WRONG, **{"X-Forwarded-For": f"198.51.100.{i}"}).status_code for i in range(15)]
    assert codes[:12] == [401] * 12 and codes[12:] == [429] * 3


def test_disabling_the_limiter_is_possible_but_explicit(client, db_session, clock, backend):
    client.app.dependency_overrides[get_settings] = lambda: settings()
    client.app.state.auth_limiter = make_limiter(backend, db_session, clock, enabled=False)
    assert {exchange(client, WRONG).status_code for _ in range(30)} == {401}


# ---------------------------------------------------------------- the other unauthenticated endpoints


def test_refresh_failures_lock_out_but_a_valid_token_is_not_penalised(env, db_session):
    user = make_user(db_session)
    tokens = exchange(env, new_code(db_session, user)).json()
    for _ in range(5):
        assert env.post(REFRESH, json={"refresh_token": WRONG}).status_code == 401
    locked = env.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    assert locked.status_code == 429 and "retry-after" in locked.headers
    assert db_session.scalar(select(AuthSession)).prev_refresh_hash is None  # the valid token was never even looked at


def test_logout_is_rate_limited_per_client_but_never_reveals_anything(env, clock):
    codes = {env.post(LOGOUT, json={"refresh_token": WRONG}).status_code for _ in range(20)}
    assert codes == {204}  # same answer whether or not a session exists
    assert env.post(LOGOUT, json={"refresh_token": WRONG}).status_code == 429
    clock.advance(61)
    assert env.post(LOGOUT, json={"refresh_token": WRONG}).status_code == 204


def test_scopes_are_independent(env):
    for _ in range(5):
        exchange(env, WRONG)
    assert exchange(env, WRONG).status_code == 429
    assert env.post(REFRESH, json={"refresh_token": WRONG}).status_code == 401  # a pairing lockout does not block refresh


# ---------------------------------------------------------------- pairing codes: expiry and reuse (with the limiter on)


def test_expired_and_reused_codes_are_rejected_identically_and_count_as_failures(env, db_session):
    user = make_user(db_session)
    used = new_code(db_session, user)
    assert exchange(env, used).status_code == 200
    expired = new_code(db_session, user)
    row = db_session.scalar(select(PairingCode).where(PairingCode.used_at.is_(None)))
    row.expires_at = row.expires_at - timedelta(minutes=30)
    db_session.commit()
    answers = [(r.status_code, r.json()["error"]["code"], r.json()["error"]["message"]) for r in (exchange(env, used), exchange(env, expired), exchange(env, WRONG))]
    assert len(set(answers)) == 1 and answers[0][:2] == (401, "invalid_pairing_code")
    for _ in range(2):
        exchange(env, WRONG)
    assert exchange(env, WRONG).status_code == 429  # 5 failures in total: reuse, expiry and guesses all count


def test_a_code_cannot_be_exchanged_twice_even_in_quick_succession(env, db_session):
    user = make_user(db_session)
    code = new_code(db_session, user)
    assert [exchange(env, code).status_code for _ in range(3)] == [200, 401, 401]
    assert db_session.query(AuthSession).count() == 1


# ---------------------------------------------------------------- no credentials in logs or responses


def test_no_credential_appears_in_logs_or_error_bodies_even_when_locked_out(env, db_session, caplog):
    user = make_user(db_session)
    valid = new_code(db_session, user)
    tokens = exchange(env, new_code(db_session, user)).json()
    secrets_seen = [valid, WRONG, tokens["access_token"], tokens["refresh_token"]]
    bodies = []
    with caplog.at_level(logging.DEBUG):
        for _ in range(6):
            bodies.append(exchange(env, WRONG).text)
        bodies.append(exchange(env, valid).text)  # locked out
        for _ in range(6):
            bodies.append(env.post(REFRESH, json={"refresh_token": WRONG}).text)
        bodies.append(env.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}).text)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "locked out" in logged and "retry_after" in logged  # the lockout is visible to operators...
    for s in secrets_seen:
        assert s not in logged and not any(s in b for b in bodies)  # ...without any credential in it


def test_refusals_are_logged_once_per_minute_so_an_attacker_cannot_flood_the_log(env, clock, caplog):
    with caplog.at_level(logging.WARNING, logger="compass.auth.ratelimit"):
        for _ in range(5):
            exchange(env, WRONG)
        for _ in range(50):
            assert exchange(env, WRONG).status_code == 429
        refusals = [r for r in caplog.records if "retry_after" in r.getMessage()]
        assert len(refusals) == 1  # 50 refused requests, one log line
        clock.advance(61)
        for _ in range(5):
            exchange(env, WRONG)  # still locked (failures are within the 15 minute window)
        assert len([r for r in caplog.records if "retry_after" in r.getMessage()]) == 2  # logged again after a minute


# ---------------------------------------------------------------- the limiter itself


def test_sliding_window_basics_and_memory_bound():
    c = Clock()
    w = SlidingWindow(limit=2, window=10, clock=c, max_keys=3)
    assert w.retry_after("a") is None
    w.add("a")
    w.add("a")
    assert w.retry_after("a") == pytest.approx(10)
    c.advance(4)
    assert w.retry_after("a") == pytest.approx(6)
    c.advance(6.1)
    assert w.retry_after("a") is None and w.count("a") == 0
    for k in "bcdef":
        w.add(k)
    assert len(w._events) <= 3  # bounded: a flood of distinct addresses cannot grow memory without limit


@pytest.mark.parametrize("peer,xff,hops,expected", [
    ("10.0.0.1", "203.0.113.7", 0, "10.0.0.1"),
    ("10.0.0.1", "203.0.113.7", 1, "203.0.113.7"),
    ("10.0.0.1", "6.6.6.6, 203.0.113.7", 1, "203.0.113.7"),
    ("10.0.0.1", "203.0.113.7, 10.1.1.1", 2, "203.0.113.7"),
    ("10.0.0.1", "203.0.113.7", 2, "10.0.0.1"),  # fewer entries than trusted hops: cannot be trusted
    ("10.0.0.1", "not-an-ip", 1, "10.0.0.1"),
    ("10.0.0.1", None, 1, "10.0.0.1"),
    ("10.0.0.1", "2001:db8::1", 1, "2001:db8::1"),
    (None, None, 0, "unknown"),
])
def test_client_address(peer, xff, hops, expected):
    assert client_address(peer, xff, hops) == expected


# ---------------------------------------------------------------- several workers / instances (the point of the database backend)


def _two_workers(client, db_session, clock, kind, **kw):
    """Two limiter objects = two worker processes. The database backend shares state through the table; memory does not."""
    return make_limiter(kind, db_session, clock, **kw), make_limiter(kind, db_session, clock, **kw)


def test_the_database_backend_enforces_one_limit_across_workers(client, db_session, clock):
    a, b = _two_workers(client, db_session, clock, "database", max_failures=5)
    for _ in range(5):
        assert a.check_and_count("exchange", "203.0.113.7") is None
        a.record_failure("exchange", "203.0.113.7")
    assert b.check_and_count("exchange", "203.0.113.7") is not None  # worker B refuses what worker A observed
    assert b.check_and_count("exchange", "198.51.100.1") is None  # other clients are unaffected
    clock.advance(901)
    assert b.check_and_count("exchange", "203.0.113.7") is None  # and the lockout ends for both


def test_the_memory_backend_is_per_worker_which_is_why_the_database_backend_exists(client, db_session, clock):
    a, b = _two_workers(client, db_session, clock, "memory", max_failures=5)
    for _ in range(5):
        a.check_and_count("exchange", "203.0.113.7")
        a.record_failure("exchange", "203.0.113.7")
    assert a.check_and_count("exchange", "203.0.113.7") is not None
    assert b.check_and_count("exchange", "203.0.113.7") is None  # DOCUMENTED LIMITATION: the other worker knows nothing


def test_database_backend_failures_survive_the_rollback_of_the_failing_request(env, db_session, backend):
    if backend != "database":
        pytest.skip("database backend only")
    for _ in range(3):
        exchange(env, WRONG)  # the request raises 401; its own transaction is rolled back, the failure rows must remain
    assert db_session.query(AuthThrottleEvent).filter_by(kind="failure").count() == 3


def test_database_backend_cleans_up_old_rows_and_stores_no_credentials(env, db_session, clock, backend):
    if backend != "database":
        pytest.skip("database backend only")
    user = make_user(db_session)
    code = new_code(db_session, user)
    exchange(env, WRONG)
    exchange(env, code)
    rows = db_session.query(AuthThrottleEvent).all()
    assert rows and {r.scope for r in rows} == {"exchange"} and {r.kind for r in rows} <= {"attempt", "failure"}
    dump = " ".join(f"{r.scope} {r.client} {r.kind}" for r in rows)
    assert WRONG not in dump and code not in dump
    clock.advance(3 * 3600)
    exchange(env, WRONG)  # the next check (after >= 1 min) deletes everything older than the longest window
    assert db_session.query(AuthThrottleEvent).count() <= 2  # only the rows of this last request remain


def test_the_backend_is_chosen_by_configuration_and_production_warns_about_the_memory_backend(monkeypatch):
    from app.main import create_app

    monkeypatch.setattr("app.main.get_settings", lambda: settings(auth_rate_limit_backend="database"))
    assert isinstance(create_app().state.auth_limiter, DatabaseAuthRateLimiter)
    monkeypatch.setattr("app.main.get_settings", lambda: settings())
    assert isinstance(create_app().state.auth_limiter, AuthRateLimiter)

    records = []

    class Grab(logging.Handler):  # create_app() resets the root handlers, so attach to the logger itself
        def emit(self, record):
            records.append(record.getMessage())

    log = logging.getLogger("compass.auth.ratelimit")
    handler = Grab(level=logging.WARNING)
    log.addHandler(handler)
    try:
        monkeypatch.setattr("app.main.get_settings", lambda: settings(environment="production"))
        create_app()
    finally:
        log.removeHandler(handler)
    assert any("per-process memory backend" in m for m in records)
