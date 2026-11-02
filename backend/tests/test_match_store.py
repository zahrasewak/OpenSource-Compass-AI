"""Persisted matches: storage, idempotency, immutability, retrieval, isolation.
SQLite only; PostgreSQL behaviour is unverified (see docs/VALIDATION_STATUS.md)."""

import hashlib
import inspect

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_github_client, get_scorer
from app.core.config import get_settings
from app.db.models import Issue, Recommendation
from app.recommend import HeuristicScorer, ScoringConfig
from app.recommend import components, heuristic, scoring_config, vocabulary
from app.recommend.types import ComponentScore, ModelInfo, ScoredIssue
from app.services import match_store
from tests.github_mock import FakeGitHub
from tests.seed import PROFILE, SETTINGS, make_user, seed_issue, seed_repo

SEARCH = "/api/v1/matches/search"
MATCHES = "/api/v1/matches"


@pytest.fixture
def env(client, db_session):
    gh = FakeGitHub({})
    client.app.dependency_overrides[get_settings] = lambda: SETTINGS
    client.app.dependency_overrides[get_github_client] = lambda: gh.client
    return gh


@pytest.fixture
def alice(client, db_session, env):
    headers, user = make_user(db_session, 1, "alice")
    client.post("/api/v1/profile", json=PROFILE, headers=headers)
    return headers


@pytest.fixture
def corpus(db_session):
    py = seed_repo(db_session, "acme", "ml-api", "Python", topics=("machine-learning", "fastapi"))
    rs = seed_repo(db_session, "acme", "rusty", "Rust")
    seed_issue(db_session, py, 1, "FastAPI model loader fails", "Machine learning pipeline crashes. " + "x" * 100,
               labels=("good first issue",))
    seed_issue(db_session, py, 2, "Docs typo", "y" * 100)
    seed_issue(db_session, rs, 3, "Borrow checker error", "z" * 100, labels=("hard",))
    return py, rs


