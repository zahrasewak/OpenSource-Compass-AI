"""Saved issues and feedback: behaviour, validation, event/feedback separation, isolation (SQLite only)."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_github_client
from app.core.config import get_settings
from app.db.models import Feedback, Issue, SavedIssue
from tests.github_mock import REPO, FakeGitHub, issue as gh_issue, ok, status
from tests.seed import PROFILE, SETTINGS, make_user, seed_issue, seed_repo

SAVED = "/api/v1/saved-issues"
FEEDBACK = "/api/v1/feedback"
SEARCH = "/api/v1/matches/search"


@pytest.fixture
def env(client, db_session):
    gh = FakeGitHub({})
    client.app.dependency_overrides[get_settings] = lambda: SETTINGS
    client.app.dependency_overrides[get_github_client] = lambda: gh.client
    return gh


@pytest.fixture
def users(db_session, env):
    return make_user(db_session, 1, "alice")[0], make_user(db_session, 2, "bob")[0]


@pytest.fixture
def corpus(db_session):
    repo = seed_repo(db_session, "acme", "ml-api", "Python")
    for n in (1, 2, 3):
        seed_issue(db_session, repo, n, f"Issue {n}", "b" * 100)
    return repo


def ref(n=1, **extra):
    return {"owner": "acme", "repo": "ml-api", "number": n, **extra}


def events(db, **filters):
    return db.scalars(select(Feedback).filter_by(**filters).order_by(Feedback.id)).all()


def make_match(client, headers):
    client.post("/api/v1/profile", json=PROFILE, headers=headers)
    return client.post(SEARCH, json={}, headers=headers).json()["items"]


# ================================================================ saved issues


def test_save_creates_entry_and_system_event_without_github_calls(client, users, corpus, env, db_session):
    a, _ = users
    r = client.post(SAVED, json=ref(), headers=a)
    assert r.status_code == 201
    body = r.json()
    assert body["created"] is True and body["number"] == 1 and body["title"] == "Issue 1"
    assert body["html_url"] == "https://github.com/acme/ml-api/issues/1"
    assert env.total == 0
    (ev,) = events(db_session, feedback_type="saved")
    assert (ev.category, ev.source, ev.feedback_value) == ("event", "system", 1)
    assert events(db_session, feedback_type="relevance") == []  # saving is not feedback


def test_duplicate_save_is_idempotent(client, users, corpus, db_session):
    a, _ = users
    first = client.post(SAVED, json=ref(), headers=a).json()
    again = client.post(SAVED, json=ref(), headers=a)
    assert again.status_code == 200 and again.json()["created"] is False and again.json()["id"] == first["id"]
    assert db_session.query(SavedIssue).count() == 1 and len(events(db_session, feedback_type="saved")) == 1


def test_concurrent_duplicate_save_returns_existing(client, users, corpus, db_session, monkeypatch):
    a, _ = users
    real_commit = db_session.commit
    calls = {"n": 0}

    def racing():
        calls["n"] += 1
        if calls["n"] == 1:
            real_commit()
            raise IntegrityError("INSERT", {}, Exception("unique"))
        real_commit()

    monkeypatch.setattr(db_session, "commit", racing)
    r = client.post(SAVED, json=ref(), headers=a)
    monkeypatch.setattr(db_session, "commit", real_commit)
    assert r.status_code == 200 and r.json()["created"] is False
    assert db_session.query(SavedIssue).count() == 1


def test_save_from_match_links_event_to_the_match(client, users, corpus, db_session):
    a, _ = users
    match = make_match(client, a)[0]
    number = match["issue"]["number"]
    assert client.post(SAVED, json=ref(number, match_id=match["match_id"]), headers=a).status_code == 201
    (ev,) = events(db_session, feedback_type="saved")
    assert ev.recommendation_id == match["match_id"]


def test_save_with_someone_elses_or_mismatched_match(client, users, corpus):
    a, b = users
    match = make_match(client, a)[0]
    other = next(n for n in (1, 2, 3) if n != match["issue"]["number"])
    assert client.post(SAVED, json=ref(match["issue"]["number"], match_id=match["match_id"]), headers=b).status_code == 404
    r = client.post(SAVED, json=ref(other, match_id=match["match_id"]), headers=a)
    assert r.status_code == 422 and r.json()["error"]["code"] == "target_mismatch"


def test_unknown_issue_is_fetched_validated_and_saved(client, users, env, db_session):
    a, _ = users
    env.routes.update({"/repos/o/r": ok(REPO), "/repos/o/r/issues/9": ok(gh_issue(9, title="remote"))})
    r = client.post(SAVED, json={"owner": "o", "repo": "r", "number": 9}, headers=a)
    assert r.status_code == 201 and r.json()["title"] == "remote"
    assert db_session.query(Issue).count() == 1


def test_issue_missing_on_github_is_not_saved(client, users, env, db_session):
    a, _ = users
    env.routes.update({"/repos/o/r": ok(REPO)})  # issue 404s
    r = client.post(SAVED, json={"owner": "o", "repo": "r", "number": 9}, headers=a)
    assert r.status_code == 404 and r.json()["error"]["code"] == "issue_not_found"
    assert db_session.query(SavedIssue).count() == 0 and events(db_session) == []


def test_pull_request_cannot_be_saved_as_issue(client, users, env, db_session):
    a, _ = users
    env.routes.update({"/repos/o/r": ok(REPO), "/repos/o/r/issues/5": ok(gh_issue(5, pr=True))})
    r = client.post(SAVED, json={"owner": "o", "repo": "r", "number": 5}, headers=a)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_an_issue"


def test_github_outage_for_unknown_issue_maps_error_and_saves_nothing(client, users, env, db_session):
    a, _ = users
    env.routes.update({"/repos/o/r": status(503)})
    r = client.post(SAVED, json={"owner": "o", "repo": "r", "number": 9}, headers=a)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_unavailable"
    assert db_session.query(SavedIssue).count() == 0


def test_already_stored_issue_can_be_saved_while_github_is_down(client, users, corpus, env):
    a, _ = users
    env.routes.update({"/repos/acme/ml-api": httpx.ConnectError("down")})
    assert client.post(SAVED, json=ref(2), headers=a).status_code == 201
    assert env.total == 0


def test_list_saved_pagination_order_and_isolation(client, users, corpus):
    a, b = users
    for n in (1, 2, 3):
        client.post(SAVED, json=ref(n), headers=a)
    client.post(SAVED, json=ref(1), headers=b)
    full = client.get(SAVED, headers=a).json()
    assert full["total"] == 3 and [i["number"] for i in full["items"]] == [3, 2, 1]  # newest first
    p1 = client.get(SAVED, params={"limit": 2}, headers=a).json()
    p2 = client.get(SAVED, params={"limit": 2, "offset": 2}, headers=a).json()
    assert [len(p1["items"]), len(p2["items"])] == [2, 1] and p1["total"] == 3
    assert client.get(SAVED, headers=b).json()["total"] == 1
    assert client.get(SAVED, params={"offset": 99}, headers=a).json()["items"] == []


def test_unsave_records_event_and_enforces_ownership(client, users, corpus, db_session):
    a, b = users
    saved = client.post(SAVED, json=ref(), headers=a).json()
    assert client.delete(f"{SAVED}/{saved['id']}", headers=b).status_code == 404  # not bob's
    assert db_session.query(SavedIssue).count() == 1
    assert client.delete(f"{SAVED}/{saved['id']}", headers=a).status_code == 204
    assert client.delete(f"{SAVED}/{saved['id']}", headers=a).status_code == 404
    assert [e.feedback_type for e in events(db_session)] == ["saved", "unsaved"]
    again = client.post(SAVED, json=ref(), headers=a)  # re-saving after unsave is allowed (SQLite may reuse the id)
    assert again.status_code == 201 and again.json()["created"] is True


@pytest.mark.parametrize("body", [
    {"owner": "a", "repo": "b", "number": 0}, {"owner": "a", "repo": "b", "number": -1},
    {"owner": "a", "repo": "b", "number": "x"}, {"owner": "..", "repo": "b", "number": 1},
    {"owner": "a b", "repo": "b", "number": 1}, {"owner": "a", "repo": "b/c", "number": 1},
    {"owner": "a", "repo": "b"}, {"owner": "a", "repo": "b", "number": 1, "user_id": 2},
    {"owner": "a", "repo": "b", "number": 1, "match_id": 0},
])
def test_save_validation(client, users, env, body):
    r = client.post(SAVED, json=body, headers=users[0])
    assert r.status_code == 422 and env.total == 0


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_delete_id_validation(client, users, bad):
    assert client.delete(f"{SAVED}/{bad}", headers=users[0]).status_code == 422


def test_saved_endpoints_require_auth(client, env):
    assert client.post(SAVED, json=ref()).status_code == 401
    assert client.get(SAVED).status_code == 401
    assert client.delete(f"{SAVED}/1").status_code == 401
    assert client.get(SAVED, headers={"Authorization": "Bearer junk"}).status_code == 401


# ================================================================ feedback


def post(client, headers, **body):
    return client.post(FEEDBACK, json=body, headers=headers)


def test_relevance_feedback_by_issue_reference(client, users, corpus, db_session):
    a, _ = users
    r = post(client, a, type="relevance", value="relevant", **ref(2))
    assert r.status_code == 201
    body = r.json()
    assert (body["type"], body["value"], body["category"], body["source"]) == ("relevance", "relevant", "explicit_feedback", "user")
    assert body["duplicate"] is False and body["number"] == 2 and body["note"] is None
    (row,) = events(db_session)
    assert row.feedback_value == 1 and row.recommendation_id is None and row.created_at is not None


@pytest.mark.parametrize("ftype,value,stored", [
    ("relevance", "not_relevant", -1), ("difficulty", "too_easy", -1), ("difficulty", "appropriate", 0),
    ("difficulty", "too_difficult", 1), ("interest", "interested", 1), ("interest", "not_interested", -1),
])
def test_value_mapping(client, users, corpus, db_session, ftype, value, stored):
    r = post(client, users[0], type=ftype, value=value, **ref())
    assert r.status_code == 201 and r.json()["value"] == value
    assert events(db_session)[0].feedback_value == stored


def test_feedback_by_match_id_derives_issue_and_links_match(client, users, corpus, db_session):
    a, _ = users
    match = make_match(client, a)[0]
    r = post(client, a, type="difficulty", value="too_easy", match_id=match["match_id"])
    assert r.status_code == 201
    assert r.json()["match_id"] == match["match_id"] and r.json()["number"] == match["issue"]["number"]
    assert events(db_session)[0].recommendation_id == match["match_id"]


def test_match_and_reference_must_agree(client, users, corpus):
    a, _ = users
    match = make_match(client, a)[0]
    other = next(n for n in (1, 2, 3) if n != match["issue"]["number"])
    ok_ = post(client, a, type="relevance", value="relevant", match_id=match["match_id"], **ref(match["issue"]["number"]))
    assert ok_.status_code == 201
    bad = post(client, a, type="relevance", value="relevant", match_id=match["match_id"], **ref(other))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "target_mismatch"


def test_cannot_attach_feedback_to_another_users_match(client, users, corpus, db_session):
    a, b = users
    match = make_match(client, a)[0]
    r = post(client, b, type="relevance", value="relevant", match_id=match["match_id"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "match_not_found"
    assert events(db_session) == []


def test_unknown_issue_is_404_and_makes_no_github_calls(client, users, env, db_session):
    r = post(client, users[0], type="relevance", value="relevant", owner="x", repo="y", number=1)
    assert r.status_code == 404 and r.json()["error"]["code"] == "issue_not_found"
    assert env.total == 0 and events(db_session) == []


def test_identical_resubmission_is_a_duplicate_and_not_stored(client, users, corpus, db_session):
    a, _ = users
    first = post(client, a, type="relevance", value="relevant", **ref()).json()
    second = post(client, a, type="relevance", value="relevant", **ref())
    assert second.status_code == 200 and second.json()["duplicate"] is True and second.json()["id"] == first["id"]
    assert len(events(db_session)) == 1


def test_changing_your_answer_keeps_history_and_flipping_back_is_allowed(client, users, corpus, db_session):
    a, _ = users
    for value in ("relevant", "not_relevant", "relevant"):
        assert post(client, a, type="relevance", value=value, **ref()).status_code == 201
    assert [e.feedback_value for e in events(db_session)] == [1, -1, 1]


def test_different_types_and_targets_are_independent(client, users, corpus, db_session):
    a, _ = users
    assert post(client, a, type="relevance", value="relevant", **ref(1)).status_code == 201
    assert post(client, a, type="relevance", value="relevant", **ref(2)).status_code == 201
    assert post(client, a, type="interest", value="interested", **ref(1)).status_code == 201
    match = make_match(client, a)[0]
    assert post(client, a, type="relevance", value="relevant", match_id=match["match_id"]).status_code == 201
    assert len(events(db_session)) == 4


def test_attempted_and_contributed_are_self_reported_events_not_proof(client, users, corpus, db_session):
    a, _ = users
    r = post(client, a, type="contributed", **ref())  # value omitted
    assert r.status_code == 201
    body = r.json()
    assert (body["category"], body["source"], body["value"]) == ("event", "self_reported", "yes")
    assert "not verified" in body["note"] and "not treated as proof" in body["note"]
    assert post(client, a, type="attempted", value="yes", **ref()).status_code == 201
    client.post(SAVED, json=ref(), headers=a)
    by_source = {(e.feedback_type, e.source) for e in events(db_session)}
    assert by_source == {("contributed", "self_reported"), ("attempted", "self_reported"), ("saved", "system")}


def test_system_event_types_cannot_be_submitted_by_users(client, users, corpus):
    for t in ("saved", "unsaved", "merged"):
        assert post(client, users[0], type=t, value="yes", **ref()).status_code == 422


@pytest.mark.parametrize("body", [
    {"type": "relevance", "value": "maybe"}, {"type": "relevance"}, {"type": "difficulty", "value": "relevant"},
    {"type": "relevance", "value": "relevant"},  # no target
    {"type": "relevance", "value": "relevant", "owner": "acme"},  # partial reference
    {"type": "relevance", "value": "relevant", "owner": "acme", "repo": "ml-api"},
    {"type": "relevance", "value": "relevant", "match_id": 0}, {"type": "relevance", "value": "relevant", "match_id": "x"},
    {"type": "contributed", "value": "no", "owner": "acme", "repo": "ml-api", "number": 1},
    {"type": "relevance", "value": "relevant", "owner": "..", "repo": "r", "number": 1},
    {"type": "relevance", "value": "relevant", "owner": "acme", "repo": "ml-api", "number": 1, "user_id": 2},
    {"value": "relevant", "owner": "acme", "repo": "ml-api", "number": 1},
])
def test_feedback_validation(client, users, corpus, db_session, body):
    r = client.post(FEEDBACK, json=body, headers=users[0])
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert events(db_session) == []


def test_feedback_is_scoped_to_each_user(client, users, corpus, db_session):
    a, b = users
    post(client, a, type="relevance", value="relevant", **ref())
    rb = post(client, b, type="relevance", value="not_relevant", **ref())
    assert rb.status_code == 201 and rb.json()["duplicate"] is False  # bob's answer isn't alice's duplicate
    rows = events(db_session)
    assert sorted((r.user_id, r.feedback_value) for r in rows) == [(1, 1), (2, -1)]


def test_feedback_requires_auth(client, env, corpus):
    body = {"type": "relevance", "value": "relevant", **ref()}
    assert client.post(FEEDBACK, json=body).status_code == 401
    assert client.post(FEEDBACK, json=body, headers={"Authorization": "Bearer junk"}).status_code == 401
