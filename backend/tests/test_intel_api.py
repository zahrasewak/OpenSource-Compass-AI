"""Repository health + issue readiness endpoints (mocked GitHub, SQLite)."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.api.deps import get_github_client
from app.core.config import get_settings
from app.db.models import Issue, IssueFeature, Repository, RepositorySnapshot
from tests import intel_data as D
from tests.github_mock import NOW, REPO, FakeGitHub, ok, status
from tests.seed import SETTINGS, make_user

HEALTH = "/api/v1/repositories/o/r/health"
READINESS = "/api/v1/issues/o/r/9/readiness"


@pytest.fixture
def env(client, db_session):
    gh = FakeGitHub({})
    client.app.dependency_overrides[get_settings] = lambda: SETTINGS
    client.app.dependency_overrides[get_github_client] = lambda: gh.client
    return gh


@pytest.fixture
def auth(db_session, env):
    return make_user(db_session, 1, "alice")[0]


def age_snapshots(db, **delta):
    for s in db.scalars(select(RepositorySnapshot)):
        s.collected_at = D.now() - timedelta(**delta)
    db.commit()


def age_linked(db, **delta):
    for f in db.scalars(select(IssueFeature)):
        f.created_at = D.now() - timedelta(**delta)
    db.commit()


# ================================================================ repository health


def test_health_for_active_repository(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    r = client.get(HEALTH, headers=auth)
    assert r.status_code == 200
    b = r.json()
    assert b["repository"]["full_name"] == "o/r"
    assert b["scoring"]["is_ml_prediction"] is False and "Not an ML prediction" in b["scoring"]["note"]
    rep = b["report"]
    assert rep["kind"] == "repository_health" and rep["label"] == "healthy" and rep["score"] >= 70
    assert {s["status"] for n, s in rep["signals"].items() if n != "release_cadence"} == {"known"}
    assert rep["signals"]["maintainer_responsiveness"]["score"] == 100.0
    assert set(rep["group_scores"]) == {"activity", "responsiveness", "maintenance"}
    f = b["freshness"]
    assert f["complete"] is True and f["head_sha"] == D.HEAD and f["errors"] == {} and f["stale"] is False
    assert f["window_days"] == 90 and f["collected_at"] and f["age_hours"] < 1
    assert b["observations"]["commits"]["count_window"] == 30
    snap = db_session.scalar(select(RepositorySnapshot))
    assert (snap.analysis_version, snap.commit_sha) == ("intel_v1", D.HEAD) and snap.data["complete"] is True


def test_snapshot_is_reused_and_refresh_is_throttled(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    client.get(HEALTH, headers=auth)
    calls = env.total
    client.get(HEALTH, headers=auth)
    client.get(HEALTH + "?refresh=true", headers=auth)  # younger than the 30s floor: ignored
    assert env.total == calls
    age_snapshots(db_session, seconds=60)
    client.get(HEALTH + "?refresh=true", headers=auth)  # older than the floor: honoured
    assert env.total > calls


def test_expired_snapshot_is_recollected_in_place(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    client.get(HEALTH, headers=auth)
    age_snapshots(db_session, days=2)
    before = env.total
    r = client.get(HEALTH, headers=auth)
    assert env.total > before and r.json()["freshness"]["age_hours"] < 1
    assert db_session.query(RepositorySnapshot).count() == 1  # same commit => same row, refreshed


def test_new_head_commit_creates_a_new_snapshot(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    client.get(HEALTH, headers=auth)
    new_head = "b" * 40
    env.routes["/repos/o/r/commits"] = ok([{**D.commit_payload(0), "sha": new_head}] + [D.commit_payload(1 + 3 * i) for i in range(10)])
    env.routes[f"/repos/o/r/git/trees/{new_head}"] = ok(D.tree_payload(D.GOOD_PATHS))
    age_snapshots(db_session, days=2)
    client.get(HEALTH, headers=auth)
    assert sorted(s.commit_sha for s in db_session.scalars(select(RepositorySnapshot))) == [D.HEAD, new_head]


def test_pagination_is_bounded(client, auth, env):
    env.routes.update(D.healthy_routes())
    link = '<https://api.github.com/repos/o/r/commits?per_page=100&page=2>; rel="next"'
    env.routes["/repos/o/r/commits"] = lambda r: ok([D.commit_payload(1 + 3 * i) for i in range(30)], link=link)
    b = client.get(HEALTH, headers=auth).json()
    assert env.calls["/repos/o/r/commits"] == 3  # IntelConfig.commit_pages
    assert b["observations"]["commits"]["window_count_is_lower_bound"] is True


def test_one_failed_signal_is_unknown_not_low_and_snapshot_is_partial(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    env.routes["/repos/o/r/issues/comments"] = status(503)
    b = client.get(HEALTH, headers=auth).json()
    resp = b["report"]["signals"]["maintainer_responsiveness"]
    assert resp["status"] == "unknown" and resp["score"] is None and resp["effective_score"] == 25.0
    assert b["freshness"]["complete"] is False and b["freshness"]["errors"] == {"comments": "github_unavailable"}
    assert b["observations"]["responses"] == {"status": "unavailable", "error": "github_unavailable"}
    calls = env.total
    client.get(HEALTH, headers=auth)
    assert env.total == calls  # a partial snapshot is still reused briefly
    age_snapshots(db_session, minutes=20)  # ...but only for the short (non-complete) TTL
    client.get(HEALTH, headers=auth)
    assert env.total > calls


def test_rate_limit_mid_collection_stops_further_requests(client, auth, env):
    env.routes.update(D.healthy_routes())
    reset = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 3600)}
    env.routes["/repos/o/r/releases"] = status(403, {"message": "API rate limit exceeded"}, **reset)
    b = client.get(HEALTH, headers=auth).json()
    errors = b["freshness"]["errors"]
    assert errors["releases"] == "github_rate_limited" and errors["issues"] == "skipped_after_rate_limit"
    assert env.calls["/repos/o/r/issues"] == 0 and env.calls["/repos/o/r/pulls"] == 0  # not hammered
    assert b["report"]["signals"]["commit_activity"]["status"] == "known"  # what succeeded is kept
    assert b["report"]["label"] != "healthy"


def test_nothing_retrievable_maps_the_real_error(client, auth, env):
    env.routes.update({"/repos/o/r": ok(REPO)})
    for path in ("commits", "releases", "issues", "pulls", "issues/comments"):
        env.routes[f"/repos/o/r/{path}"] = status(503)
    env.routes[f"/repos/o/r/git/trees/main"] = status(503)
    r = client.get(HEALTH, headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_unavailable"


def test_rate_limited_from_the_start_returns_429_with_retry_after(client, auth, env):
    reset = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 3600)}
    env.routes.update({"/repos/o/r": ok(REPO), "/repos/o/r/commits": status(403, {"message": "API rate limit exceeded"}, **reset)})
    r = client.get(HEALTH, headers=auth)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 3600


def test_failed_refresh_serves_older_stored_analysis(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    first = client.get(HEALTH, headers=auth).json()
    age_snapshots(db_session, days=2)
    for path in ("commits", "releases", "issues", "pulls", "issues/comments"):
        env.routes[f"/repos/o/r/{path}"] = status(503)
    env.routes[f"/repos/o/r/git/trees/{D.HEAD}"] = status(503)
    r = client.get(HEALTH, headers=auth)
    assert r.status_code == 200
    b = r.json()
    assert b["freshness"]["refresh_error"] == "github_unavailable" and b["freshness"]["age_hours"] >= 47
    assert b["report"]["score"] == first["report"]["score"]


def test_analysis_older_than_limit_is_flagged_stale(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    client.get(HEALTH, headers=auth)
    age_snapshots(db_session, days=20)
    for path in ("commits", "releases", "issues", "pulls", "issues/comments"):
        env.routes[f"/repos/o/r/{path}"] = status(503)
    env.routes[f"/repos/o/r/git/trees/{D.HEAD}"] = status(503)
    b = client.get(HEALTH, headers=auth).json()
    assert b["freshness"]["stale"] is True and any("older than the staleness limit" in w for w in b["report"]["warnings"])


def test_archived_repository(client, auth, env):
    env.routes.update(D.healthy_routes())
    env.routes["/repos/o/r"] = ok({**REPO, "archived": True})
    rep = client.get(HEALTH, headers=auth).json()["report"]
    assert rep["label"] == "archived" and rep["score"] == 0.0


def test_missing_file_listing_makes_maintenance_unknown(client, auth, env):
    env.routes.update(D.healthy_routes())
    env.routes[f"/repos/o/r/git/trees/{D.HEAD}"] = status(404)
    b = client.get(HEALTH, headers=auth).json()
    assert b["report"]["signals"]["maintenance_indicators"]["status"] == "unknown"
    assert b["freshness"]["errors"]["tree"] == "github_not_found"


def test_small_repository_with_few_commits_is_not_called_inactive(client, auth, env):
    env.routes.update(D.healthy_routes())
    env.routes["/repos/o/r/commits"] = ok([D.commit_payload(40), D.commit_payload(70)] + [D.commit_payload(150 + 10 * i) for i in range(8)])
    env.routes["/repos/o/r/releases"] = ok([])
    b = client.get(HEALTH, headers=auth).json()
    assert b["observations"]["commits"]["small_repository"] is True
    assert b["report"]["label"] in {"healthy", "moderate", "stable_or_slow"}
    assert any("Small repository" in w for w in b["report"]["signals"]["commit_activity"]["warnings"])


def test_inactive_repository_is_reported_as_such(client, auth, env):
    env.routes.update(D.healthy_routes())
    env.routes["/repos/o/r/commits"] = ok([D.commit_payload(800), D.commit_payload(900)])
    for p in ("releases", "issues", "pulls", "issues/comments"):
        env.routes[f"/repos/o/r/{p}"] = ok([])
    b = client.get(HEALTH, headers=auth).json()
    assert b["report"]["label"] in {"inactive", "low_activity"}
    assert b["report"]["signals"]["commit_activity"]["score"] == 0.0


def test_health_errors_and_validation(client, auth, env):
    env.routes.update({"/repos/o/r": status(404)})
    assert client.get(HEALTH, headers=auth).json()["error"]["code"] == "repository_not_found"
    env.routes["/repos/o/r"] = ok({**REPO, "private": True})
    assert client.get(HEALTH, headers=auth).status_code == 404
    assert client.get(HEALTH).status_code == 401
    assert client.get("/api/v1/repositories/o%20x/r/health", headers=auth).status_code == 422


# ================================================================ issue readiness


def test_readiness_for_well_prepared_issue(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    r = client.get(READINESS, headers=auth)
    assert r.status_code == 200
    b = r.json()
    assert b["issue"]["number"] == 9 and b["scoring"]["is_ml_prediction"] is False
    rep = b["report"]
    assert rep["kind"] == "issue_readiness" and rep["label"] == "well_prepared" and rep["score"] >= 65
    sig = rep["signals"]
    assert sig["contribution_guide"]["score"] == 100 and sig["labels"]["score"] == 100
    assert sig["issue_description"]["score"] == 100 and sig["linked_pull_requests"]["score"] == 100
    assert sig["tests"]["score"] == 100 and any("not executed" in e for e in sig["tests"]["evidence"])
    assert b["repository_docs"]["test_files"]["count"] == 1 and b["repository_docs"]["test_commands_documented"]
    assert b["upstream_errors"] == {} and b["freshness"]["linked_prs_collected_at"]
    assert any("not executed" in n for n in rep["notes"])
    row = db_session.scalar(select(IssueFeature))
    assert row.feature_version == "readiness_v1" and row.structured_features["pull_requests"] == []


@pytest.mark.parametrize("timeline,score,state", [
    ([(50, "open", None)], 25, "open"),
    ([(51, "closed", 3)], 40, "merged"),
    ([(52, "closed", None)], 70, "closed_unmerged"),
    ([(50, "open", None), (52, "closed", None)], 25, "open"),
])
def test_linked_pull_request_outcomes(client, auth, env, timeline, score, state):
    env.routes.update(D.healthy_routes(timeline=timeline))
    b = client.get(READINESS, headers=auth).json()
    assert b["report"]["signals"]["linked_pull_requests"]["score"] == score
    assert state in {p["state"] for p in b["linked_pull_requests"]["pull_requests"]}
    assert "duplicate" not in str(b["report"]["signals"]["linked_pull_requests"]).lower()


def test_cross_repository_and_non_pr_references_are_not_counted(client, auth, env):
    env.routes.update(D.healthy_routes(timeline=[(7, "open", None, "other/x"), (8, "open", None, "o/r", False)]))
    b = client.get(READINESS, headers=auth).json()
    assert b["linked_pull_requests"]["pull_requests"] == [] and b["linked_pull_requests"]["other_repository_references"] == 1
    assert b["report"]["signals"]["linked_pull_requests"]["score"] == 100


def test_linked_pr_data_reused_then_refetched(client, auth, env, db_session):
    env.routes.update(D.healthy_routes())
    client.get(READINESS, headers=auth)
    calls = env.calls["/repos/o/r/issues/9/timeline"]
    client.get(READINESS, headers=auth)
    client.get(READINESS + "?refresh=true", headers=auth)
    assert env.calls["/repos/o/r/issues/9/timeline"] == calls
    age_linked(db_session, hours=2)  # past the 1h reuse window
    env.routes["/repos/o/r/issues/9/timeline"] = ok(D.timeline_payload([(50, "open", None)]))
    b = client.get(READINESS, headers=auth).json()
    assert env.calls["/repos/o/r/issues/9/timeline"] == calls + 1
    assert b["report"]["signals"]["linked_pull_requests"]["score"] == 25
    assert db_session.query(IssueFeature).count() == 1  # updated in place, not duplicated


def test_timeline_failure_leaves_linked_prs_unknown_but_returns_the_rest(client, auth, env):
    env.routes.update(D.healthy_routes())
    env.routes["/repos/o/r/issues/9/timeline"] = status(503)
    r = client.get(READINESS, headers=auth)
    assert r.status_code == 200
    b = r.json()
    sig = b["report"]["signals"]["linked_pull_requests"]
    assert sig["status"] == "unknown" and sig["score"] is None
    assert b["upstream_errors"] == {"linked_pull_requests": "github_unavailable"} and b["linked_pull_requests"] is None
    assert b["report"]["signals"]["contribution_guide"]["status"] == "known"


def test_repository_analysis_failure_leaves_docs_unknown(client, auth, env):
    env.routes.update(D.healthy_routes())
    for p in ("commits", "releases", "issues", "pulls", "issues/comments"):
        env.routes[f"/repos/o/r/{p}"] = status(503)
    env.routes[f"/repos/o/r/git/trees/{D.HEAD}"] = status(503)
    env.routes["/repos/o/r/git/trees/main"] = status(503)
    b = client.get(READINESS, headers=auth).json()
    assert "repository_analysis" in b["upstream_errors"]
    sigs = b["report"]["signals"]
    assert sigs["contribution_guide"]["status"] == "unknown" and sigs["tests"]["status"] == "unknown"
    assert b["repository_docs"] is None


def test_missing_guide_and_tests_are_genuine_lows(client, auth, env):
    env.routes.update(D.healthy_routes(paths=["README.md", "src/app.py"]))
    env.routes["/repos/o/r/contents/README.md"] = ok(D.file_payload("Just a readme."))
    sig = client.get(READINESS, headers=auth).json()["report"]["signals"]
    assert sig["contribution_guide"]["status"] == "known" and sig["contribution_guide"]["score"] == 15
    assert sig["tests"]["status"] == "known" and sig["tests"]["score"] == 10


def test_truncated_file_listing_does_not_conclude_absence(client, auth, env):
    env.routes.update(D.healthy_routes(paths=["README.md"]))
    env.routes[f"/repos/o/r/git/trees/{D.HEAD}"] = ok(D.tree_payload(["README.md"], truncated=True))
    env.routes["/repos/o/r/contents/README.md"] = ok(D.file_payload("Just a readme."))
    sig = client.get(READINESS, headers=auth).json()["report"]["signals"]
    assert sig["contribution_guide"]["status"] == "unknown" and sig["tests"]["status"] == "unknown"


def test_incomplete_issue_description_is_reported(client, auth, env):
    env.routes.update(D.healthy_routes(target_body="it crashes", target_labels=()))
    b = client.get(READINESS, headers=auth).json()
    desc = b["report"]["signals"]["issue_description"]
    assert desc["score"] == 0.0 and any("reproduction steps" in w for w in desc["warnings"])
    assert b["report"]["signals"]["labels"]["status"] == "unknown"


def test_closed_issue_is_not_available(client, auth, env):
    env.routes.update(D.healthy_routes())
    env.routes["/repos/o/r/issues/9"] = ok(D.issue_payload(9, 20, closed_days=1))
    rep = client.get(READINESS, headers=auth).json()["report"]
    assert rep["label"] == "not_available" and rep["score"] == 0.0


def test_readiness_errors_and_validation(client, auth, env):
    env.routes.update({"/repos/o/r": ok(REPO)})
    assert client.get(READINESS, headers=auth).json()["error"]["code"] == "issue_not_found"
    env.routes["/repos/o/r/issues/9"] = ok(D.issue_payload(9, 5, pr=True))
    assert client.get(READINESS, headers=auth).json()["error"]["code"] == "not_an_issue"
    assert client.get(READINESS).status_code == 401
    for bad in ("0", "abc", "-1"):
        assert client.get(f"/api/v1/issues/o/r/{bad}/readiness", headers=auth).status_code == 422


def test_intelligence_data_is_shared_public_data_with_no_user_specific_fields(client, db_session, env):
    env.routes.update(D.healthy_routes())
    a = make_user(db_session, 1, "alice")[0]
    b = make_user(db_session, 2, "bob")[0]
    ra, rb = client.get(READINESS, headers=a), client.get(READINESS, headers=b)
    assert ra.json() == rb.json()
    text = ra.text.lower()
    assert "skill" not in text and "proficiency" not in text and "recommend" not in text
    assert db_session.query(RepositorySnapshot).count() == 1  # collected once, shared
