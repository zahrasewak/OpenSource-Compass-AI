# GitHub rate limits and caching (Stage 13)

Goal: fewer GitHub requests on first open, none on repeat views, no request storms, and clear partial results when the quota
is gone. **Nothing here works around a limit**: no IP/account rotation, no token pooling, no scraping; unauthenticated use is
60 requests/hour per IP and the code assumes that.

## What was found and what changed

| Problem (before) | Change | Where |
|---|---|---|
| Opening an issue fires match + difficulty (+ health/readiness) at once; each resolved the repository/issue itself, so N overlapping requests made N identical GitHub calls | **Single-flight**: a keyed lock per resource (`repo`, `issues`, `issue`, `intel`, `linked`). Followers wait, then re-check freshness in a fresh read transaction and reuse the leader's stored result. Different keys still run in parallel | `services/singleflight.py`, `services/repositories.py`, `services/intelligence.py` |
| Conditional requests existed (ETag/304) but never hit: the activity window start was `now - 90 days` to the second, so the URL changed on every call | Window start aligned to **midnight UTC**; the URL is stable within a day, so the client's in-memory ETag cache answers `304 Not Modified` (not counted against the quota) | `services/intelligence.py` |
| After a rate-limit response every later request still called GitHub to find out | The client remembers known exhaustion (until `X-RateLimit-Reset`) and **fails fast with 429 + `Retry-After`** without a request. No sleeping loops; waits longer than `COMPASS_GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS` (default 5 s) are returned, not slept | `services/github.py` (Stage 5), verified here |
| Readiness during a rate limit was an error | **Partial results**: locally computable signals are returned, GitHub-dependent ones are marked unknown with `upstream_errors` | `services/intelligence.py` |
| The extension asked the backend again on every navigation, panel reopen or section toggle | **Extension response cache** (worker memory): match/difficulty 5 min, health/readiness 10 min, **health keyed per repository** so all issues of a repo share one analysis; identical concurrent requests share one promise; the stored-match id is reused for save + feedback (1 request instead of 3); a rate-limit response is remembered until `Retry-After` and the UI holds **Try again** with a countdown (no retry loop) | `extension/src/background/{cache,handler}.ts`, `src/content/ui.tsx` |
| Nonessential analysis ran on page open | **Lazy**: repository health and readiness (the GitHub-hungry parts) load only when the user expands them (already so since Stage 12; unchanged and tested) | `extension/src/content/sections.tsx` |
| "Is this saved?" had no cheap answer | `GET /saved-issues/lookup/{o}/{r}/{n}`: database only, never GitHub, caller's rows only | `api/v1/saved_issues.py` |

Freshness limits are unchanged and remain the contract: repository metadata `COMPASS_GITHUB_SYNC_TTL_SECONDS` (900 s) with a
minimum refresh gap `COMPASS_GITHUB_MIN_REFRESH_SECONDS` (30 s); complete repository-intelligence snapshots 24 h
(`COMPASS_INTEL_SNAPSHOT_TTL_SECONDS`), incomplete ones only the normal sync TTL, linked-PR lookups 1 h; stale data is served and
labelled stale if a refresh fails or is rate limited.

## Evidence

### Mocked-GitHub tests (`backend/tests/test_caching.py`, 12 tests, all passing)
- 8 concurrent requests for one repository -> **1** GitHub call (control with the lock disabled -> several, proving the test can fail).
- 8 overlapping match/difficulty/readiness resolutions of one issue -> **1** repository call + **1** issue call.
- 8 concurrent health analyses -> each signal collected once; 8 concurrent linked-PR lookups -> one timeline request.
- Second health analysis the same day: **every** request answered `304`, none `200`, identical result.
- After one rate-limit response, 3 further requests make **0** more GitHub calls, 0 sleeps; readiness degrades to partial results.

### Extension tests (`extension/tests/handler.test.ts`, `cache.test.ts`; 262 extension tests in total)
5 views of one issue -> 1 backend call; 3 issues of one repository -> 1 health call; 4 concurrent identical requests -> 1;
3 actions (save + 2 feedback) -> 1 stored match; after a rate limit, repeated asks -> 0 further backend calls until the wait ends;
ordinary errors are *not* cached; a profile change, sign-in/out or settings change clears the cache.

### Real github.com run (2026-11-01, unauthenticated, Playwright Chromium, `psf/requests#1`; script `extension/e2e/real-github.mjs`)
Measured as the drop in GitHub's own `core` quota (`/rate_limit`, which is free), one real run, one issue, one network:

| Action | GitHub API requests used |
|---|---|
| Sign in + first open (match + difficulty; repository and issue fetched, nothing stored beforehand) | **2** |
| Reload of the same issue page | **0** |
| Navigate to the repository's issue list and back (GitHub's in-page navigation) | **0** |
| Expand **Repository health** (first analysis of that repository) | **14** |

Before the change, the same first open would also have been repeated by each overlapping section request when the rows were
stale; those savings are shown by the mocked tests above, not by this single real measurement. The real run does **not** prove
the reduction factor: it has no "before" measurement on real GitHub. It shows the post-change numbers are small and that repeat
views cost nothing.

## Known limits
- The ETag cache is in memory inside the backend process: it resets on restart and is not shared between workers, so a
  multi-worker deployment reuses less. A shared store (Redis) is the next step; not built.
- Single-flight locks are per process. Several backend processes can still each make one request for the same resource.
  Database uniqueness keeps the data consistent; only the saving is lost.
- A first health analysis for a repository costs ~14 requests; a quota of 60/hour allows only a handful of new repositories per
  hour without a GitHub token. Configure `COMPASS_GITHUB_TOKEN` (read-only, public data) on the server for 5,000/hour.
- When GitHub's quota is exhausted the panel shows the rate-limit message and the wait; data already stored is still served.
