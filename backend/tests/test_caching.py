"""Fewer GitHub calls: single-flight de-duplication, conditional requests, fail-fast on rate limits, saved lookup.
GitHub is mocked; nothing here depends on GitHub's real rate limits."""

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import app.db.models  # noqa: F401
from app.api.deps import get_github_client
from app.core.config import get_settings
from app.db.base import Base
from app.db.models import Issue, Repository
from app.services import intelligence as intel
from app.services import repositories as repo_service
from app.services.singleflight import KeyedLocks
from tests import intel_data as D
from tests.github_mock import NOW, REPO, FakeGitHub, ok, status
from tests.seed import SETTINGS, make_user, seed_issue, seed_repo


class NoLocks:
    """Stand-in that disables single-flight, used as the control in each concurrency test."""

    def hold(self, key):
        return nullcontext()


class SlowGitHub(FakeGitHub):
    """Thread-safe call counting plus a delay, so concurrent requests really overlap."""

    def __init__(self, routes, delay=0.25):
        self._lock = threading.Lock()
        self.delay = delay
        super().__init__(routes)

    def _handle(self, request):
        with self._lock:
            self.calls[request.url.path] += 1
        time.sleep(self.delay)
        route = self.routes.get(request.url.path)
        if route is None:
            return status(404, {"message": "Not Found"})
        return route(request) if callable(route) else route


@pytest.fixture
def factory(tmp_path):
    """One session per thread against a real database: file-backed SQLite (WAL) by default, or PostgreSQL when
    COMPASS_TEST_SUITE_DATABASE_URL is set (then the single-flight tests also exercise real row-level concurrency)."""
    url = os.environ.get("COMPASS_TEST_SUITE_DATABASE_URL")
    if url:
        engine = create_engine(url, pool_size=16, max_overflow=0)
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}", connect_args={"timeout": 30})

        @event.listens_for(engine, "connect")
        def _wal(conn, _):
            conn.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    if url:
        Base.metadata.drop_all(engine)
    engine.dispose()


def run_concurrently(n, fn):
    barrier = threading.Barrier(n)

    def task():
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(n) as pool:
        return [f.result() for f in [pool.submit(task) for _ in range(n)]]


# ---------------------------------------------------------------- the lock itself


def test_keyed_lock_serialises_one_key_runs_different_keys_in_parallel_and_cleans_up():
    locks, active, peak = KeyedLocks(), {"n": 0, "max": 0}, threading.Lock()

    def critical(key):
        with locks.hold(key):
            with peak:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.05)
            with peak:
                active["n"] -= 1

    run_concurrently(6, lambda: critical("same"))
    assert active["max"] == 1  # one at a time for the same key
    active["max"] = 0
    barrier = threading.Barrier(2)

    def parallel(key):
        with locks.hold(key):
            barrier.wait(timeout=2)  # both keys must be inside their critical sections at once

    with ThreadPoolExecutor(2) as pool:
        for f in [pool.submit(parallel, "a"), pool.submit(parallel, "b")]:
            f.result()
    assert len(locks) == 0  # idle keys are forgotten (no memory growth)


def test_keyed_lock_is_released_when_the_work_fails():
    locks = KeyedLocks()
    with pytest.raises(RuntimeError):
        with locks.hold("k"):
            raise RuntimeError("boom")
    with locks.hold("k"):
        pass
    assert len(locks) == 0


# ---------------------------------------------------------------- concurrent requests share one GitHub call


def sync_repo(factory, gh):
    def go():
        with factory() as db:
            return repo_service.get_or_sync_repository(db, gh.client, SETTINGS, "o", "r").id
    return go


def test_concurrent_requests_for_one_repository_make_one_github_call(factory):
    gh = SlowGitHub({"/repos/o/r": ok(REPO)})
    ids = run_concurrently(8, sync_repo(factory, gh))
    assert gh.calls["/repos/o/r"] == 1 and len(set(ids)) == 1
    with factory() as db:
        assert db.query(Repository).count() == 1


def test_control_without_single_flight_each_request_calls_github(factory, monkeypatch):
    monkeypatch.setattr(repo_service, "locks", NoLocks())
    gh = SlowGitHub({"/repos/o/r": ok(REPO)})
    run_concurrently(8, sync_repo(factory, gh))
    assert gh.calls["/repos/o/r"] > 1  # proves the test above can detect the problem it guards against


