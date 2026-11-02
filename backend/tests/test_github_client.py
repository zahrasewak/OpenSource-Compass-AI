"""GitHub client tests. All HTTP is mocked with httpx.MockTransport; no network is used."""

import json
import logging
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.github import GitHubClient, InMemoryETagCache, create_github_client
from app.services.github.errors import (
    GitHubAuthError,
    GitHubConnectionError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GitHubRequestError,
    GitHubResponseError,
    GitHubServerError,
    GitHubTimeoutError,
)

from tests.github_mock import NOW, REPO, TOKEN, Script, issue, ok, status  # noqa: E402


# ---------------------------------------------------------------- success + models


def test_get_repository_and_headers():
    s = Script(ok(REPO))
    repo = s.client().get_repository("o", "r")
    assert (repo.owner, repo.name, repo.license_spdx, repo.language) == ("o", "r", "MIT", "Python")
    assert repo.pushed_at == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    req = s.requests[0]
    assert req.url.path == "/repos/o/r"
    assert req.headers["accept"] == "application/vnd.github+json"
    assert req.headers["x-github-api-version"] == "2022-11-28"
    assert "authorization" not in req.headers  # unauthenticated by default


def test_authenticated_header_and_repr_hides_token():
    s = Script(ok(REPO))
    c = s.client(token=SecretStr(TOKEN))
    c.get_repository("o", "r")
    assert s.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in repr(c) and c.authenticated


def test_empty_token_means_unauthenticated():
    s = Script(ok(REPO))
    c = s.client(token=SecretStr(""))
    c.get_repository("o", "r")
    assert not c.authenticated and "authorization" not in s.requests[0].headers


def test_get_issue_parses_fields_and_unknown_body():
    s = Script(ok(issue(7, assignees=[{"login": "a"}], body="hello")))
    i = s.client().get_issue("o", "r", 7)
    assert (i.number, i.body, i.author, i.assignees, i.is_pull_request) == (7, "hello", "u", ["a"], False)
    assert [l.name for l in i.labels] == ["bug"]
    assert s.requests[0].url.path == "/repos/o/r/issues/7"


def test_languages_labels_comments_events_prs_commits_releases():
    s = Script(
        ok({"Python": 100, "Go": 5}),
        ok([{"name": "good first issue", "color": "fff"}]),
        ok([{"id": 1, "user": {"login": "m"}, "body": "hi", "created_at": "2024-01-01T00:00:00Z",
             "author_association": "MEMBER"}]),
        ok([{"id": 2, "event": "labeled", "actor": {"login": "m"}, "created_at": "2024-01-01T00:00:00Z"}]),
        ok([{"number": 3, "title": "p", "state": "closed", "user": {"login": "x"},
             "created_at": "2024-01-01T00:00:00Z", "merged_at": "2024-01-02T00:00:00Z"}]),
        ok([{"sha": "a" * 40, "author": None, "commit": {"committer": {"date": "2024-02-01T00:00:00Z"}}}]),
        ok([{"tag_name": "v1", "published_at": "2024-03-01T00:00:00Z"}]),
    )
    c = s.client()
    assert c.get_languages("o", "r") == {"Python": 100, "Go": 5}
    assert c.list_labels("o", "r").items[0].name == "good first issue"
    assert c.list_issue_comments("o", "r", 1).items[0].author == "m"
    assert c.list_issue_events("o", "r", 1).items[0].event == "labeled"
    assert c.list_pull_requests("o", "r").items[0].merged_at is not None
    commit = c.list_commits("o", "r", since=datetime(2024, 1, 1, tzinfo=timezone.utc)).items[0]
    assert commit.sha == "a" * 40 and commit.author is None and commit.committed_at.year == 2024
    assert c.list_releases("o", "r").items[0].tag_name == "v1"
    assert s.requests[5].url.params["since"] == "2024-01-01T00:00:00Z"


def test_list_issues_filters_pull_requests_and_sends_params():
    s = Script(ok([issue(1), issue(2, pr=True), issue(3)]))
    res = s.client().list_issues("o", "r", labels=["good first issue", "bug"])
    assert [i.number for i in res.items] == [1, 3]
    p = s.requests[0].url.params
    assert p["labels"] == "good first issue,bug" and p["state"] == "open" and p["per_page"] == "100"


# ---------------------------------------------------------------- pagination


def _page_handler(pages_total):
    def h(request):
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page < pages_total:
            headers["link"] = f'<https://api.github.com/repos/o/r/issues?per_page=100&page={page + 1}>; rel="next", <x>; rel="last"'
        return ok([issue(page * 10 + k) for k in range(2)], **headers)
    return h


