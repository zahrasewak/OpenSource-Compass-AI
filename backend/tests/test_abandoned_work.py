"""Abandoned requests: does the backend keep working (and spending GitHub quota) after the client has gone?

Stage 14 inferred that it does (quota kept dropping after the panel stopped waiting). These tests REPRODUCE it against a
real uvicorn server with a slow fake GitHub and a raw socket that disconnects mid-request, then show the behaviour with
the cancellation mechanism off (the control = the behaviour before Stage 15) and on.

Root cause: endpoints and the GitHub client are synchronous and run in worker threads; closing the connection never
reached those threads. Fix: app/core/requestctx.py + app/core/middleware.py + checkpoints in the GitHub client and the
single-flight locks. Limits that remain, and are asserted here so they are not forgotten: a request already in flight to
GitHub finishes (at most one, bounded by the time budget), and work other requests are waiting for is deliberately NOT
cancelled.
"""

import http.client
import json
import socket
import threading
import time
from collections import namedtuple

import pytest
import uvicorn
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_github_client
from app.core import requestctx
from app.core.config import Settings, get_settings
from app.core.middleware import RequestContextMiddleware
from app.core.security import issue_token
from app.db.base import Base
from app.db.models import RepositorySnapshot, User
from app.db.session import get_db
from app.main import create_app
from app.services import intelligence as intel
from app.services.github.errors import GitHubDeadlineError
from app.services.singleflight import KeyedLocks, locks
from tests import intel_data as D
from tests.github_mock import FakeGitHub, status
from tests.test_caching import SlowGitHub

SECRET = "s" * 48
HEALTH = "/api/v1/repositories/o/r/health"
Live = namedtuple("Live", "port gh factory token")


# ---------------------------------------------------------------- a real server, a slow GitHub, and clients that can hang up


@pytest.fixture
def live(tmp_path, monkeypatch):
    started = []

    def start(*, gh, **settings_kw) -> Live:
        cfg = Settings(auth_secret=SECRET, _env_file=None, **settings_kw)
        monkeypatch.setattr("app.main.get_settings", lambda: cfg)  # the middleware reads its budgets when the app is built
        engine = create_engine(f"sqlite:///{(tmp_path / 'live.db').as_posix()}", connect_args={"timeout": 30})

        @event.listens_for(engine, "connect")
        def _wal(conn, _):
            conn.execute("PRAGMA journal_mode=WAL")

        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            user = User(github_user_id=1, username="alice")
            db.add(user)
            db.commit()
            token = issue_token(user.id, cfg)

        app = create_app()

        def db_for_request():
            with factory() as session:
                yield session

        app.dependency_overrides[get_db] = db_for_request
        app.dependency_overrides[get_settings] = lambda: cfg
        app.dependency_overrides[get_github_client] = lambda: gh.client
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        started.append((server, thread, engine))
        return Live(port, gh, factory, token)

    yield start
    for server, thread, engine in started:
        server.should_exit = True
        thread.join(timeout=10)
        engine.dispose()


def abandon(live: Live, path: str, after: float) -> int:
    """Send a request, hang up after `after` seconds without reading the answer; return GitHub calls made so far."""
    s = socket.create_connection(("127.0.0.1", live.port))
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {live.token}\r\nConnection: close\r\n\r\n".encode())
    time.sleep(after)
    s.close()
    return live.gh.total


def get(live: Live, path: str):
    t0 = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=30)
    conn.request("GET", path, headers={"Authorization": f"Bearer {live.token}"})
    resp = conn.getresponse()
    body = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, body, time.time() - t0


def settle(gh, quiet=1.0, timeout=12.0) -> int:
    """Wait until GitHub has not been called for `quiet` seconds; return the final call count."""
    last, since, end = gh.total, time.time(), time.time() + timeout
    while time.time() < end:
        time.sleep(0.1)
        if gh.total != last:
            last, since = gh.total, time.time()
        elif time.time() - since >= quiet:
            break
    return gh.total