def search(client, headers, **body):
    r = client.post(SEARCH, json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- persistence


def test_search_persists_returned_page_with_method_version_and_breakdown(client, alice, corpus, db_session):
    body = search(client, alice)
    assert len(body["items"]) == 3 and all(i["match_id"] and i["generated_at"] for i in body["items"])
    rows = db_session.scalars(select(Recommendation).order_by(Recommendation.id)).all()
    assert len(rows) == 3
    row = db_session.get(Recommendation, body["items"][0]["match_id"])
    assert row.ranking_model_version == "heuristic_v1"
    assert row.final_score == body["items"][0]["final_score"]
    assert row.skill_score == body["items"][0]["components"]["skill_compatibility"]["score"]
    assert row.coverage == body["items"][0]["coverage"]
    stored = row.components
    assert stored["model"] == {"id": "heuristic_v1", "name": "heuristic_baseline", "version": "v1",
                               "kind": "heuristic", "is_ml_prediction": False}
    assert stored["scoring_config"]["w_skill"] == 0.35 and stored["scoring_config"]["unknown_fill"] == 25.0
    assert set(stored["components"]) == set(body["items"][0]["components"])
    assert stored["warnings"] == body["items"][0]["warnings"]
    snap = row.data_snapshot
    assert snap["scored_at"] and snap["user"]["skills"]["python"] == 0.9
    assert snap["issue"]["number"] == 1 and snap["issue"]["repo_fetched_at"] and snap["issue"]["issue_fetched_at"]


def test_unknown_components_are_stored_as_null_not_zero(client, alice, corpus, db_session):
    item = next(i for i in search(client, alice)["items"] if i["issue"]["number"] == 2)  # no difficulty label
    row = db_session.get(Recommendation, item["match_id"])
    assert row.difficulty_score is None and row.components["components"]["difficulty_suitability"]["status"] == "unknown"
    assert row.skill_score is not None


def test_only_the_returned_page_is_stored(client, alice, corpus, db_session):
    body = search(client, alice, limit=1, offset=1)
    assert len(body["items"]) == 1
    assert db_session.query(Recommendation).count() == 1


def test_dry_run_persists_nothing(client, alice, corpus, db_session):
    body = search(client, alice, persist=False)
    assert all(i["match_id"] is None and i["generated_at"] is None for i in body["items"])
    assert db_session.query(Recommendation).count() == 0


def test_retried_search_is_idempotent(client, alice, corpus, db_session):
    first = search(client, alice)
    second = search(client, alice)
    assert [i["match_id"] for i in first["items"]] == [i["match_id"] for i in second["items"]]
    assert [i["generated_at"] for i in first["items"]] == [i["generated_at"] for i in second["items"]]
    assert db_session.query(Recommendation).count() == 3


def test_concurrent_identical_insert_is_resolved_by_rereading(client, alice, corpus, db_session, monkeypatch):
    """Simulate a competing request committing the same rows between our check and our commit."""
    real_commit = db_session.commit
    state = {"calls": 0}

    def racing_commit():
        state["calls"] += 1
        if state["calls"] == 1:
            real_commit()  # the "other request" wins
            raise IntegrityError("INSERT", {}, Exception("unique violation"))
        real_commit()

    monkeypatch.setattr(db_session, "commit", racing_commit)
    body = search(client, alice)
    monkeypatch.setattr(db_session, "commit", real_commit)
    assert all(i["match_id"] for i in body["items"])
    assert db_session.query(Recommendation).count() == 3  # no duplicates


def test_changed_profile_creates_new_history_and_old_scores_are_untouched(client, alice, corpus, db_session):
    before = search(client, alice)["items"]
    old_ids = {i["match_id"]: i["final_score"] for i in before}
    client.put("/api/v1/profile", json={**PROFILE, "skills": [{"name": "rust", "proficiency": 1.0}]}, headers=alice)
    after = search(client, alice)["items"]
    assert not set(old_ids) & {i["match_id"] for i in after}  # all new rows
    assert db_session.query(Recommendation).count() == 6
    for match_id, score in old_ids.items():  # history unchanged
        assert client.get(f"{MATCHES}/{match_id}", headers=alice).json()["final_score"] == score


def test_changed_issue_data_creates_a_new_record(client, alice, corpus, db_session):
    search(client, alice)
    issue = db_session.scalar(select(Issue).where(Issue.issue_number == 2))
    issue.body = "Now mentions FastAPI and machine learning " + "q" * 100
    db_session.commit()
    search(client, alice)
    assert db_session.query(Recommendation).count() == 4  # only issue 2 differs


def test_formula_change_does_not_rewrite_history(client, alice, corpus, db_session):
    old = search(client, alice)["items"][0]
    new_scorer = HeuristicScorer(ScoringConfig(w_skill=0.05, w_interest=0.05, w_difficulty=0.05,
                                               w_activity=0.80, w_readiness=0.05))
    client.app.dependency_overrides[get_scorer] = lambda: new_scorer
    new = search(client, alice)["items"][0]
    assert new["final_score"] != old["final_score"] and new["match_id"] != old["match_id"]
    kept = client.get(f"{MATCHES}/{old['match_id']}", headers=alice).json()
    assert kept["final_score"] == old["final_score"] and kept["scoring_config"]["w_skill"] == 0.35
    assert client.get(f"{MATCHES}/{new['match_id']}", headers=alice).json()["scoring_config"]["w_activity"] == 0.8


# Digest of the scoring code for each released heuristic version. Never edit an existing entry:
# when scoring behaviour changes, bump HEURISTIC_INFO.version and ADD a new entry.
SCORING_CODE_DIGESTS = {
    "v1": "fccef4a77eaa35e85b86f6002b9d4635995ee6263b6aa84a5a16d327a94d5af3",
}


def test_heuristic_scoring_code_is_frozen_per_version():
    """Fails if scoring code changed without a version bump, because stored `heuristic_v1` results would
    then no longer mean what they say. Comment/format-only edits also trip it: intentional friction."""
    source = "".join(inspect.getsource(m) for m in (components, heuristic, scoring_config, vocabulary))
    digest = hashlib.sha256(source.replace("\r\n", "\n").encode()).hexdigest()
    version = heuristic.HEURISTIC_INFO.version
    assert version in SCORING_CODE_DIGESTS, f"new version {version!r}: add its digest {digest}"
    assert digest == SCORING_CODE_DIGESTS[version], (
        f"scoring code changed but version is still {version!r}. Bump HEURISTIC_INFO.version and add "
        f"digest {digest} for the new version."
    )


class FakeMlScorer:
    info = ModelInfo("fake_ml", "t0", "ml", True, "test double")

    def score(self, user, issue, now):
        comp = ComponentScore("skill_compatibility", 1.0, 80.0, 1.0, "known", effective_score=80.0)
        return ScoredIssue(final_score=80.0, coverage=1.0, insufficient_information=False,
                           components={"skill_compatibility": comp}, reasons=(), warnings=(),
                           missing_skills=(), model=self.info)


def test_heuristic_and_other_scorers_coexist_for_later_comparison(client, alice, corpus, db_session):
    search(client, alice)
    client.app.dependency_overrides[get_scorer] = lambda: FakeMlScorer()
    search(client, alice)
    heur = client.get(MATCHES, params={"model": "heuristic_v1"}, headers=alice).json()
    other = client.get(MATCHES, params={"model": "ml_t0"}, headers=alice).json()
    assert heur["total"] == 3 and other["total"] == 3
    assert {m["issue"]["number"] for m in heur["items"]} == {m["issue"]["number"] for m in other["items"]}
    assert other["items"][0]["model"]["is_ml_prediction"] is True
    assert client.get(MATCHES, headers=alice).json()["total"] == 6


# ---------------------------------------------------------------- retrieval


def test_get_match_detail_returns_stored_breakdown(client, alice, corpus):
    item = search(client, alice)["items"][0]
    r = client.get(f"{MATCHES}/{item['match_id']}", headers=alice)
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == item["match_id"] and d["final_score"] == item["final_score"]
    assert d["model"]["id"] == "heuristic_v1" and d["model"]["is_ml_prediction"] is False
    assert d["repository"]["full_name"] == "acme/ml-api" and d["issue"]["number"] == 1
    assert d["issue"]["title"] == "FastAPI model loader fails"
    assert d["components"] == item["components"] and d["reasons"] == item["reasons"]
    assert d["warnings"] == item["warnings"] and d["missing_skills"] == item["missing_skills"]
    assert d["component_scores"]["skill_compatibility"] == item["components"]["skill_compatibility"]["score"]
    assert d["scoring_config"]["unknown_fill"] == 25.0 and d["data_snapshot"]["user"]["has_profile"] is True
    assert "never recomputed" in d["note"]


def test_stored_match_does_not_change_when_issue_changes(client, alice, corpus, db_session):
    item = search(client, alice)["items"][0]
    issue = db_session.scalar(select(Issue).where(Issue.issue_number == 1))
    issue.title = "Totally different title"
    db_session.commit()
    d = client.get(f"{MATCHES}/{item['match_id']}", headers=alice).json()
    assert d["issue"]["title"] == "FastAPI model loader fails"


def test_list_pagination_order_and_filters(client, alice, corpus):
    items = search(client, alice)["items"]
    pages = [client.get(MATCHES, params={"limit": 2, "offset": o}, headers=alice).json() for o in (0, 2)]
    assert [len(p["items"]) for p in pages] == [2, 1] and pages[0]["total"] == 3
    ids = [m["id"] for p in pages for m in p["items"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 3  # newest first, no overlap
    only = client.get(MATCHES, params={"repository": "ACME/rusty"}, headers=alice).json()
    assert [m["issue"]["number"] for m in only["items"]] == [3]
    top = max(i["final_score"] for i in items)
    assert client.get(MATCHES, params={"min_score": top}, headers=alice).json()["total"] >= 1
    assert client.get(MATCHES, params={"min_score": 100}, headers=alice).json()["total"] == 0
    assert client.get(MATCHES, params={"model": "nope_v9"}, headers=alice).json()["total"] == 0
    assert client.get(MATCHES, params={"offset": 50}, headers=alice).json()["items"] == []


def test_include_insufficient_filter(client, db_session, env, corpus):
    headers, _ = make_user(db_session, 5, "noprofile")
    search(client, headers)
    assert client.get(MATCHES, headers=headers).json()["total"] == 3
    assert client.get(MATCHES, params={"include_insufficient": "false"}, headers=headers).json()["total"] == 0


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"min_score": 101},
                                    {"min_score": -1}, {"repository": "no-slash"}, {"repository": "a/b/c"},
                                    {"include_insufficient": "maybe"}])
