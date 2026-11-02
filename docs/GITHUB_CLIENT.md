# GitHub API client

Code: `backend/app/services/github/` (`client.py`, `cache.py`, `models.py`, `errors.py`).
It is independent of profiles, recommendations and ML; callers get typed, validated objects.

**Status:** tested only against mocked HTTP responses. It has **not** been run against the live
GitHub API.

## Authentication
- Public data works unauthenticated (GitHub allows 60 requests/hour per IP).
- Set `COMPASS_GITHUB_TOKEN` for 5000 requests/hour. Use a fine-grained token with no permissions
  beyond public read. The token is read from the environment only (`SecretStr`), never committed.
- The token is sent only to the configured API origin (`COMPASS_GITHUB_API_URL`, must be `https`).
  Redirects and pagination links to any other origin are refused. It never appears in `repr`, logs,
  exceptions or API responses, and is never passed to the extension.
- GitHub App / OAuth user tokens are not implemented; the client accepts any bearer token string,
  so a per-user token can be injected later by constructing a client per request.

## Rate limits
- Every response's `x-ratelimit-*` headers are recorded (`client.last_rate_limit`).
- On 403/429 the client decides if it is a rate limit: `Retry-After` (seconds or HTTP date) wins;
  else `x-ratelimit-remaining: 0` with `x-ratelimit-reset`; else a "rate limit"/"abuse" message or
  429 (secondary limit, default wait 60 s as GitHub advises).
- It sleeps for at least the required time (jitter only adds delay) and retries, within the retry
  budget. If the required wait exceeds `COMPASS_GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS` (default 60) it
  raises `GitHubRateLimitError(retry_after_seconds=...)` immediately instead of blocking a worker.
- If the last response showed `remaining == 0`, later requests are not sent until the reset time
  (or raise the same error if it is too far away).
- A 403 with no rate-limit signal is `GitHubPermissionError` and is never retried.

## Retries
`COMPASS_GITHUB_MAX_RETRIES` (default 3 => up to 4 attempts) applies to timeouts, network errors,
500/502/503/504 and rate-limit waits. Delay = exponential backoff with equal jitter, in
`[d/2, d]` where `d = min(30 s, 0.5 s * 2^attempt)`; a `Retry-After` on 5xx is honoured.
401, 404/410, 422 and other 4xx are never retried.

## Caching
- Each successful response with an `ETag` is stored (in memory, LRU, 1000 entries by default).
- The next identical request sends `If-None-Match`; a `304` returns the cached body (and its
  pagination link). Because every use is revalidated with GitHub, stale data is not served.
  Trade-off: cached reads still cost a network round trip (304s do not count against the
  authenticated primary limit, but do count unauthenticated).
- The cache is behind the `ETagCache` protocol. A Redis implementation is **not written yet**; the
  in-memory cache is per process and lost on restart. Cache keys do not include the token, which
  is safe only because the client reads public data.

## Pagination and validation
- `list_*` methods follow `Link: rel="next"` up to `max_pages` (default 1-5 depending on method,
  100 items per page) and return `PagedResult(items, truncated, pages_fetched)`; `truncated=True`
  means more pages exist.
- `list_issues` removes pull requests (GitHub's issues endpoint returns both).
- Payloads are validated with Pydantic. Failures raise `GitHubResponseError` naming the failed
  fields only (never payload content). Optional data that is absent stays `None` (unknown).
- Owner/repo names and issue numbers are validated before any request is made.

## Errors
`GitHubError` base; `GitHubAuthError` (401), `GitHubPermissionError`, `GitHubNotFoundError`,
`GitHubRequestError`, `GitHubRateLimitError`, `GitHubServerError`, `GitHubTimeoutError`,
`GitHubConnectionError`, `GitHubResponseError`. Mapping these to API responses happens in the
service/API layer in a later stage.

## Usage
```python
from app.core.config import get_settings
from app.services.github import create_github_client

with create_github_client(get_settings()) as gh:
    repo = gh.get_repository("pallets", "flask")
    issues = gh.list_issues("pallets", "flask", labels=["good first issue"], max_pages=1)
```

## Running the tests
```
cd backend
.venv\Scripts\python -m pytest -q tests/test_github_client.py     # Windows
python -m pytest -q tests/test_github_client.py                   # with the venv active
```
Tests use `httpx.MockTransport` with injected clock/sleep/RNG: no network, no GitHub account, no
real waiting.

## Known limitations
- Not exercised against live GitHub; real-world header/payload quirks may surface.
- Synchronous client (matches the current sync services); no connection-level concurrency limits.
- No GraphQL, no file-content/README/tree fetching, no timeline/linked-PR endpoint yet (Stage 7).
- Rate-limit state is per client instance, not shared across processes.
