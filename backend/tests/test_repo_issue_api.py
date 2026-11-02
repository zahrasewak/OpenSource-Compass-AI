"""Repository/issue API tests. GitHub is mocked; the database is in-memory SQLite."""

from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.api.deps import get_github_client
from app.core.config import Settings, get_settings
from app.core.security import issue_token
from app.db.models import Issue, Repository, User
from app.services import repositories as svc
from tests.github_mock import REPO, TOKEN, FakeGitHub, Script, issue, ok, status

SETTINGS = Settings(auth_secret="s" * 48, _env_file=None)
REPO_URL = "/api/v1/repositories/o/r"
ISSUE_URL = "/api/v1/issues/o/r"


@pytest.fixture
def setup(client, db_session):
    def _make(routes, settings=SETTINGS):
        gh = FakeGitHub(routes)
        client.app.dependency_overrides[get_settings] = lambda: settings
        client.app.dependency_overrides[get_github_client] = lambda: gh.client
        return gh

    return _make


@pytest.fixture
def auth(db_session):
    u = User(github_user_id=1, username="alice")
    db_session.add(u)
    db_session.commit()
    return {"Authorization": f"Bearer {issue_token(u.id, SETTINGS)}"}


def issues_route(items, link=None):
    return lambda r: ok(items, **({"link": link} if link else {}))


def routes(issue_items=None):
    return {
        "/repos/o/r": ok(REPO),
        "/repos/o/r/issues": ok(issue_items if issue_items is not None else [issue(1), issue(2)]),
    }


def age_repo(db, seconds=100_000):
    old = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    for r in db.scalars(select(Repository)):
        r.last_fetched_at = old
        r.issues_synced_at = old
    for i in db.scalars(select(Issue)):
        i.fetched_at = old
    db.commit()


# ---------------------------------------------------------------- repository endpoint


