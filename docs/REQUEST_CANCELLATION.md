# Abandoned requests, time budgets and cancellation (Stage 15, P0)

Stage 14 *inferred* from GitHub-quota readings that the backend keeps working after the browser gives up. Stage 15 verified it, traced the
cause, fixed what can be fixed, and tested before and after. Everything below states what was reproduced, what is only a limit of the design,
and what was **not** verified.

## 1. Finding and root cause
**Reproduced** (not just inferred): `backend/tests/test_abandoned_work.py` starts a real uvicorn server, points the GitHub client at a slow fake
(0.3 s per call), sends `GET /repositories/o/r/health` over a raw socket and hangs up after 0.7 s.

| Case | GitHub calls at hang-up | GitHub calls afterwards (final) |
|---|---|---|
| Control: cancellation switched off (= behaviour before Stage 15) | 3 | **9** (the whole analysis ran on, and was stored for nobody) |
| Fix: cancellation on | 3 | **3** (stopped at once; nothing written; lock released) |

Root cause: the endpoints and the GitHub client are **synchronous** and run in worker threads. When the connection closes, the server learns
about it (`http.disconnect`) but nothing carried that to the thread doing the work. There was also **no overall time bound**: a first-time
health analysis is ~10-14 sequential GitHub calls, each allowed 10 s and up to 3 retries with backoff, so a slow network could take minutes while
the extension waited at most 90 s (30 s for other calls).

Trace across layers: FastAPI/Starlette (disconnect visible only via `receive()`, not delivered to sync endpoints) -> thread pool (`def`
endpoints; not interruptible) -> `GitHubClient._get` (httpx, per-call timeout, retry/backoff `sleep`) -> `collect_repository` (per-signal calls, **one
commit at the end**) -> database (one transaction per request, rolled back when the session closes). There are **no background tasks or durable
jobs** in this application today.

## 2. What was implemented
| Piece | Where | Behaviour |
|---|---|---|
| Per-request context | `app/core/requestctx.py` | `RequestContext`: a cancel flag and a time budget, held in a `ContextVar` (anyio copies it into the worker thread) |
| Disconnect detection | `app/core/middleware.py` | Outermost ASGI middleware. One "pump" task is the only reader of the server's `receive` and forwards messages in order; on `http.disconnect` it sets the cancel flag. (A single reader avoids competing with the framework's own middleware.) |
| Checkpoints | `GitHubClient._checkpoint()` | Before every outbound request and in backoff waits: raises `RequestCancelled` (client gone) or `GitHubDeadlineError` (budget spent). Per-call HTTP timeout is capped to the budget left; waits are slept in 0.25 s slices so a disconnect is noticed quickly; a wait longer than the budget left fails immediately instead of sleeping |
| Single-flight awareness | `app/services/singleflight.py` | A waiting request stops waiting if it is cancelled or out of time (and leaks no lock entry). **A holder is shielded from disconnect-cancellation while other requests wait for its result** (their client is still there), unshielded when nobody waits |
| Budgets | settings | `COMPASS_REQUEST_DEADLINE_SECONDS` 25 (normal), `COMPASS_SLOW_REQUEST_DEADLINE_SECONDS` 60 (repository health, issue readiness); `COMPASS_REQUEST_CANCELLATION_ENABLED` true. Chosen below the extension's own 30 s / 90 s so the client gets a result, not a timeout |
| Error mapping | `app/main.py` | `RequestCancelled` -> 499 (nobody reads it; logged at INFO). `GitHubDeadlineError` is a `GitHubTimeoutError`: **504 `github_timeout`** for single-resource calls |

**Consistency of state.** Cancellation raises between outbound calls, never inside a database write. Repository analysis (`collect_repository`),
issue sync and linked-PR lookup fetch first and write once, so a cancelled request leaves **no partial rows** (asserted: no snapshot is stored).
When the *budget* (not a disconnect) runs out during repository health, signals not yet fetched are recorded as unknown with error
`github_timeout`, and the analysis is returned and stored as an **incomplete** snapshot (never presented as complete; refreshed after the normal
sync TTL). Measured: a 1 s budget returned a partial analysis in 1.27 s with 4 GitHub calls instead of a full one in 2.78 s with 9 calls.

## 3. What is deliberately not cancelled, and limits
- **Work others wait for** (single-flight followers) continues even if its own client left (tested: the follower receives a complete analysis and
  the analysis runs once).
- **A request already in flight to GitHub finishes** (a synchronous HTTP call cannot be aborted from another thread); at most one per cancelled
  request, itself bounded by the remaining budget. The tests allow "+2" calls after a hang-up for this reason.
- **Durable background jobs** (none exist) must not run under a request context, or they would be cancelled with the request that started them. The
  future execution sandbox needs its own job lifecycle.
- **Spent calls are not refunded.** Cancelling saves the *remaining* calls; completed calls stay in the ETag cache, so a retry is cheaper.
- **Closing the panel or navigating away does not disconnect anything**: the extension's background worker issues the request and keeps waiting for
  it. Disconnects occur when the worker's own timeout aborts the fetch (30 s / 90 s) or the browser/network drops the connection. A "cancel on
  panel close" message to the worker is possible but was not built (not required by the finding).
- The mechanism is per process; it needs no shared state, so it is unaffected by the number of workers.

## 4. Verification status
| Check | Status |
|---|---|
| Reproduction and fix against a real uvicorn server, raw-socket disconnect (control vs fix) | **Verified** (SQLite, fake GitHub with real delays); numbers above |
| Waiting-request shield, budget (partial result), retry-loop budget, middleware message order, waiter cancellation, lock cleanup, no-state-on-cancel | **Verified** by 17 tests in `test_abandoned_work.py`; 7 of them fail if the checkpoint is disabled (checked) |
| Existing behaviour outside requests (retries, sleeps) unchanged | **Verified** (`test_github_client.py` unmodified, passing) |
| Same under PostgreSQL | The tests use their own SQLite file; the rest of the suite ran on PostgreSQL (see [POSTGRES_VALIDATION.md](POSTGRES_VALIDATION.md)) |
| Against the **real** github.com / a real browser disconnect | **Not verified.** The quota-drain observation of Stage 14 was not re-created on the real site; the fix is evidenced with a slow fake only |
| Behind a real reverse proxy (does it forward client disconnects promptly?) | **Not verified.** Some proxies buffer or keep the upstream connection open; test in the deployment environment |

## 5. Configuration and deployment
No migration. New optional settings (all have safe defaults): `COMPASS_REQUEST_CANCELLATION_ENABLED`, `COMPASS_REQUEST_DEADLINE_SECONDS`,
`COMPASS_SLOW_REQUEST_DEADLINE_SECONDS`. Under uvicorn nothing else is needed. If a proxy sits in front, make sure it propagates client
disconnects and that its own timeouts are not shorter than the budgets above. `COMPASS_GITHUB_TIMEOUT_SECONDS` (10 s per call) and
`COMPASS_GITHUB_MAX_RETRIES` (3) still apply inside the budget.