def test_opening_an_issue_page_fires_overlapping_requests_but_fetches_each_resource_once(factory):
    """match + difficulty + readiness all resolve the same issue: one repository call and one issue call in total."""
    gh = SlowGitHub({"/repos/o/r": ok(REPO), "/repos/o/r/issues/7": ok(D.issue_payload(7, 5, body="x" * 100))})

    def resolve():
        with factory() as db:
            repo, issue = intel.find_issue_rows(db, gh.client, SETTINGS, "o", "r", 7)
            return issue.issue_number

    assert run_concurrently(8, resolve) == [7] * 8
    assert gh.calls["/repos/o/r"] == 1 and gh.calls["/repos/o/r/issues/7"] == 1


def test_concurrent_health_analyses_collect_each_signal_once(factory):
    with factory() as db:
        repo = seed_repo(db, "o", "r", "Python")
        repo_id = repo.id
    gh = SlowGitHub(D.healthy_routes(), delay=0.1)

    def analyse():
        with factory() as db:
            snap, err = intel.ensure_repo_intelligence(db, gh.client, SETTINGS, db.get(Repository, repo_id))
            return snap.id, err

    results = run_concurrently(6, analyse)
    assert {err for _, err in results} == {None} and len({sid for sid, _ in results}) == 1
    for path in ("/repos/o/r/commits", "/repos/o/r/releases", "/repos/o/r/pulls", "/repos/o/r/issues/comments"):
        assert gh.calls[path] == 1, path
    with factory() as db:
        assert db.query(type(intel.latest_snapshot(db, repo_id))).count() == 1


def test_concurrent_linked_pr_lookups_share_one_timeline_request(factory):
    with factory() as db:
        repo = seed_repo(db, "o", "r", "Python")
        issue = seed_issue(db, repo, 9, "t", "b" * 100)
        repo_id, issue_id = repo.id, issue.id
    gh = SlowGitHub({"/repos/o/r/issues/9/timeline": ok(D.timeline_payload([(50, "open", None)]))})

    def lookup():
        with factory() as db:
            return intel.ensure_linked_prs(db, gh.client, SETTINGS, db.get(Repository, repo_id), db.get(Issue, issue_id))[0]

    out = run_concurrently(6, lookup)
    assert gh.calls["/repos/o/r/issues/9/timeline"] == 1
    assert all(o["pull_requests"][0]["number"] == 50 for o in out)


# ---------------------------------------------------------------- conditional requests (ETag / 304)


def etag_routes(routes, log):
    """Wrap static routes so they answer If-None-Match with 304, and record which requests got a full body."""
    out = {}
    for path, response in routes.items():
        if not isinstance(response, httpx.Response):
            out[path] = response
            continue
        tag = '"' + hashlib.md5(response.content).hexdigest() + '"'

        def route(request, response=response, tag=tag, path=path):
            if request.headers.get("if-none-match") == tag:
                log.append(("304", path))
                return httpx.Response(304)
            log.append(("200", path))
            return httpx.Response(200, content=response.content, headers={"etag": tag, "content-type": "application/json"})

        out[path] = route
    return out


def test_repeat_analysis_reuses_etags_so_unchanged_data_is_not_downloaded_again(factory):
    with factory() as db:
        repo = seed_repo(db, "o", "r", "Python")
    log: list = []
    since_values: list = []
    routes = etag_routes(D.healthy_routes(), log)
    inner = routes["/repos/o/r/issues"]
    routes["/repos/o/r/issues"] = lambda r: (since_values.append(r.url.params.get("since")), inner(r))[1]
    gh = FakeGitHub(routes)
    morning = datetime.now(timezone.utc).replace(hour=3, minute=0, second=0, microsecond=0)

    with factory() as db:
        repo = db.scalar(select(Repository))
        first = intel.collect_repository(db, gh.client, SETTINGS, repo, morning)
        first_commits = first.data["observations"]["commits"]["count_window"]
        first_pass = list(log)
        log.clear()
        second = intel.collect_repository(db, gh.client, SETTINGS, repo, morning + timedelta(hours=5))  # same day, hours later
        second_commits = second.data["observations"]["commits"]["count_window"]

    assert {kind for kind, _ in first_pass} == {"200"} and len(first_pass) >= 7
    assert {kind for kind, _ in log} == {"304"}, log  # every request was answered "not modified"
    assert not any(kind == "200" for kind, _ in log)
    assert len(since_values) == 2 and since_values[0] == since_values[1]  # the window start is stable within a day
    assert first_commits == second_commits == 30  # identical result from cached bodies


