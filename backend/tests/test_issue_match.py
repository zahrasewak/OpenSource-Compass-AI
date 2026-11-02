"""POST /issues/{owner}/{repo}/{number}/match: the single-issue heuristic match the browser extension shows."""

import pytest
from sqlalchemy import select

from app.api.deps import get_github_client
from app.core.config import get_settings
from app.db.models import Issue, Recommendation
from tests import intel_data as D
from tests.github_mock import REPO, FakeGitHub, ok, status
from tests.seed import PROFILE, SETTINGS, make_user, seed_issue, seed_repo

URL = "/api/v1/issues/o/r/{n}/match"
BODY = "Parser crashes when loading data. " + "x" * 120


@pytest.fixture
def env(client, db_session):
    gh = FakeGitHub({"/repos/o/r": ok(REPO)})
    client.app.dependency_overrides[get_settings] = lambda: SETTINGS
    client.app.dependency_overrides[get_github_client] = lambda: gh.client
    return gh


@pytest.fixture
def alice(client, db_session, env):
    headers, _ = make_user(db_session, 1, "alice")
    client.post("/api/v1/profile", json=PROFILE, headers=headers)
    return headers


def add_remote_issue(env, n=1, **kw):
    env.routes[f"/repos/o/r/issues/{n}"] = ok(D.issue_payload(n, 5, body=kw.pop("body", BODY), title=kw.pop("title", "Parser crashes on load"), **kw))


def test_scores_one_issue_and_labels_it_a_heuristic(client, alice, env):
    add_remote_issue(env)
    r = client.post(URL.format(n=1), headers=alice)
    assert r.status_code == 200
    b = r.json()
    assert (b["model"]["name"], b["model"]["version"], b["model"]["kind"], b["model"]["is_ml_prediction"]) == ("heuristic_baseline", "v1", "heuristic", False)
    assert "not an ML prediction" in b["model"]["score_note"]
    m = b["match"]
    assert m["repository"]["full_name"] == "o/r" and m["issue"]["number"] == 1 and 0 <= m["final_score"] <= 100
    assert set(m["components"]) == {"skill_compatibility", "interest_similarity", "difficulty_suitability", "repository_activity", "contribution_readiness"}
    assert "rank" not in m  # a single issue has no position in a ranking
    assert b["weights"]["skill_compatibility"] == 0.35 and b["ranking_reliability"]["status"] in {"ok", "limited"}


def test_persisted_by_default_and_idempotent(client, alice, env, db_session):
    add_remote_issue(env)
    first = client.post(URL.format(n=1), headers=alice).json()["match"]
    second = client.post(URL.format(n=1), headers=alice).json()["match"]
    assert first["match_id"] and first["match_id"] == second["match_id"]
    assert db_session.query(Recommendation).count() == 1
    assert client.get(f"/api/v1/matches/{first['match_id']}", headers=alice).status_code == 200


def test_dry_run_stores_nothing(client, alice, env, db_session):
    add_remote_issue(env)
    m = client.post(URL.format(n=1), json={"persist": False}, headers=alice).json()["match"]
    assert m["match_id"] is None and m["generated_at"] is None and db_session.query(Recommendation).count() == 0


def test_same_score_as_the_search_endpoint(client, alice, env, db_session):
    repo = seed_repo(db_session, "o", "r", "Python")
    seed_issue(db_session, repo, 1, "Parser crashes on load", BODY)
    search = client.post("/api/v1/matches/search", json={"persist": False}, headers=alice).json()["items"][0]
    single = client.post(URL.format(n=1), json={"persist": False}, headers=alice).json()["match"]
    assert single["final_score"] == search["final_score"] and single["components"] == search["components"]
    assert single["reasons"] == search["reasons"] and single["warnings"] == search["warnings"]


def test_no_profile_is_insufficient_and_never_a_perfect_skill_match(client, db_session, env):
    headers, _ = make_user(db_session, 5, "noprofile")
    add_remote_issue(env)
    b = client.post(URL.format(n=1), headers=headers).json()
    assert b["ranking_reliability"]["status"] == "insufficient" and "no profile" in b["ranking_reliability"]["reasons"][0].lower()
    skill = b["match"]["components"]["skill_compatibility"]
    assert skill["score"] is None and skill["status"] == "unknown" and b["match"]["insufficient_information"] is True
    assert client.get("/api/v1/profile", headers=headers).status_code == 404  # scoring did not create a profile


def test_each_user_is_scored_from_their_own_profile(client, db_session, env):
    a, _ = make_user(db_session, 1, "alice")
    b, _ = make_user(db_session, 2, "bob")
    client.post("/api/v1/profile", json=PROFILE, headers=a)
    client.post("/api/v1/profile", json={"skills": [{"name": "rust", "proficiency": 1.0}]}, headers=b)
    add_remote_issue(env)
    sa = client.post(URL.format(n=1), json={"persist": False}, headers=a).json()["match"]["components"]["skill_compatibility"]["score"]
    sb = client.post(URL.format(n=1), json={"persist": False}, headers=b).json()["match"]["components"]["skill_compatibility"]["score"]
    assert sa != sb
    mine = client.post(URL.format(n=1), headers=a).json()["match"]["match_id"]
    assert client.get(f"/api/v1/matches/{mine}", headers=b).status_code == 404


def test_v2_is_opt_in_and_uses_stored_intelligence_without_new_github_calls(client, alice, env, db_session):
    repo = seed_repo(db_session, "o", "r", "Python")
    seed_issue(db_session, repo, 1, "Parser crashes on load", BODY)
    D.store_snapshot(db_session, repo, "healthy")
    calls = env.total
    v2 = client.post(URL.format(n=1), json={"scorer": "heuristic_v2", "persist": False}, headers=alice).json()
    assert v2["model"]["version"] == "v2" and "repository_health" in v2["match"]["components"]
    assert v2["match"]["components"]["repository_health"]["status"] == "known"
    assert env.total == calls  # nothing fetched: the issue was already stored
    assert client.post(URL.format(n=1), json={"scorer": "nope"}, headers=alice).status_code == 422


def test_unknown_issue_is_fetched_once_then_served_from_storage(client, alice, env):
    add_remote_issue(env)
    client.post(URL.format(n=1), headers=alice)
    first = env.total
    client.post(URL.format(n=1), headers=alice)
    assert env.total == first and first > 0


def test_missing_issue_pull_request_and_upstream_errors(client, alice, env):
    assert client.post(URL.format(n=9), headers=alice).json()["error"]["code"] == "issue_not_found"
    env.routes["/repos/o/r/issues/5"] = ok(D.issue_payload(5, 3, pr=True))
    assert client.post(URL.format(n=5), headers=alice).json()["error"]["code"] == "not_an_issue"


def test_github_outage_maps_to_502(client, alice, env):
    env.routes["/repos/x/y"] = status(503)  # a repository nothing is stored for, with GitHub failing
    r = client.post("/api/v1/issues/x/y/1/match", headers=alice)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_unavailable"


def test_auth_and_validation(client, alice, env):
    assert client.post(URL.format(n=1)).status_code == 401
    assert client.post(URL.format(n=1), headers={"Authorization": "Bearer junk"}).status_code == 401
    for bad in ("0", "abc", "-1"):
        assert client.post(URL.format(n=bad), headers=alice).status_code == 422
    assert client.post(URL.format(n=1), json={"user_id": 2}, headers=alice).status_code == 422  # identity is never client-supplied