def slow_github(delay=0.3, **extra):
    return SlowGitHub(D.healthy_routes(**extra), delay=delay)


# ---------------------------------------------------------------- 1. reproduction (control) and 2. the fix


def test_CONTROL_without_cancellation_an_abandoned_request_keeps_spending_github_calls(live):
    """Behaviour before Stage 15 (cancellation switched off): the work continues long after the client has gone."""
    lv = live(gh=slow_github(), request_cancellation_enabled=False)
    at_hangup = abandon(lv, HEALTH, after=0.7)
    final = settle(lv.gh)
    print(f"[abandoned-work] CONTROL (cancellation off): GitHub calls at hang-up={at_hangup}, final={final}")
    assert at_hangup <= 3, "the client hung up early in the analysis"
    assert final >= at_hangup + 5, f"expected the analysis to run on after the disconnect (calls {at_hangup} -> {final})"
    with lv.factory() as db:  # ...and the result was stored for nobody: the only beneficiary is a later retry
        assert db.scalar(select(RepositorySnapshot)) is not None


def test_FIX_a_disconnect_stops_further_github_calls_and_leaves_no_partial_state(live):
    lv = live(gh=slow_github(), request_cancellation_enabled=True)
    at_hangup = abandon(lv, HEALTH, after=0.7)
    final = settle(lv.gh)
    print(f"[abandoned-work] FIX (cancellation on): GitHub calls at hang-up={at_hangup}, final={final}")
    assert final <= at_hangup + 2, f"work continued after the disconnect (calls {at_hangup} -> {final})"  # <= one request already in flight
    with lv.factory() as db:
        assert db.scalar(select(RepositorySnapshot)) is None  # nothing half-written
    assert len(locks) == 0  # the single-flight lock was released
    # the server is healthy afterwards and a retry completes (it reuses whatever the ETag cache and database already hold)
    status_code, body, _ = get(lv, HEALTH)
    assert status_code == 200 and body["freshness"]["complete"] is True


def test_FIX_cancelled_work_is_not_counted_against_a_second_identical_request_that_is_waiting_for_it(live):
    """Single-flight shield: request B is waiting for request A's analysis; A's client hangs up; the work must continue for B."""
    lv = live(gh=slow_github(), request_cancellation_enabled=True)
    result = {}
    waiter = threading.Thread(target=lambda: result.update(zip(("status", "body", "elapsed"), get(lv, HEALTH))))
    leader = threading.Thread(target=lambda: abandon(lv, HEALTH, after=1.0))
    leader.start()
    time.sleep(0.2)
    waiter.start()
    leader.join()
    waiter.join(timeout=30)
    assert result["status"] == 200 and result["body"]["freshness"]["complete"] is True
    total_single_analysis = lv.gh.total
    settle(lv.gh)
    assert lv.gh.total == total_single_analysis  # nobody re-ran the analysis; one collection served the waiter
    with lv.factory() as db:
        assert db.query(RepositorySnapshot).count() == 1


# ---------------------------------------------------------------- 3. bounded time: partial results instead of a long wait


def test_a_time_budget_returns_a_partial_analysis_quickly(live):
    lv = live(gh=slow_github(delay=0.3), slow_request_deadline_seconds=1.0)
    status_code, body, elapsed = get(lv, HEALTH)
    print(f"[abandoned-work] BUDGET 1 s: status={status_code} elapsed={elapsed:.2f}s calls={lv.gh.total} complete={body['freshness']['complete']} errors={sorted(set(body['freshness']['errors'].values()))}")
    assert status_code == 200
    assert elapsed < 2.4, f"took {elapsed:.1f}s although the budget was 1s (a full analysis needs ~3s)"
    assert body["freshness"]["complete"] is False
    assert "github_timeout" in set(body["freshness"]["errors"].values())  # which signals are missing, and why
    assert lv.gh.total <= 6  # it stopped asking GitHub when the budget ran out
    with lv.factory() as db:  # stored as an INCOMPLETE snapshot: retried soon, never presented as complete
        assert db.scalar(select(RepositorySnapshot)).data["complete"] is False