def test_get_repository_success_and_persisted(setup, auth, client, db_session):
    gh = setup(routes())
    r = client.get(REPO_URL, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["full_name"] == "o/r" and body["primary_language"] == "Python" and body["license_spdx"] == "MIT"
    assert body["html_url"] == "https://github.com/o/r" and body["topics"] == ["ml"]
    row = db_session.scalar(select(Repository))
    assert (row.owner, row.name, row.stargazers_count) == ("o", "r", 5)
    assert gh.total == 1


def test_response_contains_only_allow_listed_fields(setup, auth, client):
    setup({"/repos/o/r": ok({**REPO, "node_id": "X", "url": "https://api.github.com/x", "secret_extra": "S"})})
    r = client.get(REPO_URL, headers=auth)
    assert "node_id" not in r.text and "secret_extra" not in r.text and "api.github.com" not in r.text


def test_fresh_repository_served_without_github_call(setup, auth, client, db_session):
    gh = setup(routes())
    client.get(REPO_URL, headers=auth)
    client.get(REPO_URL, headers=auth)
    assert gh.total == 1
    assert db_session.query(Repository).count() == 1


def test_lookup_is_case_insensitive_and_does_not_duplicate(setup, auth, client, db_session):
    gh = setup(routes())
    client.get(REPO_URL, headers=auth)
    r = client.get("/api/v1/repositories/O/R", headers=auth)
    assert r.status_code == 200 and r.json()["full_name"] == "o/r"
    assert gh.total == 1
    age_repo(db_session)
    client.get("/api/v1/repositories/O/R", headers=auth)  # stale -> refetch, update in place
    assert db_session.query(Repository).count() == 1


def test_repeated_sync_updates_in_place(setup, auth, client, db_session):
    state = {"desc": "one"}
    gh = setup({"/repos/o/r": lambda r: ok({**REPO, "description": state["desc"], "stargazers_count": 9})})
    client.get(REPO_URL, headers=auth)
    row_id = db_session.scalar(select(Repository.id))
    state["desc"] = "two"
    age_repo(db_session)
    r = client.get(REPO_URL, headers=auth)
    assert r.json()["description"] == "two"
    assert db_session.query(Repository).count() == 1 and db_session.scalar(select(Repository.id)) == row_id


def test_refresh_honoured_only_when_data_not_too_recent(setup, auth, client, db_session):
    gh = setup(routes())
    client.get(REPO_URL, headers=auth)
    client.get(REPO_URL + "?refresh=true", headers=auth)  # younger than min_refresh -> ignored
    assert gh.total == 1
    gh.calls.clear()
    age_repo(db_session, seconds=60)  # older than min_refresh (30s) but inside the normal TTL (900s)
    client.get(REPO_URL + "?refresh=true", headers=auth)
    assert gh.total == 1
    client.get(REPO_URL, headers=auth)  # normal read: now fresh again
    assert gh.total == 1


def test_missing_repository_404(setup, auth, client, db_session):
    setup({})
    r = client.get(REPO_URL, headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "repository_not_found"
    assert db_session.query(Repository).count() == 0


@pytest.mark.parametrize("private", [True, None])
def test_private_or_unknown_visibility_is_never_stored_or_returned(setup, auth, client, db_session, private):
    payload = {**REPO, "description": "TOP-SECRET"}
    if private is None:
        payload.pop("private", None)
    else:
        payload["private"] = private
    setup({"/repos/o/r": ok(payload), "/repos/o/r/issues": ok([issue(1)])})
    for url in (REPO_URL, REPO_URL + "/issues", ISSUE_URL + "/1"):
        r = client.get(url, headers=auth)
        assert r.status_code == 404 and "TOP-SECRET" not in r.text
        assert r.json()["error"]["code"] == "repository_not_found"
    assert db_session.query(Repository).count() == 0 and db_session.query(Issue).count() == 0


def test_public_repository_requires_explicit_private_false(setup, auth, client):
    setup({"/repos/o/r": ok({**REPO, "private": False})})
    assert client.get(REPO_URL, headers=auth).status_code == 200


# ---------------------------------------------------------------- issue list


def many(n):
    return [issue(i, updated_at=f"2024-02-{(i % 27) + 1:02d}T00:00:00Z") for i in range(1, n + 1)]


def test_list_issues_persists_filters_pull_requests_and_paginates(setup, auth, client, db_session):
    items = many(5) + [issue(99, pr=True)]
    gh = setup(routes(items))
    r = client.get(REPO_URL + "/issues?limit=2&offset=0", headers=auth)
    body = r.json()
    assert r.status_code == 200 and body["total"] == 5 and len(body["items"]) == 2
    assert body["limit"] == 2 and body["offset"] == 0 and body["sync_truncated"] is False
    page2 = client.get(REPO_URL + "/issues?limit=2&offset=2", headers=auth).json()
    page3 = client.get(REPO_URL + "/issues?limit=2&offset=4", headers=auth).json()
    seen = [i["number"] for i in body["items"] + page2["items"] + page3["items"]]
    assert sorted(seen) == [1, 2, 3, 4, 5] and len(set(seen)) == 5
    assert db_session.query(Issue).count() == 5  # PR not stored
    assert gh.calls["/repos/o/r/issues"] == 1  # later pages served from the database


def test_list_state_filter_and_labels_shape(setup, auth, client, db_session):
    setup(routes([issue(1), issue(2, state="closed")]))
    client.get(REPO_URL + "/issues", headers=auth)
    allr = client.get(REPO_URL + "/issues?state=all", headers=auth).json()
    open_ = client.get(REPO_URL + "/issues", headers=auth).json()
    closed = client.get(REPO_URL + "/issues?state=closed", headers=auth).json()
    assert (allr["total"], open_["total"], closed["total"]) == (2, 1, 1)
    assert open_["items"][0]["labels"] == [{"name": "bug", "color": None, "description": None}]


def test_repeated_issue_sync_is_idempotent_and_updates(setup, auth, client, db_session):
    data = {"items": [issue(1, title="old"), issue(2)]}
    gh = setup({"/repos/o/r": ok(REPO), "/repos/o/r/issues": lambda r: ok(data["items"])})
    client.get(REPO_URL + "/issues", headers=auth)
    ids = sorted(db_session.scalars(select(Issue.id)))
    data["items"] = [issue(1, title="new"), issue(2), issue(3)]
    age_repo(db_session)
    body = client.get(REPO_URL + "/issues", headers=auth).json()
    assert body["total"] == 3
    assert sorted(db_session.scalars(select(Issue.id)))[:2] == ids
    assert {i["number"]: i["title"] for i in body["items"]}[1] == "new"
    age_repo(db_session)
    client.get(REPO_URL + "/issues", headers=auth)
    assert db_session.query(Issue).count() == 3


def test_truncation_is_reported(setup, auth, client):
    def page(r):
        p = int(r.url.params.get("page", "1"))
        link = {"link": f'<https://api.github.com/repos/o/r/issues?per_page=100&page={p + 1}>; rel="next"'}
        return ok([issue(p)], **link)

    setup({"/repos/o/r": ok(REPO), "/repos/o/r/issues": page},
          settings=Settings(auth_secret="s" * 48, github_issue_sync_max_pages=2, _env_file=None))
    body = client.get(REPO_URL + "/issues", headers=auth).json()
    assert body["sync_truncated"] is True and body["total"] == 2


def test_failed_issue_sync_leaves_database_unchanged(setup, auth, client, db_session):
    state = {"fail": False}
    setup({"/repos/o/r": ok(REPO),
           "/repos/o/r/issues": lambda r: status(503) if state["fail"] else ok([issue(1)])})
    client.get(REPO_URL + "/issues", headers=auth)
    age_repo(db_session)
    state["fail"] = True
    r = client.get(REPO_URL + "/issues", headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_unavailable"
    assert db_session.query(Issue).count() == 1


@pytest.mark.parametrize("qs", ["limit=0", "limit=101", "offset=-1", "state=bogus", "limit=abc"])
def test_list_validation(setup, auth, client, qs):
    gh = setup(routes())
    r = client.get(f"{REPO_URL}/issues?{qs}", headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert gh.total == 0


# ---------------------------------------------------------------- issue detail


def test_get_issue_success_persisted_and_cached(setup, auth, client, db_session):
    gh = setup({"/repos/o/r": ok(REPO), "/repos/o/r/issues/7": ok(issue(7, body="details", assignees=[{"login": "a"}]))})
    r = client.get(f"{ISSUE_URL}/7", headers=auth)
    assert r.status_code == 200
    b = r.json()
    assert (b["repository"], b["number"], b["body"], b["assignees"], b["author"]) == ("o/r", 7, "details", ["a"], "u")
    assert b["html_url"] == "https://github.com/o/r/issues/7"
    assert db_session.query(Issue).count() == 1
    client.get(f"{ISSUE_URL}/7", headers=auth)
    assert gh.total == 2  # one repo call + one issue call, nothing on the second read


def test_stale_issue_is_refetched_and_updated(setup, auth, client, db_session):
    state = {"title": "a"}
    gh = setup({"/repos/o/r": ok(REPO), "/repos/o/r/issues/7": lambda r: ok(issue(7, title=state["title"]))})
    client.get(f"{ISSUE_URL}/7", headers=auth)
    state["title"] = "b"
    age_repo(db_session)
    assert client.get(f"{ISSUE_URL}/7", headers=auth).json()["title"] == "b"
    assert db_session.query(Issue).count() == 1


def test_issue_not_found(setup, auth, client, db_session):
    setup({"/repos/o/r": ok(REPO)})
    r = client.get(f"{ISSUE_URL}/404", headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "issue_not_found"
    assert db_session.query(Issue).count() == 0


def test_pull_request_number_is_not_an_issue(setup, auth, client, db_session):
    setup({"/repos/o/r": ok(REPO), "/repos/o/r/issues/5": ok(issue(5, pr=True))})
    r = client.get(f"{ISSUE_URL}/5", headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_an_issue"
    assert db_session.query(Issue).count() == 0


def test_issue_for_missing_repository(setup, auth, client):
    setup({})
    assert client.get(f"{ISSUE_URL}/1", headers=auth).json()["error"]["code"] == "repository_not_found"


def test_concurrent_insert_race_is_tolerated(setup, auth, client, db_session, monkeypatch):
    """If another request inserts the row between our lookup and insert, we update instead of failing."""
    gh = setup(routes())
    client.get(REPO_URL, headers=auth)  # row now exists
    real = svc._find_repo
    calls = {"n": 0}

    def blind_first_time(db, owner, name):
        calls["n"] += 1
        return None if calls["n"] == 2 else real(db, owner, name)  # 1st call: stale check; 2nd: upsert lookup

    monkeypatch.setattr(svc, "_find_repo", blind_first_time)
    age_repo(db_session)
    assert client.get(REPO_URL, headers=auth).status_code == 200
    assert db_session.query(Repository).count() == 1


# ---------------------------------------------------------------- validation + auth


@pytest.mark.parametrize(
    "path",
    ["/api/v1/repositories/o%20x/r", "/api/v1/repositories/o/r$x", "/api/v1/repositories/../r",
     "/api/v1/issues/o/r/0", "/api/v1/issues/o/r/-1", "/api/v1/issues/o/r/abc", "/api/v1/issues/o/r/99999999999",
     "/api/v1/issues/%2E%2E/r/1", "/api/v1/repositories/o/%2E%2E/issues"],
)
def test_invalid_identifiers_rejected_without_github_calls(setup, auth, client, path):
    gh = setup(routes())
    r = client.get(path, headers=auth)
    assert r.status_code in (404, 422)
    assert gh.total == 0


@pytest.mark.parametrize("path", [REPO_URL, REPO_URL + "/issues", ISSUE_URL + "/1"])
def test_authentication_required(setup, client, path):
    gh = setup(routes())
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer junk"}).status_code == 401
    assert gh.total == 0


# ---------------------------------------------------------------- GitHub failures -> API errors


def failing(response):
    return {"/repos/o/r": response}


def test_rate_limit_maps_to_429_with_retry_after(setup, auth, client):
    reset = int(__import__("tests.github_mock", fromlist=["NOW"]).NOW) + 3600
    setup(failing(status(403, {"message": "API rate limit exceeded"},
                         **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})))
    r = client.get(REPO_URL, headers=auth)
    assert r.status_code == 429 and r.json()["error"]["code"] == "github_rate_limited"
    assert int(r.headers["retry-after"]) >= 3600
    assert r.json()["error"]["details"]["retry_after_seconds"] >= 3600


def test_upstream_5xx_maps_to_502(setup, auth, client):
    setup(failing(status(503)))
    r = client.get(REPO_URL, headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_unavailable"


def test_timeout_maps_to_504(setup, auth, client):
    setup(failing(httpx.ReadTimeout("slow")))
    r = client.get(REPO_URL, headers=auth)
    assert r.status_code == 504 and r.json()["error"]["code"] == "github_timeout"


def test_network_error_maps_to_502(setup, auth, client):
    setup(failing(httpx.ConnectError("refused")))
    assert client.get(REPO_URL, headers=auth).json()["error"]["code"] == "github_unavailable"


def test_server_credential_failure_maps_to_502_without_leaking(setup, auth, client):
    setup(failing(status(401, {"message": f"Bad credentials {TOKEN}"})))
    r = client.get(REPO_URL, headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_auth_failed"
    assert TOKEN not in r.text and "Bad credentials" not in r.text


def test_access_denied_maps_to_403(setup, auth, client):
    setup(failing(status(403, {"message": "Repository access blocked"})))
    r = client.get(REPO_URL, headers=auth)
    assert r.status_code == 403 and r.json()["error"]["code"] == "github_access_denied"


@pytest.mark.parametrize("response", [httpx.Response(200, text="<html>nope</html>"), ok([1, 2]),
                                      ok({"name": "r"})])
def test_malformed_upstream_maps_to_502(setup, auth, client, db_session, response):
    setup(failing(response))
    r = client.get(REPO_URL, headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_bad_response"
    assert db_session.query(Repository).count() == 0


def test_malformed_issue_payload_maps_to_502(setup, auth, client, db_session):
    setup({"/repos/o/r": ok(REPO), "/repos/o/r/issues/1": ok(issue(1, created_at="garbage"))})
    r = client.get(f"{ISSUE_URL}/1", headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_bad_response"
    assert db_session.query(Issue).count() == 0


def test_rejected_request_maps_to_502(setup, auth, client):
    setup(failing(status(422)))
    assert client.get(REPO_URL, headers=auth).json()["error"]["code"] == "github_request_rejected"


# ---------------------------------------------------------------- profile separation


def test_no_profile_is_valid_and_issue_data_has_no_match_fields(setup, auth, client):
    setup({**routes(), "/repos/o/r/issues/1": ok(issue(1))})
    assert client.get("/api/v1/profile", headers=auth).status_code == 404  # no profile exists
    for url in (REPO_URL, REPO_URL + "/issues", ISSUE_URL + "/1"):
        r = client.get(url, headers=auth)
        assert r.status_code == 200
        text = r.text.lower()
        for forbidden in ("match", "skill", "score", "proficiency", "recommend"):
            assert forbidden not in text
    assert client.get("/api/v1/profile", headers=auth).status_code == 404  # still no profile created


def test_repository_data_is_shared_not_per_user(setup, client, db_session):
    gh = setup(routes())
    users = []
    for gid, name in ((1, "a"), (2, "b")):
        u = User(github_user_id=gid, username=name)
        db_session.add(u)
        db_session.commit()
        users.append({"Authorization": f"Bearer {issue_token(u.id, SETTINGS)}"})
    client.get(REPO_URL, headers=users[0])
    client.get(REPO_URL, headers=users[1])
    assert gh.total == 1 and db_session.query(Repository).count() == 1
