"""POST /matches/search tests. GitHub is mocked, DB is in-memory SQLite.
Passing these tests shows the endpoint follows its documented rules, not that rankings are accurate."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.api.deps import get_github_client, get_scorer
from app.core.config import Settings, get_settings
from app.core.security import issue_token
from app.db.models import Issue, Repository, User
from app.recommend.types import ComponentScore, ModelInfo, ScoredIssue
from tests.github_mock import NOW, REPO, FakeGitHub, issue as gh_issue, ok, status

URL = "/api/v1/matches/search"
from tests.seed import PROFILE, SETTINGS, ago, make_user, seed_issue, seed_repo  # noqa: E402


@pytest.fixture
def env(client, db_session):
    gh = FakeGitHub({})
    client.app.dependency_overrides[get_settings] = lambda: SETTINGS
    client.app.dependency_overrides[get_github_client] = lambda: gh.client
    return gh


@pytest.fixture
def alice(db_session, env):
    return make_user(db_session, 1, "alice")[0]


@pytest.fixture
def alice_with_profile(client, alice):
    assert client.post("/api/v1/profile", json=PROFILE, headers=alice).status_code == 201
    return alice


def search(client, headers, **body):
    return client.post(URL, json=body, headers=headers)


@pytest.fixture
def corpus(db_session):
    py = seed_repo(db_session, "acme", "ml-api", "Python", topics=("machine-learning", "fastapi"))
    rs = seed_repo(db_session, "acme", "rusty", "Rust", pushed_days=3)
    seed_issue(db_session, py, 1, "FastAPI model loader fails", "Machine learning pipeline crashes. " + "x" * 100,
               labels=("good first issue",))
    seed_issue(db_session, py, 2, "Docs typo", "y" * 100)
    seed_issue(db_session, rs, 3, "Borrow checker error in parser", "z" * 100, labels=("hard",))
    return py, rs


# ---------------------------------------------------------------- core behaviour


def test_ranks_matching_issue_first_with_explanations(client, alice_with_profile, corpus):
    r = search(client, alice_with_profile)
    assert r.status_code == 200
    body = r.json()
    assert [(i["repository"]["full_name"], i["issue"]["number"]) for i in body["items"]] == [
        ("acme/ml-api", 1), ("acme/ml-api", 2), ("acme/rusty", 3)]
    top = body["items"][0]
    assert top["rank"] == 1 and top["issue"]["html_url"] == "https://github.com/acme/ml-api/issues/1"
    assert top["components"]["skill_compatibility"]["score"] > body["items"][2]["components"]["skill_compatibility"]["score"]
    assert top["components"]["interest_similarity"]["score"] == 100.0
    assert any("machine learning" in x for x in top["reasons"])
    assert body["items"][2]["missing_skills"] == ["rust"]  # genuine mismatch reported, not hidden
    assert body["items"][2]["components"]["skill_compatibility"]["score"] == 0.0  # known-low, not null


def test_labels_response_declares_heuristic_baseline(client, alice_with_profile, corpus):
    body = search(client, alice_with_profile).json()
    m = body["model"]
    assert (m["name"], m["kind"], m["is_ml_prediction"]) == ("heuristic_baseline", "heuristic", False)
    assert "not an ML prediction" in m["score_note"] and "probabilities" in m["score_note"]
    assert body["weights"] == {"skill_compatibility": 0.35, "interest_similarity": 0.25,
                               "difficulty_suitability": 0.2, "repository_activity": 0.15,
                               "contribution_readiness": 0.05}


def test_final_score_equals_weighted_sum_of_effective_components(client, alice_with_profile, corpus):
    for item in search(client, alice_with_profile).json()["items"]:
        total = sum(c["weight"] * c["effective_score"] for c in item["components"].values())
        assert item["final_score"] == pytest.approx(total, abs=1e-3)
        assert 0 <= item["final_score"] <= 100


def test_not_fabricating_difficulty_or_labels(client, alice_with_profile, corpus):
    items = {i["issue"]["number"]: i for i in search(client, alice_with_profile).json()["items"]}
    d2 = items[2]["components"]["difficulty_suitability"]  # issue 2 has no difficulty label
    assert d2["score"] is None and d2["status"] == "unknown" and d2["evidence"] == []
    assert items[2]["components"]["difficulty_suitability"]["effective_score"] == 25.0
    d1 = items[1]["components"]["difficulty_suitability"]
    assert d1["score"] == 70.0 and "noisy" in d1["evidence"][0]


def test_deterministic_ranking_and_tie_breaking(client, alice_with_profile, db_session):
    for owner, name in (("zeta", "lib"), ("alpha", "lib"), ("alpha", "app")):
        repo = seed_repo(db_session, owner, name, "Python")
        seed_issue(db_session, repo, 20, "same", "w" * 100)
        seed_issue(db_session, repo, 10, "same", "w" * 100)
    first = search(client, alice_with_profile).json()["items"]
    keys = [(i["repository"]["full_name"], i["issue"]["number"]) for i in first]
    assert keys == [("alpha/app", 10), ("alpha/app", 20), ("alpha/lib", 10), ("alpha/lib", 20),
                    ("zeta/lib", 10), ("zeta/lib", 20)]
    assert len({i["final_score"] for i in first}) == 1  # genuinely tied scores
    assert search(client, alice_with_profile).json()["items"] == first


# ---------------------------------------------------------------- missing data


def test_no_profile_is_insufficient_not_a_perfect_match(client, alice, corpus):
    body = search(client, alice).json()
    assert client.get("/api/v1/profile", headers=alice).status_code == 404  # search did not create a profile
    assert body["ranking_reliability"]["status"] == "insufficient"
    assert "no profile" in body["ranking_reliability"]["reasons"][0].lower()
    for item in body["items"]:
        assert item["insufficient_information"] is True
        sk = item["components"]["skill_compatibility"]
        assert sk["score"] is None and sk["status"] == "unknown" and sk["effective_score"] == 25.0
        assert item["final_score"] < 50
        assert item["warnings"][0].startswith("Insufficient information")


def test_profile_without_skills_or_interests_is_insufficient(client, alice, corpus):
    client.post("/api/v1/profile", json={}, headers=alice)
    body = search(client, alice).json()
    assert body["ranking_reliability"]["status"] == "insufficient"
    assert "no skills or interests" in body["ranking_reliability"]["reasons"][0]


def test_profile_missing_interests_flags_unknown_component_only(client, alice, corpus):
    client.post("/api/v1/profile", json={"skills": [{"name": "python", "proficiency": 0.9}]}, headers=alice)
    top = search(client, alice).json()["items"][0]
    assert top["components"]["interest_similarity"]["score"] is None
    assert top["components"]["skill_compatibility"]["score"] is not None
    assert top["components"]["difficulty_suitability"]["score"] is None  # no target difficulty either


def test_unknown_activity_and_stale_data_are_marked_not_invented(client, alice_with_profile, db_session):
    nopush = seed_repo(db_session, "a", "nopush", pushed_days=None)
    seed_issue(db_session, nopush, 1, "x", "q" * 100)
    stale = seed_repo(db_session, "a", "stale", fetched_days=30)
    seed_issue(db_session, stale, 1, "x", "q" * 100, fetched_days=30)
    items = {i["repository"]["name"]: i for i in search(client, alice_with_profile).json()["items"]}
    act = items["nopush"]["components"]["repository_activity"]
    assert act["score"] is None and act["status"] == "unknown" and "no last-push date" in act["warnings"][0].lower()
    s_act = items["stale"]["components"]["repository_activity"]
    s_ready = items["stale"]["components"]["contribution_readiness"]
    assert s_act["status"] == "stale" and s_act["score"] is None and "stale" in s_act["warnings"][0]
    assert s_ready["status"] == "stale" and s_ready["score"] is None
    assert any("stale" in w for w in items["stale"]["warnings"])


def test_readiness_is_never_reported_as_certain(client, alice_with_profile, corpus):
    for item in search(client, alice_with_profile).json()["items"]:
        r = item["components"]["contribution_readiness"]
        assert r["confidence"] <= 0.5 and r["effective_score"] < 100
        assert any("have not been checked" in w for w in r["warnings"])


def test_empty_dataset(client, alice_with_profile):
    body = search(client, alice_with_profile).json()
    assert body["items"] == [] and body["total"] == 0
    assert body["ranking_reliability"]["status"] == "no_candidates"
    assert "repositories" in body["ranking_reliability"]["reasons"][0]


def test_closed_issues_and_archived_repos_are_excluded_by_default(client, alice_with_profile, db_session):
    repo = seed_repo(db_session)
    seed_issue(db_session, repo, 1, "closed one", state="closed")
    seed_issue(db_session, repo, 2, "open one")
    old = seed_repo(db_session, "o", "old", archived=True)
    seed_issue(db_session, old, 1, "in archived")
    assert [i["issue"]["title"] for i in search(client, alice_with_profile).json()["items"]] == ["open one"]
    with_arch = search(client, alice_with_profile, include_archived=True).json()["items"]
    archived = next(i for i in with_arch if i["repository"]["name"] == "old")
    assert archived["components"]["repository_activity"]["score"] == 0.0


# ---------------------------------------------------------------- filters + pagination


def test_pagination_is_consistent(client, alice_with_profile, db_session):
    repo = seed_repo(db_session)
    for n in range(1, 6):
        seed_issue(db_session, repo, n, f"i{n}", "p" * 100)
    pages = [search(client, alice_with_profile, limit=2, offset=o).json() for o in (0, 2, 4)]
    assert [len(p["items"]) for p in pages] == [2, 2, 1]
    assert all(p["total"] == 5 for p in pages)
    flat = [i for p in pages for i in p["items"]]
    assert [i["rank"] for i in flat] == [1, 2, 3, 4, 5]
    assert [i["issue"]["number"] for i in flat] == [1, 2, 3, 4, 5]
    assert search(client, alice_with_profile, limit=2, offset=10).json()["items"] == []


def test_language_and_topic_filters(client, alice_with_profile, corpus):
    only_rust = search(client, alice_with_profile, language="rust").json()
    assert [i["repository"]["name"] for i in only_rust["items"]] == ["rusty"]
    topic = search(client, alice_with_profile, topic="FastAPI").json()
    assert {i["repository"]["name"] for i in topic["items"]} == {"ml-api"}
    assert search(client, alice_with_profile, topic="nonexistent").json()["total"] == 0


def test_max_inactive_days_filter_excludes_unknown_activity(client, alice_with_profile, db_session):
    fresh = seed_repo(db_session, "a", "fresh", pushed_days=2)
    old = seed_repo(db_session, "a", "old", pushed_days=100)
    unknown = seed_repo(db_session, "a", "unknown", pushed_days=None)
    for r in (fresh, old, unknown):
        seed_issue(db_session, r, 1, "x")
    names = [i["repository"]["name"] for i in search(client, alice_with_profile, max_inactive_days=30).json()["items"]]
    assert names == ["fresh"]


def test_request_difficulty_overrides_profile_for_that_search_only(client, alice_with_profile, corpus):
    def diff(**kw):
        items = {i["issue"]["number"]: i for i in search(client, alice_with_profile, **kw).json()["items"]}
        return items[1]["components"]["difficulty_suitability"]["score"]
    assert diff() == 70.0  # profile target 2 vs label difficulty 1
    assert diff(preferred_difficulty=1) == 100.0
    assert diff() == 70.0


@pytest.mark.parametrize("body", [
    {"limit": 0}, {"limit": 51}, {"offset": -1}, {"preferred_difficulty": 0}, {"preferred_difficulty": 6},
    {"max_inactive_days": 0}, {"language": ""}, {"repositories": ["noslash"]}, {"repositories": ["a/b/c"]},
    {"repositories": ["../x"]}, {"repositories": ["a/b", "A/B"]}, {"repositories": [f"o/r{i}" for i in range(6)]},
    {"user_id": 2}, {"unknown": 1}, {"limit": "ten"},
])
def test_invalid_requests_rejected_without_github_calls(client, alice_with_profile, env, body):
    r = client.post(URL, json=body, headers=alice_with_profile)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert env.total == 0


def test_search_without_repositories_never_calls_github(client, alice_with_profile, corpus, env):
    search(client, alice_with_profile)
    assert env.total == 0


# ---------------------------------------------------------------- auth + isolation


def test_authentication_required(client, env, corpus):
    assert client.post(URL, json={}).status_code == 401
    assert client.post(URL, json={}, headers={"Authorization": "Bearer junk"}).status_code == 401
    assert env.total == 0


def test_each_user_is_scored_from_their_own_profile(client, db_session, env, corpus):
    a, _ = make_user(db_session, 1, "alice")
    b, _ = make_user(db_session, 2, "bob")
    client.post("/api/v1/profile", json=PROFILE, headers=a)
    client.post("/api/v1/profile", json={"skills": [{"name": "rust", "proficiency": 1.0}]}, headers=b)
    ra = search(client, a).json()["items"]
    rb = search(client, b).json()["items"]
    assert ra[0]["repository"]["name"] == "ml-api" and rb[0]["repository"]["name"] == "rusty"
    assert ra[0]["components"]["skill_compatibility"]["score"] != rb[0]["components"]["skill_compatibility"]["score"]


def test_unauthenticated_user_id_cannot_borrow_another_profile(client, db_session, env, corpus):
    a, ua = make_user(db_session, 1, "alice")
    b, ub = make_user(db_session, 2, "bob")  # bob has no profile
    client.post("/api/v1/profile", json=PROFILE, headers=a)
    assert client.post(URL, json={"user_id": ua.id}, headers=b).status_code == 422
    r = client.post(f"{URL}?user_id={ua.id}", json={}, headers={**b, "X-User-Id": str(ua.id)})
    assert r.json()["ranking_reliability"]["status"] == "insufficient"
    assert all(i["components"]["skill_compatibility"]["score"] is None for i in r.json()["items"])


# ---------------------------------------------------------------- upstream behaviour


def requested_routes(**over):
    return {"/repos/o/r": ok(REPO), "/repos/o/r/issues": ok([gh_issue(1, body="python " + "z" * 100)]), **over}


def test_requested_repository_is_synced_then_served_from_storage(client, alice_with_profile, env):
    env.routes.update(requested_routes())
    r = search(client, alice_with_profile, repositories=["o/r"])
    assert r.status_code == 200 and r.json()["items"][0]["repository"]["full_name"] == "o/r"
    calls = env.total
    assert calls > 0
    search(client, alice_with_profile, repositories=["o/r"])
    assert env.total == calls  # fresh data: no further GitHub calls


def test_upstream_failure_with_no_stored_data_returns_mapped_error(client, alice_with_profile, env):
    env.routes.update({"/repos/o/r": status(503)})
    r = search(client, alice_with_profile, repositories=["o/r"])
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_unavailable"


def test_rate_limit_with_no_data_returns_429_with_retry_after(client, alice_with_profile, env):
    reset = int(NOW) + 3600
    env.routes.update({"/repos/o/r": status(403, {"message": "API rate limit exceeded"},
                                            **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})})
    r = search(client, alice_with_profile, repositories=["o/r"])
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 3600


def test_upstream_failure_falls_back_to_stale_stored_data(client, alice_with_profile, env, db_session):
    repo = seed_repo(db_session, "o", "r", fetched_days=30)
    seed_issue(db_session, repo, 1, "old data", "k" * 100, fetched_days=30)
    env.routes.update({"/repos/o/r": httpx.ReadTimeout("slow")})
    body = search(client, alice_with_profile, repositories=["o/r"]).json()
    assert len(body["items"]) == 1
    assert body["upstream_errors"] == [{"repository": "o/r", "code": "github_timeout",
                                        "message": "GitHub did not respond in time"}]
    assert body["ranking_reliability"]["status"] == "limited"
    assert body["items"][0]["components"]["repository_activity"]["status"] == "stale"


def test_partial_failure_still_returns_working_repositories(client, alice_with_profile, env):
    env.routes.update(requested_routes())  # o/r works; o/bad is missing on GitHub -> 404
    body = search(client, alice_with_profile, repositories=["o/r", "o/bad"]).json()
    assert [i["repository"]["name"] for i in body["items"]] == ["r"]
    assert body["upstream_errors"][0]["repository"] == "o/bad"
    assert body["upstream_errors"][0]["code"] == "repository_not_found"


def test_private_requested_repository_is_never_scored(client, alice_with_profile, env, db_session):
    env.routes.update({"/repos/o/r": ok({**REPO, "private": True}), "/repos/o/r/issues": ok([gh_issue(1)])})
    r = search(client, alice_with_profile, repositories=["o/r"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "repository_not_found"
    assert db_session.query(Repository).count() == 0 and db_session.query(Issue).count() == 0


# ---------------------------------------------------------------- scorer interchangeability


class ReverseScorer:
    """Stand-in for a future ML scorer: proves the endpoint depends only on the Scorer interface."""

    info = ModelInfo("fake_model", "t0", "ml", True, "test double")

    def score(self, user, issue, now):
        comps = {"skill_compatibility": ComponentScore("skill_compatibility", 1.0, 50.0, 1.0, "known",
                                                      effective_score=50.0)}
        return ScoredIssue(final_score=float(issue.number), coverage=1.0, insufficient_information=False,
                           components=comps, reasons=(), warnings=(), missing_skills=(), model=self.info)


def test_endpoint_works_with_any_scorer_implementation(client, alice_with_profile, corpus):
    client.app.dependency_overrides[get_scorer] = lambda: ReverseScorer()
    body = search(client, alice_with_profile).json()
    assert body["model"]["name"] == "fake_model" and body["model"]["is_ml_prediction"] is True
    assert [i["issue"]["number"] for i in body["items"]] == [3, 2, 1]  # ordered by that scorer's scores


def test_sparse_but_perfect_match_is_not_ranked_below_a_known_mismatch(client, db_session, env, corpus):
    """Regression: 'insufficient information' must not force an item below better-scored ones."""
    b, _ = make_user(db_session, 2, "bob")
    client.post("/api/v1/profile", json={"skills": [{"name": "rust", "proficiency": 1.0}]}, headers=b)
    items = search(client, b).json()["items"]
    assert items[0]["repository"]["name"] == "rusty"
    assert [i["final_score"] for i in items] == sorted((i["final_score"] for i in items), reverse=True)