def test_CONTROL_without_a_budget_the_same_analysis_runs_to_completion(live):
    lv = live(gh=slow_github(delay=0.3), slow_request_deadline_seconds=300)
    status_code, body, elapsed = get(lv, HEALTH)
    print(f"[abandoned-work] NO BUDGET: status={status_code} elapsed={elapsed:.2f}s calls={lv.gh.total}")
    assert status_code == 200 and body["freshness"]["complete"] is True and elapsed > 2.0


def test_a_budget_stops_retry_loops_on_ordinary_endpoints(live):
    gh = SlowGitHub({"/repos/o/r": status(503)}, delay=0.4)
    lv = live(gh=gh, request_deadline_seconds=0.7)
    status_code, body, elapsed = get(lv, "/api/v1/repositories/o/r")
    assert status_code == 504 and body["error"]["code"] == "github_timeout"
    assert elapsed < 1.5 and gh.total <= 2  # without a budget: 4 attempts, ~1.6s, then a 502


def test_CONTROL_without_a_budget_a_failing_upstream_is_retried_to_the_limit(live):
    gh = SlowGitHub({"/repos/o/r": status(503)}, delay=0.4)
    lv = live(gh=gh, request_deadline_seconds=300)
    status_code, body, _ = get(lv, "/api/v1/repositories/o/r")
    assert status_code == 502 and gh.total == 4


# ---------------------------------------------------------------- pieces, in isolation


def test_the_middleware_reports_a_disconnect_to_worker_threads_and_forwards_messages_in_order():
    import asyncio

    seen = {}

    async def app(scope, receive, send):
        ctx = requestctx.current()
        seen["first"] = await receive()
        seen["cancelled_before"] = ctx.cancel.is_set()
        seen["second"] = await receive()  # the disconnect is forwarded to the application too
        seen["third"] = await receive()  # ...and keeps being reported
        seen["cancelled_after"] = ctx.cancel.is_set()

    messages = iter([{"type": "http.request", "body": b"x", "more_body": False}, {"type": "http.disconnect"}])

    async def receive():
        try:
            return next(messages)
        except StopIteration:
            await asyncio.sleep(3600)

    asyncio.run(RequestContextMiddleware(app)({"type": "http", "path": "/x"}, receive, lambda m: None))
    assert seen["first"]["type"] == "http.request" and seen["second"]["type"] == "http.disconnect" and seen["third"]["type"] == "http.disconnect"
    assert seen["cancelled_after"] is True
    assert requestctx.current() is None  # the context does not leak out of the request


def test_the_slow_budget_applies_only_to_the_expensive_endpoints():
    m = RequestContextMiddleware(None, deadline_seconds=25, slow_deadline_seconds=60)
    assert m._budget("/api/v1/repositories/o/r/health") == 60
    assert m._budget("/api/v1/issues/o/r/7/readiness") == 60
    assert m._budget("/api/v1/issues/o/r/7/match") == 25 and m._budget("/api/v1/repositories/o/r") == 25


def _ctx(**kw):
    ctx = requestctx.RequestContext(**kw)
    return ctx, requestctx.activate(ctx)


def test_the_client_stops_before_its_next_request_after_a_cancel():
    gh = FakeGitHub({"/repos/o/r": D.healthy_routes()["/repos/o/r"]})
    ctx, token = _ctx()
    try:
        gh.client.get_repository("o", "r")
        ctx.cancel.set()
        with pytest.raises(requestctx.RequestCancelled):
            gh.client.get_repository("o", "r")
        assert gh.total == 1
    finally:
        requestctx.deactivate(token)


def test_a_cancel_during_a_backoff_wait_is_noticed_within_one_slice():
    gh = FakeGitHub({"/repos/o/r": status(503)})
    ctx, token = _ctx(deadline_seconds=600)
    slices = []

    def sleeper(seconds):
        slices.append(seconds)
        ctx.cancel.set()  # the client hangs up while we are "sleeping"

    gh.client._sleep = sleeper
    try:
        with pytest.raises(requestctx.RequestCancelled):
            gh.client.get_repository("o", "r")
        assert len(slices) == 1 and slices[0] <= 0.25 and gh.total == 1  # one short slice, no further request
    finally:
        requestctx.deactivate(token)