def test_pagination_follows_links():
    s = Script(_page_handler(3))
    res = s.client().list_issues("o", "r", max_pages=5)
    assert [i.number for i in res.items] == [10, 11, 20, 21, 30, 31]
    assert res.pages_fetched == 3 and not res.truncated
    assert len(s.requests) == 3


def test_pagination_is_bounded_and_flags_truncation():
    s = Script(_page_handler(10))
    res = s.client().list_issues("o", "r", max_pages=2)
    assert res.pages_fetched == 2 and res.truncated and len(s.requests) == 2


def test_pagination_refuses_other_origin_and_never_sends_token_there():
    seen_hosts = []

    def h(request):
        seen_hosts.append(request.url.host)
        return ok([issue(1)], link='<https://evil.example.com/steal?page=2>; rel="next"')

    s = Script(h)
    with pytest.raises(GitHubResponseError, match="different origin"):
        s.client(token=TOKEN).list_issues("o", "r", max_pages=5)
    assert seen_hosts == ["api.github.com"]


def test_array_expected_for_list_endpoint():
    with pytest.raises(GitHubResponseError):
        Script(ok({"not": "a list"})).client().list_issues("o", "r")


# ---------------------------------------------------------------- ETag / 304


def test_etag_stored_then_conditional_request_and_304_served_from_cache():
    def second(request):
        assert request.headers["if-none-match"] == '"abc"'
        return httpx.Response(304)

    s = Script(ok(REPO, etag='"abc"'), second)
    c = s.client()
    first = c.get_repository("o", "r")
    again = c.get_repository("o", "r")
    assert first == again and again.full_name == "o/r"
    assert "if-none-match" not in s.requests[0].headers


def test_changed_resource_replaces_cache_entry():
    new = {**REPO, "description": "new"}
    s = Script(ok(REPO, etag='"1"'), ok(new, etag='"2"'), lambda r: httpx.Response(304) if r.headers.get("if-none-match") == '"2"' else status(500))
    c = s.client()
    assert c.get_repository("o", "r").description is None
    assert c.get_repository("o", "r").description == "new"
    assert c.get_repository("o", "r").description == "new"


def test_304_preserves_pagination_link_from_cache():
    link = '<https://api.github.com/repos/o/r/issues?per_page=100&page=2>; rel="next"'

    def h(request):
        page = request.url.params.get("page", "1")
        conditional = "if-none-match" in request.headers
        if page == "1":
            return httpx.Response(304) if conditional else ok([issue(1)], etag='"p1"', link=link)
        return httpx.Response(304) if conditional else ok([issue(2)], etag='"p2"')

    s = Script(h)
    c = s.client()
    assert [i.number for i in c.list_issues("o", "r").items] == [1, 2]
    again = c.list_issues("o", "r")
    assert [i.number for i in again.items] == [1, 2]
    assert len(s.requests) == 4


def test_304_without_cache_entry_is_an_error():
    with pytest.raises(GitHubResponseError, match="304"):
        Script(httpx.Response(304)).client().get_repository("o", "r")


def test_cached_data_cannot_be_mutated_by_callers():
    cache = InMemoryETagCache()
    s = Script(ok({"Python": 1}, etag='"l"'), httpx.Response(304))
    c = s.client(cache=cache)
    c.get_languages("o", "r")["Python"] = 999
    assert c.get_languages("o", "r") == {"Python": 1}


def test_cache_lru_eviction():
    from app.services.github.cache import CacheEntry
    cache = InMemoryETagCache(max_entries=2)
    for k in "abc":
        cache.set(k, CacheEntry("e", k))
    assert cache.get("a") is None and cache.get("b") and cache.get("c") and len(cache) == 2


# ---------------------------------------------------------------- rate limits


def test_primary_rate_limit_waits_until_reset_then_succeeds():
    reset = int(NOW) + 30
    s = Script(status(403, {"message": "API rate limit exceeded"}, **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}), ok(REPO))
    s.client(rng=lambda: 0.0).get_repository("o", "r")
    assert s.sleeps == [31.0]  # reset - now + 1; never earlier than the reset time
    assert len(s.requests) == 2


def test_rate_limit_wait_never_below_required_even_with_jitter():
    s = Script(status(429, **{"retry-after": "10"}), ok(REPO))
    s.client(rng=lambda: 0.99).get_repository("o", "r")
    assert s.sleeps[0] >= 10


def test_retry_after_seconds_honoured_for_secondary_limit():
    s = Script(status(403, {"message": "You have exceeded a secondary rate limit"}, **{"retry-after": "7"}), ok(REPO))
    s.client().get_repository("o", "r")
    assert s.sleeps == [7.0]


