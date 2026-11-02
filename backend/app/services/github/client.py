"""Synchronous, rate-limit-aware GitHub REST client for public data.

Behaviour summary (see docs/GITHUB_CLIENT.md):
  * conditional requests with ETag / If-None-Match; 304 is served from the cache
  * primary and secondary rate limits honoured via Retry-After / x-ratelimit-reset; the client
    never retries before the permitted time and refuses to block longer than a configured bound
  * bounded retries with exponential backoff + jitter for timeouts, network errors and 5xx
  * permanent errors (401, 403 non-rate-limit, 404, 422...) are never retried
  * pagination follows Link headers only to the same origin and is bounded
  * the token is only ever sent to the configured API origin and is never logged or put in errors
"""

import base64
import logging
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, TypeVar
from urllib.parse import quote

import httpx
from pydantic import SecretStr, ValidationError

from app.core import requestctx
from app.services.github.cache import CacheEntry, ETagCache, InMemoryETagCache
from app.services.github.errors import (
    GitHubAuthError,
    GitHubConnectionError,
    GitHubDeadlineError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GitHubRequestError,
    GitHubResponseError,
    GitHubServerError,
    GitHubTimeoutError,
)
from app.services.github.models import (
    FileText,
    GitTree,
    TimelineReference,
    CommitSummary,
    Issue,
    IssueComment,
    IssueEvent,
    Label,
    PagedResult,
    PullRequestSummary,
    RateLimit,
    Release,
    Repository,
)

logger = logging.getLogger("compass.github")

T = TypeVar("T")

API_VERSION = "2022-11-28"
RETRYABLE_SERVER_STATUSES = {500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 307, 308}
MAX_REDIRECTS = 3
SECONDARY_LIMIT_DEFAULT_WAIT = 60.0  # GitHub: wait at least a minute when no Retry-After is given
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/@-]{1,255}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._ +@~-]{1,255}$")
_NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _check_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value) or value in {".", ".."}:
        raise ValueError(f"invalid {what}")
    return value


def _parse_retry_after(value: str | None, now: float) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - now)


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, ValueError):
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Page:
    __slots__ = ("data", "next_url")

    def __init__(self, data: Any, next_url: str | None) -> None:
        self.data = data
        self.next_url = next_url