# ---------------------------------------------------------------- rate limits: no retry loops, fail fast, partial results


@pytest.fixture
def env(client, db_session):
    gh = FakeGitHub({})
    client.app.dependency_overrides[get_settings] = lambda: SETTINGS
    client.app.dependency_overrides[get_github_client] = lambda: gh.client
    return gh


def limited():
    reset = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 3600)}
    return status(403, {"message": "API rate limit exceeded"}, **reset)


def test_rate_limit_is_reported_once_then_later_requests_fail_fast_without_calling_github(client, db_session, env):
    headers, _ = make_user(db_session, 1, "alice")
    env.routes["/repos/o/r"] = limited()
    first = client.get("/api/v1/repositories/o/r", headers=headers)
    assert first.status_code == 429 and int(first.headers["retry-after"]) >= 3600
    assert env.calls["/repos/o/r"] == 1
    for _ in range(3):  # a user hammering the page does not hammer GitHub
        again = client.get("/api/v1/repositories/o/r", headers=headers)
        assert again.status_code == 429 and int(again.headers["retry-after"]) >= 3500
    assert env.total == 1
    assert env.script.sleeps == []  # no waiting, no retry loop


def test_readiness_during_a_rate_limit_returns_partial_results_not_an_error(client, db_session, env):
    headers, _ = make_user(db_session, 1, "alice")
    repo = seed_repo(db_session, "o", "r", "Python")
    seed_issue(db_session, repo, 9, "Parser crashes", D.GOOD_BODY, labels=("good first issue",))
    env.routes["/repos/o/r/issues/9/timeline"] = limited()
    for p in ("commits", "releases", "issues", "pulls", "issues/comments"):
        env.routes[f"/repos/o/r/{p}"] = limited()
    r = client.get("/api/v1/issues/o/r/9/readiness", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["upstream_errors"]["linked_pull_requests"] == "github_rate_limited"
    assert body["report"]["signals"]["issue_description"]["status"] == "known"  # locally computable signals still work
    assert body["report"]["signals"]["linked_pull_requests"]["status"] == "unknown"
    assert env.script.sleeps == [] and env.total <= 3  # it stopped asking GitHub


# ---------------------------------------------------------------- saved-issue lookup


LOOKUP = "/api/v1/saved-issues/lookup/o/r/{n}"


def test_saved_lookup_reflects_only_the_callers_saves_and_never_calls_github(client, db_session, env):
    a, _ = make_user(db_session, 1, "alice")
    b, _ = make_user(db_session, 2, "bob")
    repo = seed_repo(db_session, "o", "r", "Python")
    seed_issue(db_session, repo, 1, "one", "x" * 100)
    seed_issue(db_session, repo, 2, "two", "x" * 100)
    assert client.get(LOOKUP.format(n=1), headers=a).json() == {"saved": False, "saved_issue": None}
    saved = client.post("/api/v1/saved-issues", json={"owner": "o", "repo": "r", "number": 1}, headers=a).json()
    found = client.get(LOOKUP.format(n=1), headers=a).json()
    assert found["saved"] is True and found["saved_issue"]["id"] == saved["id"] and found["saved_issue"]["number"] == 1
    assert client.get(LOOKUP.format(n=2), headers=a).json()["saved"] is False
    assert client.get(LOOKUP.format(n=1), headers=b).json()["saved"] is False  # bob cannot see alice's saves
    client.delete(f"/api/v1/saved-issues/{saved['id']}", headers=a)
    assert client.get(LOOKUP.format(n=1), headers=a).json()["saved"] is False
    assert env.total == 0


def test_saved_lookup_for_unknown_issue_and_validation(client, db_session, env):
    a, _ = make_user(db_session, 1, "alice")
    assert client.get("/api/v1/saved-issues/lookup/x/y/5", headers=a).json() == {"saved": False, "saved_issue": None}
    assert client.get(LOOKUP.format(n=1)).status_code == 401
    for bad in ("0", "abc", "-1"):
        assert client.get(LOOKUP.format(n=bad), headers=a).status_code == 422
    assert env.total == 0