def test_list_validation(client, alice, params):
    assert client.get(MATCHES, params=params, headers=alice).status_code == 422


# ---------------------------------------------------------------- auth + isolation


def test_other_users_cannot_read_a_match_by_guessing_the_id(client, db_session, env, corpus):
    a, _ = make_user(db_session, 1, "alice")
    b, _ = make_user(db_session, 2, "bob")
    client.post("/api/v1/profile", json=PROFILE, headers=a)
    mine = search(client, a)["items"]
    for item in mine:
        r = client.get(f"{MATCHES}/{item['match_id']}", headers=b)
        assert r.status_code == 404 and r.json()["error"]["code"] == "match_not_found"
        assert client.get(f"{MATCHES}/{item['match_id']}", headers=a).status_code == 200
    assert client.get(MATCHES, headers=b).json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    # bob's own search never reuses alice's rows even for identical issues
    bobs = search(client, b)["items"]
    assert not {i["match_id"] for i in bobs} & {i["match_id"] for i in mine}
    assert client.get(MATCHES, headers=a).json()["total"] == 3


def test_missing_and_malformed_ids(client, alice):
    assert client.get(f"{MATCHES}/99999", headers=alice).status_code == 404
    for bad in ("0", "-1", "abc", "99999999999"):
        assert client.get(f"{MATCHES}/{bad}", headers=alice).status_code == 422


def test_authentication_required(client, env, corpus):
    assert client.get(MATCHES).status_code == 401
    assert client.get(f"{MATCHES}/1").status_code == 401
    assert client.get(MATCHES, headers={"Authorization": "Bearer junk"}).status_code == 401