def test_retry_after_http_date():
    from email.utils import formatdate
    s = Script(status(429, **{"retry-after": formatdate(NOW + 20, usegmt=True)}), ok(REPO))
    s.client().get_repository("o", "r")
    assert s.sleeps[0] == pytest.approx(20.0, abs=1.0)


def test_secondary_limit_without_retry_after_uses_default_wait():
    s = Script(status(403, {"message": "You have exceeded a secondary rate limit."}), ok(REPO))
    s.client(max_rate_limit_wait=120).get_repository("o", "r")
    assert s.sleeps == [60.0]


def test_wait_longer_than_bound_raises_without_sleeping():
    reset = int(NOW) + 3600
    s = Script(status(403, {"message": "API rate limit exceeded"}, **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}))
    with pytest.raises(GitHubRateLimitError) as ei:
        s.client(max_rate_limit_wait=60).get_repository("o", "r")
    assert s.sleeps == [] and len(s.requests) == 1
    assert ei.value.retry_after_seconds == pytest.approx(3601)


def test_known_exhaustion_blocks_next_request_without_calling_github():
    reset = int(NOW) + 3600
    s = Script(ok(REPO, **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset), "x-ratelimit-limit": "60"}))
    c = s.client()
    c.get_repository("o", "r")
    assert c.last_rate_limit.remaining == 0 and c.last_rate_limit.limit == 60
    with pytest.raises(GitHubRateLimitError):
        c.get_repository("o", "r")
    assert len(s.requests) == 1


def test_known_exhaustion_waits_when_reset_is_near():
    reset = int(NOW) + 5
    s = Script(ok(REPO, **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}), ok(REPO))
    c = s.client()
    c.get_repository("o", "r")
    c.get_repository("o", "r")
    assert s.sleeps == [6.0]


def test_rate_limit_retries_are_bounded():
    s = Script(status(429, **{"retry-after": "1"}))
    with pytest.raises(GitHubRateLimitError):
        s.client(max_retries=2).get_repository("o", "r")
    assert len(s.requests) == 3 and s.sleeps == [1.0, 1.0]


def test_plain_403_is_permission_error_not_retried():
    s = Script(status(403, {"message": "Resource not accessible"}))
    with pytest.raises(GitHubPermissionError):
        s.client().get_repository("o", "r")
    assert len(s.requests) == 1 and s.sleeps == []


# ---------------------------------------------------------------- transient errors + retry limits


def test_backoff_is_exponential_with_jitter_bounds():
    s = Script(status(503), status(502), status(500), ok(REPO))
    s.client(rng=lambda: 0.0, backoff_base=1.0, max_retries=3).get_repository("o", "r")
    assert s.sleeps == [0.5, 1.0, 2.0]  # equal jitter with rng=0 -> d/2 where d = 1,2,4
    s2 = Script(status(503), status(503), ok(REPO))
    s2.client(rng=lambda: 1.0, backoff_base=1.0).get_repository("o", "r")
    assert s2.sleeps == [1.0, 2.0]


def test_backoff_capped_at_max():
    s = Script(status(500), status(500), status(500), status(500), ok(REPO))
    s.client(rng=lambda: 1.0, backoff_base=10, backoff_max=15, max_retries=4).get_repository("o", "r")
    assert max(s.sleeps) == 15


def test_server_error_after_max_retries():
    s = Script(status(503))
    with pytest.raises(GitHubServerError):
        s.client(max_retries=2).get_repository("o", "r")
    assert len(s.requests) == 3 and len(s.sleeps) == 2


def test_zero_retries_means_single_attempt():
    s = Script(status(500))
    with pytest.raises(GitHubServerError):
        s.client(max_retries=0).get_repository("o", "r")
    assert len(s.requests) == 1 and s.sleeps == []


def test_503_retry_after_is_honoured():
    s = Script(status(503, **{"retry-after": "4"}), ok(REPO))
    s.client().get_repository("o", "r")
    assert s.sleeps == [4.0]


def test_timeout_retried_then_succeeds():
    s = Script(httpx.ReadTimeout("slow"), ok(REPO))
    s.client().get_repository("o", "r")
    assert len(s.requests) == 2 and len(s.sleeps) == 1


def test_timeout_exhausted():
    s = Script(httpx.ConnectTimeout("slow"))
    with pytest.raises(GitHubTimeoutError):
        s.client(max_retries=2).get_repository("o", "r")
    assert len(s.requests) == 3