def test_a_wait_longer_than_the_remaining_budget_fails_immediately_instead_of_sleeping():
    gh = FakeGitHub({"/repos/o/r": status(503)})
    ctx, token = _ctx(deadline_seconds=0.05)
    try:
        gh.script.sleeps.clear()
        with pytest.raises(GitHubDeadlineError):
            gh.client.get_repository("o", "r")
        assert gh.script.sleeps == [] and gh.total <= 1
    finally:
        requestctx.deactivate(token)


def test_outside_a_request_nothing_changes():
    gh = FakeGitHub({"/repos/o/r": status(503)})
    assert requestctx.current() is None
    from app.services.github.errors import GitHubServerError

    with pytest.raises(GitHubServerError):
        gh.client.get_repository("o", "r")
    assert gh.total == 4 and len(gh.script.sleeps) == 3  # the original retry behaviour, unsliced


def test_a_cancelled_collection_writes_nothing(db_session):
    from tests.seed import SETTINGS, seed_repo

    repo = seed_repo(db_session, "o", "r", "Python")
    gh = FakeGitHub(D.healthy_routes())
    ctx, token = _ctx()
    real = gh.client._send
    calls = []

    def cancel_after_two(url, etag, remaining=None):
        calls.append(url.path)
        if len(calls) == 2:
            ctx.cancel.set()
        return real(url, etag, remaining)

    gh.client._send = cancel_after_two
    try:
        with pytest.raises(requestctx.RequestCancelled):
            intel.collect_repository(db_session, gh.client, SETTINGS, repo, intel.utcnow())
    finally:
        requestctx.deactivate(token)
    assert len(calls) == 2
    assert db_session.scalar(select(RepositorySnapshot)) is None


def test_a_waiter_that_is_cancelled_stops_waiting_and_leaks_nothing():
    kl = KeyedLocks()
    ctx, token = _ctx()
    release = threading.Event()
    holder_in = threading.Event()

    def holder():
        with kl.hold("k"):
            holder_in.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    holder_in.wait(5)
    threading.Timer(0.3, ctx.cancel.set).start()
    try:
        t0 = time.time()
        with pytest.raises(requestctx.RequestCancelled):
            with kl.hold("k"):
                pass
        assert time.time() - t0 < 2
    finally:
        requestctx.deactivate(token)
        release.set()
        t.join()
    assert len(kl) == 0


def test_a_waiter_out_of_time_stops_waiting():
    kl = KeyedLocks()
    ctx, token = _ctx(deadline_seconds=0.3)
    release, holder_in = threading.Event(), threading.Event()

    def holder():
        with kl.hold("k"):
            holder_in.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    holder_in.wait(5)
    try:
        with pytest.raises(GitHubDeadlineError):
            with kl.hold("k"):
                pass
    finally:
        requestctx.deactivate(token)
        release.set()
        t.join()
    assert len(kl) == 0


def test_a_holder_is_shielded_from_cancellation_only_while_somebody_waits():
    kl = KeyedLocks()
    ctx, token = _ctx()
    t = None
    try:
        with kl.hold("k"):
            ctx.cancel.set()
            assert kl.waiters("k") == 0 and ctx.cancelled() is True  # nobody waits: the disconnect counts

            def waiter():
                with kl.hold("k"):
                    pass

            t = threading.Thread(target=waiter)
            t.start()
            end = time.time() + 3
            while kl.waiters("k") == 0 and time.time() < end:
                time.sleep(0.01)
            assert kl.waiters("k") == 1 and ctx.cancelled() is False  # somebody waits: keep working for them
    finally:
        requestctx.deactivate(token)
        if t:
            t.join(timeout=5)
    assert len(kl) == 0
