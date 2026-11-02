"""heuristic_v2: repository health + readiness inside the existing recommendation pipeline.
These tests show the documented mechanics and ranking effects; they do NOT show v2 is better than v1."""

import hashlib
import inspect

import pytest
from sqlalchemy import select

from app.api.deps import get_github_client
from app.core.config import get_settings
from app.db.models import Recommendation
from app.intelligence import activity, config as intel_config, readiness, scoring
from app.recommend import heuristic_v2
from tests import intel_data as D
from tests.github_mock import FakeGitHub, ok, status
from tests.seed import PROFILE, SETTINGS, make_user, seed_issue, seed_repo

SEARCH = "/api/v1/matches/search"
MATCHES = "/api/v1/matches"
BODY = "Parser crashes when loading data. " + "x" * 120


@pytest.fixture
def env(client, db_session):
    gh = FakeGitHub({})
    client.app.dependency_overrides[get_settings] = lambda: SETTINGS
    client.app.dependency_overrides[get_github_client] = lambda: gh.client
    return gh


@pytest.fixture
def alice(client, db_session, env):
    headers, _ = make_user(db_session, 1, "alice")
    client.post("/api/v1/profile", json=PROFILE, headers=headers)
    return headers


def search(client, headers, scorer="heuristic_v2", **kw):
    r = client.post(SEARCH, json={"scorer": scorer, **kw}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def by_repo(body):
    return {i["repository"]["name"]: i for i in body["items"]}


def twin_repos(db, kinds):
    """Identical Python repos/issues that differ only in their stored intelligence."""
    out = {}
    for name, kind in kinds.items():
        repo = seed_repo(db, "acme", name, "Python")
        seed_issue(db, repo, 1, "Parser crashes on load", BODY)
        if kind:
            D.store_snapshot(db, repo, kind)
        out[name] = repo
    return out


# ---------------------------------------------------------------- selection + versioning


def test_v1_remains_the_default_and_v2_is_opt_in(client, alice, db_session):
    twin_repos(db_session, {"a": "healthy"})
    v1 = client.post(SEARCH, json={}, headers=alice).json()
    assert v1["model"]["version"] == "v1" and "repository_activity" in v1["weights"]
    v2 = search(client, alice)
    assert (v2["model"]["name"], v2["model"]["version"], v2["model"]["is_ml_prediction"]) == ("heuristic_baseline", "v2", False)
    assert v2["weights"] == {"skill_compatibility": 0.30, "interest_similarity": 0.20, "difficulty_suitability": 0.15,
                             "repository_health": 0.20, "contribution_readiness": 0.15}
    assert sum(v2["weights"].values()) == pytest.approx(1.0)
    assert "not shown to outperform" in v2["model"]["description"]
    assert client.post(SEARCH, json={"scorer": "heuristic_v3"}, headers=alice).status_code == 422


def test_v2_changes_only_activity_and_readiness_components(client, alice, db_session):
    twin_repos(db_session, {"a": "healthy"})
    item = search(client, alice)["items"][0]
    assert list(item["components"]) == ["skill_compatibility", "interest_similarity", "difficulty_suitability",
                                       "repository_health", "contribution_readiness"]
    v1_item = client.post(SEARCH, json={"persist": False}, headers=alice).json()["items"][0]
    for same in ("skill_compatibility", "interest_similarity", "difficulty_suitability"):
        a, b = item["components"][same], v1_item["components"][same]
        assert (a["score"], a["confidence"], a["evidence"]) == (b["score"], b["confidence"], b["evidence"])


# ---------------------------------------------------------------- health and readiness affect ranking


def test_repository_health_changes_ranking_where_v1_cannot_tell_repos_apart(client, alice, db_session):
    twin_repos(db_session, {"alpha-inactive": "inactive", "zeta-healthy": "healthy"})
    v1 = client.post(SEARCH, json={"persist": False}, headers=alice).json()["items"]
    assert v1[0]["final_score"] == v1[1]["final_score"]  # v1 sees identical repos...
    assert [i["repository"]["name"] for i in v1] == ["alpha-inactive", "zeta-healthy"]  # ...so alphabetical tie-break
    v2 = search(client, alice, persist=False)["items"]
    assert [i["repository"]["name"] for i in v2] == ["zeta-healthy", "alpha-inactive"]
    assert v2[0]["final_score"] > v2[1]["final_score"] + 2
    healthy = v2[0]["components"]["repository_health"]
    assert healthy["status"] == "known" and healthy["score"] >= 70 and healthy["confidence"] >= 0.6


def test_missing_health_is_unknown_never_better_than_good_nor_free_of_penalty(client, alice, db_session):
    twin_repos(db_session, {"healthy": "healthy", "unanalysed": None, "inactive": "inactive"})
    body = search(client, alice, persist=False)
    items = by_repo(body)
    unknown = items["unanalysed"]["components"]["repository_health"]
    assert unknown["status"] == "unknown" and unknown["score"] is None and unknown["effective_score"] == 25.0
    assert any("has not been analysed" in w for w in unknown["warnings"])
    scores = {n: i["final_score"] for n, i in items.items()}
    assert scores["healthy"] > scores["unanalysed"] > scores["inactive"]  # unknown sits between good and genuinely bad
    assert any("Repository health has not been analysed for 1 repository" in r for r in body["ranking_reliability"]["reasons"])


def test_linked_pr_state_changes_ranking_only_in_v2(client, alice, db_session):
    repo = seed_repo(db_session, "acme", "proj", "Python")
    D.store_snapshot(db_session, repo, "healthy")
    issues = [seed_issue(db_session, repo, n, "Parser crashes on load", BODY) for n in (1, 2)]
    D.store_linked_prs(db_session, issues[0], [(50, "open", None)])  # issue 1 already has an open PR
    D.store_linked_prs(db_session, issues[1], [])  # issue 2 checked: nothing references it
    v1 = [i["issue"]["number"] for i in client.post(SEARCH, json={"persist": False}, headers=alice).json()["items"]]
    v2 = search(client, alice, persist=False)["items"]
    assert v1 == [1, 2]  # v1 cannot see PRs: tie broken by number
    assert [i["issue"]["number"] for i in v2] == [2, 1]
    open_pr = v2[1]["components"]["contribution_readiness"]
    assert any("already be working" in e for e in open_pr["evidence"] + v2[1]["reasons"]) or "already be working" in str(v2[1])


def test_readiness_signals_reorder_issues_in_one_repository(client, alice, db_session):
    repo = seed_repo(db_session, "acme", "proj", "Python")
    D.store_snapshot(db_session, repo, "healthy")
    seed_issue(db_session, repo, 1, "Parser crashes on load", "crash", labels=())
    seed_issue(db_session, repo, 2, "Parser crashes on load", D.GOOD_BODY, labels=("good first issue",))
    items = search(client, alice, persist=False)["items"]
    assert [i["issue"]["number"] for i in items] == [2, 1]
    good, thin = (i["components"]["contribution_readiness"] for i in items)
    assert good["score"] > thin["score"] and any("Missing" in w for w in thin["warnings"] + items[1]["warnings"])


def test_stale_and_archived_analyses(client, alice, db_session):
    repos = twin_repos(db_session, {"fresh": None, "old": None, "closed": None})
    D.store_snapshot(db_session, repos["old"], "healthy", age_days=20)
    D.store_snapshot(db_session, repos["closed"], "healthy", archived=True)
    items = by_repo(search(client, alice, persist=False))
    old = items["old"]["components"]["repository_health"]
    assert old["status"] == "stale" and old["score"] is None and "too old to trust" in old["warnings"][0]
    closed = items["closed"]["components"]["repository_health"]
    assert closed["score"] == 0.0 and closed["status"] == "known"
    assert items["closed"]["final_score"] < items["old"]["final_score"]


def test_explanations_cite_actual_signals_and_flag_what_is_unchecked(client, alice, db_session):
    twin_repos(db_session, {"a": "healthy"})
    item = search(client, alice, persist=False)["items"][0]
    text = " ".join(item["reasons"])
    assert "Repository health" in text and "Last commit" in text and "Maintainers replied" in text
    readiness_lines = item["components"]["contribution_readiness"]["evidence"]
    assert readiness_lines[0].startswith("Contribution readiness")  # shown even when it is not a positive "reason"
    assert any("linked pull requests" in w for w in item["components"]["contribution_readiness"]["warnings"])
    for c in ("repository_health", "contribution_readiness"):
        assert item["components"][c]["confidence"] < 1.0  # never certain


def test_v2_final_score_is_the_weighted_sum_of_effective_components(client, alice, db_session):
    twin_repos(db_session, {"a": "healthy", "b": None, "c": "inactive"})
    for item in search(client, alice, persist=False)["items"]:
        total = sum(c["weight"] * c["effective_score"] for c in item["components"].values())
        assert item["final_score"] == pytest.approx(total, abs=1e-3) and 0 <= item["final_score"] <= 100


# ---------------------------------------------------------------- history + storage


def test_v1_history_is_preserved_when_v2_is_used(client, alice, db_session):
    twin_repos(db_session, {"a": "healthy", "b": None})
    v1 = client.post(SEARCH, json={}, headers=alice).json()["items"]
    v1_ids = [i["match_id"] for i in v1]
    v1_detail = [client.get(f"{MATCHES}/{m}", headers=alice).json() for m in v1_ids]
    v2 = search(client, alice)["items"]
    assert not set(v1_ids) & {i["match_id"] for i in v2}
    assert [client.get(f"{MATCHES}/{m}", headers=alice).json() for m in v1_ids] == v1_detail  # untouched
    rows = db_session.scalars(select(Recommendation)).all()
    assert sorted({r.ranking_model_version for r in rows}) == ["heuristic_v1", "heuristic_v2"]
    before = db_session.query(Recommendation).count()
    client.post(SEARCH, json={}, headers=alice)  # re-running v1 after v2 existed must not add rows
    assert db_session.query(Recommendation).count() == before
    for r in rows:
        if r.ranking_model_version == "heuristic_v1":
            assert "intel" not in r.data_snapshot["issue"]  # v1 snapshots keep their original shape
            assert "repository_activity" in r.components["components"]


def test_v2_matches_are_stored_with_inputs_and_config_for_reproducibility(client, alice, db_session):
    twin_repos(db_session, {"a": "healthy"})
    item = search(client, alice)["items"][0]
    d = client.get(f"{MATCHES}/{item['match_id']}", headers=alice).json()
    assert d["model"]["id"] == "heuristic_v2" and d["scoring_config"]["formula"] == "heuristic_v2"
    assert d["scoring_config"]["component_weights"]["repository_health"] == 0.20
    intel = d["data_snapshot"]["issue"]["intel"]
    assert intel["health"]["report"]["kind"] == "repository_health" and intel["readiness"]["report"]["kind"] == "issue_readiness"
    assert "repository_health" in d["component_scores"] and "repository_activity" not in d["component_scores"]
    assert db_session.get(Recommendation, item["match_id"]).activity_score == item["components"]["repository_health"]["score"]
    listed = client.get(MATCHES, params={"model": "heuristic_v2"}, headers=alice).json()
    assert listed["total"] == 1 and listed["items"][0]["model"]["id"] == "heuristic_v2"


def test_v2_retry_is_idempotent_and_data_changes_create_new_history(client, alice, db_session):
    repos = twin_repos(db_session, {"a": "healthy"})
    first = search(client, alice)["items"][0]["match_id"]
    assert search(client, alice)["items"][0]["match_id"] == first
    assert db_session.query(Recommendation).count() == 1
    D.store_snapshot(db_session, repos["a"], "inactive", sha="c" * 40)  # a newer analysis of the repository
    again = search(client, alice)["items"][0]["match_id"]
    assert again != first  # changed inputs -> new row; the first one is still there
    assert client.get(f"{MATCHES}/{first}", headers=alice).status_code == 200


def test_other_users_cannot_read_v2_matches(client, db_session, env):
    a, _ = make_user(db_session, 1, "alice")
    b, _ = make_user(db_session, 2, "bob")
    client.post("/api/v1/profile", json=PROFILE, headers=a)
    twin_repos(db_session, {"a": "healthy"})
    mine = search(client, a)["items"][0]["match_id"]
    assert client.get(f"{MATCHES}/{mine}", headers=b).status_code == 404
    assert client.get(f"{MATCHES}/{mine}", headers=a).status_code == 200
    theirs = search(client, b)["items"][0]
    assert theirs["match_id"] != mine and theirs["components"]["skill_compatibility"]["score"] is None  # bob has no profile


# ---------------------------------------------------------------- requesting repositories (live analysis)


def test_requested_repository_is_analysed_and_used_by_v2(client, alice, env, db_session):
    env.routes.update(D.healthy_routes())
    body = search(client, alice, repositories=["o/r"])
    item = body["items"][0]
    assert item["components"]["repository_health"]["status"] == "known"
    assert db_session.query(Repository_snapshot()).count() == 1
    calls = env.total
    search(client, alice, repositories=["o/r"])
    assert env.total == calls  # everything fresh: no further GitHub requests


def Repository_snapshot():
    from app.db.models import RepositorySnapshot
    return RepositorySnapshot


def test_v1_search_never_triggers_analysis(client, alice, env, db_session):
    env.routes.update(D.healthy_routes())
    client.post(SEARCH, json={"repositories": ["o/r"]}, headers=alice)
    assert env.calls["/repos/o/r/commits"] == 0 and env.calls[f"/repos/o/r/git/trees/{D.HEAD}"] == 0
    assert db_session.query(Repository_snapshot()).count() == 0


def test_analysis_failure_degrades_to_unknown_without_blocking_results(client, alice, env, db_session):
    routes = D.healthy_routes()
    state = {"issues_calls": 0}

    def issues_once(request):
        state["issues_calls"] += 1
        return routes["/repos/o/r/issues"] if state["issues_calls"] == 1 else status(503)  # sync works; analysis does not

    env.routes.update(routes)
    env.routes["/repos/o/r/issues"] = issues_once
    for p in ("commits", "releases", "pulls", "issues/comments"):
        env.routes[f"/repos/o/r/{p}"] = status(503)
    env.routes[f"/repos/o/r/git/trees/main"] = status(503)
    body = search(client, alice, repositories=["o/r"])
    assert body["items"] and body["items"][0]["components"]["repository_health"]["status"] == "unknown"
    assert [e["repository"] for e in body["upstream_errors"]] == ["o/r (health analysis)"]
    assert body["ranking_reliability"]["status"] == "limited"


# ---------------------------------------------------------------- frozen code per version

# Digest of everything that defines heuristic_v2 behaviour (scorer + analyzers). When it must change,
# bump heuristic_v2.V2_INFO.version to a NEW version and add a new entry; never edit an existing one.
SCORING_CODE_DIGESTS_V2 = {
    "v2": "5aa608ae6ff2d373a819433b89162d9e09a5f29fcfc27f3633478c1902636a37",
}


def _digest() -> str:
    mods = (heuristic_v2, intel_config, scoring, activity, readiness)
    source = "".join(inspect.getsource(m) for m in mods).replace("\r\n", "\n")
    return hashlib.sha256(source.encode()).hexdigest()


def test_heuristic_v2_code_is_frozen_per_version():
    version = heuristic_v2.V2_INFO.version
    assert version in SCORING_CODE_DIGESTS_V2, f"new version {version!r}: add digest {_digest()}"
    assert _digest() == SCORING_CODE_DIGESTS_V2[version], (
        f"v2 scoring/analysis code changed but version is still {version!r}. Bump the version and add digest {_digest()}.")