def test_connection_error_exhausted():
    s = Script(httpx.ConnectError("refused"))
    with pytest.raises(GitHubConnectionError):
        s.client(max_retries=1).get_repository("o", "r")
    assert len(s.requests) == 2


@pytest.mark.parametrize(
    "code,exc",
    [(401, GitHubAuthError), (404, GitHubNotFoundError), (410, GitHubNotFoundError),
     (422, GitHubRequestError), (400, GitHubRequestError)],
)
def test_permanent_errors_not_retried(code, exc):
    s = Script(status(code))
    with pytest.raises(exc):
        s.client().get_repository("o", "r")
    assert len(s.requests) == 1 and s.sleeps == []


# ---------------------------------------------------------------- malformed responses


def test_non_json_body():
    with pytest.raises(GitHubResponseError, match="non-JSON"):
        Script(httpx.Response(200, text="<html>oops</html>")).client().get_repository("o", "r")


def test_wrong_top_level_type():
    with pytest.raises(GitHubResponseError):
        Script(ok([1, 2])).client().get_repository("o", "r")


def test_missing_required_field_reports_field_not_payload():
    bad = {**REPO}
    del bad["default_branch"]
    bad["description"] = "SENSITIVE-PAYLOAD-TEXT"
    with pytest.raises(GitHubResponseError) as ei:
        Script(ok(bad)).client().get_repository("o", "r")
    assert "default_branch" in str(ei.value) and "SENSITIVE" not in str(ei.value)


def test_bad_date_in_issue():
    with pytest.raises(GitHubResponseError):
        Script(ok(issue(1, created_at="not-a-date"))).client().get_issue("o", "r", 1)


def test_languages_bad_shape():
    with pytest.raises(GitHubResponseError):
        Script(ok({"Python": "lots"})).client().get_languages("o", "r")


def test_error_response_with_non_json_body_is_handled():
    s = Script(httpx.Response(403, text="forbidden"))
    with pytest.raises(GitHubPermissionError):
        s.client().get_repository("o", "r")


# ---------------------------------------------------------------- input validation + security


@pytest.mark.parametrize("owner,repo", [("..", "r"), ("o", "../x"), ("o/x", "r"), ("", "r"), ("o", "r?x=1"), ("o", "a b")])
def test_invalid_owner_or_repo_rejected_before_any_request(owner, repo):
    s = Script(ok(REPO))
    with pytest.raises(ValueError):
        s.client().get_repository(owner, repo)
    assert s.requests == []


@pytest.mark.parametrize("n", [0, -1, True, "1"])
def test_invalid_issue_number(n):
    with pytest.raises(ValueError):
        Script(ok({})).client().get_issue("o", "r", n)


def test_https_required():
    with pytest.raises(ValueError, match="https"):
        GitHubClient(base_url="http://api.github.com")


def test_redirect_same_origin_followed_cross_origin_refused():
    s = Script(httpx.Response(301, headers={"location": "https://api.github.com/repositories/1"}), ok(REPO))
    s.client().get_repository("o", "r")
    assert s.requests[1].url.path == "/repositories/1"

    seen = []
    def h(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example.com/"})
    with pytest.raises(GitHubResponseError):
        Script(h).client(token=TOKEN).get_repository("o", "r")
    assert seen == ["api.github.com"]


def test_token_not_in_logs_or_errors(caplog):
    s = Script(status(503), status(401, {"message": f"bad {TOKEN}"}))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GitHubAuthError) as ei:
            s.client(token=TOKEN).get_repository("o", "r")
    assert TOKEN not in caplog.text and TOKEN not in str(ei.value) and TOKEN not in repr(ei.value)


def test_get_rate_limit():
    s = Script(ok({"resources": {"core": {"limit": 60, "remaining": 59, "reset": 1700000600}}}))
    rl = s.client().get_rate_limit()
    assert (rl.limit, rl.remaining, rl.reset_epoch) == (60, 59, 1700000600)


def test_get_rate_limit_malformed():
    with pytest.raises(GitHubResponseError):
        Script(ok({"resources": {}})).client().get_rate_limit()


# ---------------------------------------------------------------- configuration


def test_factory_reads_settings_and_token_stays_secret():
    settings = Settings(github_token=TOKEN, github_max_retries=1, github_timeout_seconds=3, _env_file=None)
    assert TOKEN not in repr(settings) and TOKEN not in str(settings.model_dump())
    c = create_github_client(settings)
    assert c.authenticated and TOKEN not in repr(c)
    c.close()


def test_factory_unauthenticated_by_default():
    assert not create_github_client(Settings(_env_file=None)).authenticated