class GitHubClient:
    """Reusable GitHub client. Create once, reuse, and call `close()` when done."""

    def __init__(
        self,
        token: SecretStr | str | None = None,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        max_rate_limit_wait: float = 60.0,
        cache: ETagCache | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        user_agent: str = "opensource-compass-ai",
    ) -> None:
        base = httpx.URL(base_url)
        if base.scheme != "https":
            raise ValueError("GitHub base_url must use https so credentials are never sent in clear text")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._base = base
        self._max_retries = max_retries
        self._timeout = timeout
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._max_rate_limit_wait = max_rate_limit_wait
        self._cache: ETagCache = cache if cache is not None else InMemoryETagCache()
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._rate: RateLimit | None = None
        self._requests_made = 0

        if isinstance(token, str):
            token = SecretStr(token)
        self._authenticated = bool(token and token.get_secret_value())
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": user_agent,
        }
        if self._authenticated:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"  # type: ignore[union-attr]
        self._http = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=False,  # redirects are followed manually, same-origin only
        )

    def __repr__(self) -> str:  # never include the token
        return f"GitHubClient(base_url={str(self._base)!r}, authenticated={self._authenticated})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def requests_made(self) -> int:
        """HTTP requests sent by this client instance (including retries), for audit records."""
        return self._requests_made

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def last_rate_limit(self) -> RateLimit | None:
        """Most recent rate-limit headers seen (None before the first response)."""
        return self._rate

    # ------------------------------------------------------------------ public API

    def get_rate_limit(self) -> RateLimit:
        data = self._get_object("/rate_limit", None)
        core = (data.get("resources") or {}).get("core")
        if not isinstance(core, dict):
            raise GitHubResponseError("rate_limit response missing resources.core")
        return RateLimit(limit=core.get("limit"), remaining=core.get("remaining"), reset_epoch=core.get("reset"))

    def get_repository(self, owner: str, repo: str) -> Repository:
        return self._model(Repository, self._get_object(self._repo(owner, repo), None))

    def get_languages(self, owner: str, repo: str) -> dict[str, int]:
        data = self._get_object(f"{self._repo(owner, repo)}/languages", None)
        if not all(isinstance(k, str) and isinstance(v, int) for k, v in data.items()):
            raise GitHubResponseError("languages response has unexpected shape")
        return data

    def list_labels(self, owner: str, repo: str, *, max_pages: int = 5) -> PagedResult[Label]:
        return self._paged_models(f"{self._repo(owner, repo)}/labels", None, Label, max_pages)

    def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        return self._model(Issue, self._get_object(f"{self._repo(owner, repo)}/issues/{self._number(number)}", None))

    def list_issues(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        since: datetime | None = None,
        max_pages: int = 3,
    ) -> PagedResult[Issue]:
        """Issues only: pull requests (which the issues endpoint also returns) are filtered out."""
        if state not in {"open", "closed", "all"}:
            raise ValueError("state must be open, closed or all")
        params: dict[str, str] = {"state": state, "sort": "updated", "direction": "desc"}
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = _iso(since)
        raw = self._paged(f"{self._repo(owner, repo)}/issues", params, max_pages)
        issues = [self._model(Issue, item) for item in raw.items]
        return PagedResult[Issue](
            items=[i for i in issues if not i.is_pull_request],
            truncated=raw.truncated,
            pages_fetched=raw.pages_fetched,
        )

    def list_issue_comments(self, owner: str, repo: str, number: int, *, max_pages: int = 3) -> PagedResult[IssueComment]:
        path = f"{self._repo(owner, repo)}/issues/{self._number(number)}/comments"
        return self._paged_models(path, None, IssueComment, max_pages)

    def list_issue_events(self, owner: str, repo: str, number: int, *, max_pages: int = 3) -> PagedResult[IssueEvent]:
        path = f"{self._repo(owner, repo)}/issues/{self._number(number)}/events"
        return self._paged_models(path, None, IssueEvent, max_pages)

    def list_pull_requests(
        self, owner: str, repo: str, *, state: str = "all", max_pages: int = 3
    ) -> PagedResult[PullRequestSummary]:
        if state not in {"open", "closed", "all"}:
            raise ValueError("state must be open, closed or all")
        params = {"state": state, "sort": "updated", "direction": "desc"}
        return self._paged_models(f"{self._repo(owner, repo)}/pulls", params, PullRequestSummary, max_pages)

    def list_commits(
        self, owner: str, repo: str, *, since: datetime | None = None, max_pages: int = 3
    ) -> PagedResult[CommitSummary]:
        params = {"since": _iso(since)} if since else None
        return self._paged_models(f"{self._repo(owner, repo)}/commits", params, CommitSummary, max_pages)

    def list_releases(self, owner: str, repo: str, *, max_pages: int = 1) -> PagedResult[Release]:
        return self._paged_models(f"{self._repo(owner, repo)}/releases", None, Release, max_pages)

    def list_repo_issue_comments(
        self, owner: str, repo: str, *, since: datetime, max_pages: int = 3
    ) -> PagedResult[IssueComment]:
        """Comments on issues AND pull-request conversations repository-wide (oldest first).

        One paginated call replaces a request per issue. `since` filters on update time, so callers
        must still check created_at. Formal PR reviews are a different endpoint and are not included.
        """
        params = {"sort": "created", "direction": "asc", "since": _iso(since)}
        return self._paged_models(f"{self._repo(owner, repo)}/issues/comments", params, IssueComment, max_pages)

    def get_git_tree(self, owner: str, repo: str, ref: str) -> GitTree:
        """Recursive file listing in ONE request. `ref` should be a commit SHA to pin the analysis."""
        if not _REF_RE.match(ref) or ".." in ref or ref.startswith("/"):
            raise ValueError("invalid git ref")
        data = self._get_object(f"{self._repo(owner, repo)}/git/trees/{quote(ref, safe='')}", {"recursive": "1"})
        return self._model(GitTree, data)

    def get_file_text(self, owner: str, repo: str, path: str, *, ref: str | None = None,
                      max_bytes: int = 200_000) -> FileText:
        """Decoded text of one small file; `text` is None if it exceeds `max_bytes`. Never executes anything."""
        segments = path.split("/")
        if not path or any(s in {"", ".", ".."} or not _PATH_SEGMENT_RE.match(s) for s in segments):
            raise ValueError("invalid file path")
        params = {"ref": ref} if ref else None
        if ref is not None and (not _REF_RE.match(ref) or ".." in ref):
            raise ValueError("invalid git ref")
        data = self._get_object(
            f"{self._repo(owner, repo)}/contents/{'/'.join(quote(s, safe='') for s in segments)}", params)
        if data.get("type") != "file":
            raise GitHubResponseError("path is not a file")
        size = data.get("size")
        if not isinstance(size, int):
            raise GitHubResponseError("file response missing size")
        if size > max_bytes:
            return FileText(path=path, size=size, text=None)
        content = data.get("content")
        if data.get("encoding") != "base64" or not isinstance(content, str):
            raise GitHubResponseError("unsupported file encoding")
        try:
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        except ValueError as exc:
            raise GitHubResponseError("file content is not valid base64") from exc
        return FileText(path=path, size=size, text=text)

    def list_issue_timeline(
        self, owner: str, repo: str, number: int, *, max_pages: int = 3
    ) -> PagedResult[TimelineReference]:
        """Cross-references to this issue from other issues/PRs (the only timeline events used)."""
        raw = self._paged(f"{self._repo(owner, repo)}/issues/{self._number(number)}/timeline", None, max_pages)
        refs: list[TimelineReference] = []
        for item in raw.items:
            if not isinstance(item, dict):
                raise GitHubResponseError("timeline entry is not an object")
            if item.get("event") != "cross-referenced":
                continue
            source = (item.get("source") or {}).get("issue")
            if not isinstance(source, dict):
                continue
            pull = source.get("pull_request")
            repo_info = source.get("repository") if isinstance(source.get("repository"), dict) else {}
            refs.append(self._model(TimelineReference, {
                "source_number": source.get("number"),
                "source_is_pull_request": isinstance(pull, dict),
                "source_state": source.get("state"),
                "source_merged_at": pull.get("merged_at") if isinstance(pull, dict) else None,
                "source_repository": repo_info.get("full_name"),
                "source_html_url": source.get("html_url"),
                "created_at": item.get("created_at"),
            }))
        return PagedResult[TimelineReference](items=refs, truncated=raw.truncated, pages_fetched=raw.pages_fetched)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _repo(owner: str, repo: str) -> str:
        return f"/repos/{_check_name(owner, 'owner')}/{_check_name(repo, 'repository name')}"

    @staticmethod
    def _number(number: int) -> int:
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("issue number must be a positive integer")
        return number

    @staticmethod
    def _model(cls: type[T], data: Any) -> T:
        try:
            return cls.model_validate(data)  # type: ignore[attr-defined]
        except ValidationError as exc:
            # Report field locations only, never payload content.
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise GitHubResponseError(f"{cls.__name__} failed validation (fields: {', '.join(fields)})") from exc

    def _url(self, path: str, params: dict[str, str] | None) -> httpx.URL:
        url = self._base.copy_with(path=self._base.path.rstrip("/") + path)
        if params:
            url = url.copy_with(params=sorted(params.items()))  # sorted => stable cache keys
        return url

    def _get_object(self, path: str, params: dict[str, str] | None) -> dict:
        page = self._get(self._url(path, params))
        if not isinstance(page.data, dict):
            raise GitHubResponseError("expected a JSON object")
        return page.data

    def _paged(self, path: str, params: dict[str, str] | None, max_pages: int) -> PagedResult[Any]:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        params = {**(params or {}), "per_page": "100"}
        url: httpx.URL | None = self._url(path, params)
        items: list[Any] = []
        pages = 0
        truncated = False
        while url is not None:
            if pages >= max_pages:
                truncated = True
                break
            page = self._get(url)
            if not isinstance(page.data, list):
                raise GitHubResponseError("expected a JSON array")
            items.extend(page.data)
            pages += 1
            url = self._next_url(page.next_url)
        return PagedResult[Any](items=items, truncated=truncated, pages_fetched=pages)

    def _paged_models(self, path: str, params: dict[str, str] | None, cls: type[T], max_pages: int) -> PagedResult[T]:
        raw = self._paged(path, params, max_pages)
        return PagedResult[cls](  # type: ignore[valid-type]
            items=[self._model(cls, i) for i in raw.items], truncated=raw.truncated, pages_fetched=raw.pages_fetched
        )

    def _same_origin(self, url: httpx.URL) -> bool:
        return (url.scheme, url.host, url.port) == (self._base.scheme, self._base.host, self._base.port)

    def _next_url(self, link_target: str | None) -> httpx.URL | None:
        if not link_target:
            return None
        url = httpx.URL(link_target)
        if not self._same_origin(url):  # would leak the token to another host
            raise GitHubResponseError("pagination link points to a different origin; refusing to follow")
        return url

    # ------------------------------------------------------------------ transport + retry

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with equal jitter: in [d/2, d], d = min(max, base * 2**attempt)."""
        d = min(self._backoff_max, self._backoff_base * (2**attempt))
        return d / 2 + self._rng() * d / 2

    def _checkpoint(self) -> float | None:
        """Stop work nobody is waiting for. Called before every outbound request and while waiting.

        Raises RequestCancelled if the client disconnected (and nobody else shares this work), GitHubDeadlineError if the
        request's time budget is spent. Returns the seconds of budget left (None when outside a request or unbounded).
        """
        ctx = requestctx.current()
        if ctx is None:
            return None
        if ctx.cancelled():
            raise requestctx.RequestCancelled()
        remaining = ctx.remaining()
        if remaining is not None and remaining <= 0:
            raise GitHubDeadlineError("the request's time budget is exhausted")
        return remaining

    def _wait(self, seconds: float, reason: str, path: str, attempt: int) -> None:
        remaining = self._checkpoint()
        if remaining is not None and seconds >= remaining:
            # Sleeping would use up the whole budget and then fail anyway: fail now, without waiting.
            logger.warning("github retry abandoned: reason=%s path=%s wait=%.1fs exceeds the time left (%.1fs)", reason, path, seconds, remaining)
            raise GitHubDeadlineError("the request's time budget is too small for the required wait")
        logger.warning("github retry: reason=%s path=%s attempt=%d wait=%.1fs", reason, path, attempt + 1, seconds)
        if requestctx.current() is None:
            self._sleep(seconds)
            return
        left = seconds  # inside a request: sleep in short slices so a disconnect is noticed within ~0.25 s
        while left > 0:
            step = min(0.25, left)
            self._sleep(step)
            left -= step
            self._checkpoint()

    def _send(self, url: httpx.URL, etag: str | None, remaining: float | None = None) -> httpx.Response:
        headers = {"If-None-Match": etag} if etag else {}
        # Inside a request the per-call timeout never exceeds the time budget left.
        extra = {} if remaining is None else {"timeout": max(0.05, min(self._timeout, remaining))}
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self._requests_made += 1
            resp = self._http.get(current, headers=headers, **extra)
            if resp.status_code not in REDIRECT_STATUSES:
                return resp
            location = resp.headers.get("location")
            if not location:
                raise GitHubResponseError("redirect without Location header")
            target = current.join(location)
            if not self._same_origin(target):
                raise GitHubResponseError("redirect to a different origin; refusing to follow")
            current = target
        raise GitHubResponseError("too many redirects")

    def _record_rate_limit(self, resp: httpx.Response) -> None:
        h = resp.headers
        if "x-ratelimit-remaining" in h or "x-ratelimit-limit" in h:
            self._rate = RateLimit(
                limit=_int_header(h, "x-ratelimit-limit"),
                remaining=_int_header(h, "x-ratelimit-remaining"),
                reset_epoch=_int_header(h, "x-ratelimit-reset"),
            )

    def _guard_known_exhaustion(self, path: str) -> None:
        """Avoid sending requests we know will be refused: wait for (or refuse until) the reset time."""
        rate = self._rate
        if rate and rate.remaining == 0 and rate.reset_epoch:
            wait = rate.reset_epoch - self._clock()
            if wait > 0:
                wait += 1.0
                if wait > self._max_rate_limit_wait:
                    raise GitHubRateLimitError("GitHub rate limit exhausted; retry after reset", wait)
                self._wait(wait, "primary_rate_limit_known", path, 0)

    def _rate_limit_wait(self, resp: httpx.Response) -> float | None:
        """Seconds we must wait if this 403/429 is a rate limit; None if it is not one."""
        now = self._clock()
        retry_after = _parse_retry_after(resp.headers.get("retry-after"), now)
        if retry_after is not None:
            return retry_after
        remaining = _int_header(resp.headers, "x-ratelimit-remaining")
        reset = _int_header(resp.headers, "x-ratelimit-reset")
        if remaining == 0 and reset is not None:
            return max(0.0, reset - now) + 1.0
        message = _error_message(resp).lower()
        if resp.status_code == 429 or "rate limit" in message or "abuse" in message:
            return SECONDARY_LIMIT_DEFAULT_WAIT
        return None

    def _get(self, url: httpx.URL) -> _Page:
        path = url.path
        key = str(url)
        cached = self._cache.get(key)
        etag = cached.etag if cached else None
        last_attempt = self._max_retries

        for attempt in range(self._max_retries + 1):
            remaining = self._checkpoint()
            self._guard_known_exhaustion(path)
            try:
                resp = self._send(url, etag, remaining)
            except httpx.TimeoutException as exc:
                if attempt == last_attempt:
                    raise GitHubTimeoutError(f"GitHub request timed out after {attempt + 1} attempts") from exc
                self._wait(self._backoff(attempt), "timeout", path, attempt)
                continue
            except httpx.TransportError as exc:
                if attempt == last_attempt:
                    raise GitHubConnectionError(
                        f"could not reach GitHub after {attempt + 1} attempts ({type(exc).__name__})"
                    ) from exc
                self._wait(self._backoff(attempt), "connection_error", path, attempt)
                continue

            self._record_rate_limit(resp)
            status = resp.status_code

            if status == 304:
                if cached is None:  # we sent no validator, so this should be impossible
                    raise GitHubResponseError("received 304 without a cached entry")
                return _Page(cached.data, cached.next_url)

            if 200 <= status < 300:
                return self._accept(key, resp)

            if status in RETRYABLE_SERVER_STATUSES:
                retry_after = _parse_retry_after(resp.headers.get("retry-after"), self._clock())
                if attempt == last_attempt or (retry_after is not None and retry_after > self._max_rate_limit_wait):
                    raise GitHubServerError(f"GitHub returned {status}")
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                self._wait(delay, f"http_{status}", path, attempt)
                continue

            if status in (403, 429):
                wait = self._rate_limit_wait(resp)
                if wait is not None:
                    if wait > self._max_rate_limit_wait or attempt == last_attempt:
                        raise GitHubRateLimitError("GitHub rate limit reached", wait)
                    # Never earlier than permitted; jitter only adds delay.
                    self._wait(wait + self._rng(), "rate_limit", path, attempt)
                    continue
                raise GitHubPermissionError("GitHub denied access to this resource")

            raise _client_error(resp)

        raise AssertionError("unreachable")  # pragma: no cover

    def _accept(self, key: str, resp: httpx.Response) -> _Page:
        try:
            data = resp.json()
        except ValueError as exc:
            raise GitHubResponseError("GitHub returned a non-JSON response") from exc
        link = resp.headers.get("link", "")
        match = _NEXT_LINK_RE.search(link)
        next_url = match.group(1) if match else None
        etag = resp.headers.get("etag")
        if etag:
            self._cache.set(key, CacheEntry(etag=etag, data=data, next_url=next_url))
        return _Page(data, next_url)


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message", "")) if isinstance(body, dict) else ""
    except ValueError:
        return ""


def _client_error(resp: httpx.Response) -> Exception:
    status = resp.status_code
    if status == 401:
        return GitHubAuthError("GitHub rejected the configured credentials")
    if status in (404, 410):
        return GitHubNotFoundError("GitHub resource not found (it may not exist or may be private)")
    return GitHubRequestError(f"GitHub rejected the request with status {status}", status)
