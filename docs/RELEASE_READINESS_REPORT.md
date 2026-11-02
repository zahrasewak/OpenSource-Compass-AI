# Release readiness report: OpenSource Compass AI

Date of the evidence: 2026-11-02. State: **development / pre-release. Not deployed anywhere. Not installed through a browser's extension UI.**
Companion documents: [EXECUTION_QUEUE.md](EXECUTION_QUEUE.md) (task by task), [BROWSER_TEST_REPORT.md](BROWSER_TEST_REPORT.md),
[VALIDATION_STATUS.md](VALIDATION_STATUS.md), [PENDING_VALIDATION.md](PENDING_VALIDATION.md), [AUTHENTICATION.md](AUTHENTICATION.md).

Classification used for every item: **Implemented and verified** / **Implemented but partially verified** / **Not implemented** / **Blocked by external requirements**.
Nothing below is marked verified on the strength of a design or a unit test alone when the claim is about a browser, a database server or GitHub.

## 1. Executive summary
- The backend (FastAPI + SQLAlchemy + Alembic) and the Chrome/Edge MV3 extension work together end to end in automated runs: sign-in with a one-time
  pairing code, heuristic match scores with honest "unknown" handling, difficulty hint, repository health and readiness, saved issues, feedback,
  profile editing, token renewal, revocation and lockout.
- Security work of this round: rate limiting of the unauthenticated auth endpoints (memory or database-shared), bounds on "Keep me signed in" sessions,
  a fix for a real exposure (content scripts could read `chrome.storage.local`), sign out everywhere, cancellation of abandoned backend work with
  deadlines, and a production extension build pinned to one https backend.
- Verified results: backend **690 passed / 7 skipped** (SQLite) and **695 passed / 2 skipped** (PostgreSQL 16.2, 8/8 validation steps, migrations to 0008);
  extension **317 unit tests** (after the UI redesign), type-check and build pass; browser suite **35/35** in Chrome 156 and Edge 156, light and dark, on the redesigned UI.
- What is **not** done or **not** proven: official PostgreSQL image/CI run, ESLint, logged-in GitHub, GitHub OAuth, the execution sandbox,
  recommendation quality (never evaluated), an ML model (none usable), production deployment and TLS, store publication, screen-reader testing.
- Verdict: suitable for continued development and for use by the owner on a trusted machine. **Not ready for a public or multi-user production release.**

## 2. Files changed (by area; the project is not a Git repository, so there is no diff)
| Area | Files |
|---|---|
| Backend auth and limits | `backend/app/core/ratelimit.py` (new), `app/api/v1/auth.py`, `app/services/sessions.py`, `app/core/config.py`, `app/main.py`, `app/cli.py` |
| Backend cancellation | `app/core/requestctx.py` (new), `app/core/middleware.py` (new), `app/services/github/client.py`, `app/services/github/errors.py`, `app/services/singleflight.py` |
| Database | `app/db/models/tables.py`, `alembic/versions/0007_auth_session_persistent_flag.py`, `0008_auth_throttle_events.py`, `alembic/env.py` |
| Backend tests and scripts | `tests/test_auth_ratelimit.py`, `test_sessions.py`, `test_abandoned_work.py`, `test_cors.py`, `test_postgres_integration.py`, `conftest.py`, `scripts/validate_postgres.py` |
| CI | `.github/workflows/ci.yml` (PostgreSQL job runs the validation script; has never run) |
| Extension | `src/shared/{storage,api,session,schemas,authSchemas,messages,buildinfo}.ts`, `src/background/*`, `src/options/*`, `src/content/*` (placeholder buttons removed), `scripts/{build,verify-dist}.mjs`, `package.json`, `tests/*` (incl. `build.test.ts`) |
| Browser tests | `extension/e2e/run.mjs` (35 checks), `e2e/slow_app.py` (test-only delay wrapper), `e2e/real-github*.mjs`, `e2e/seed.py` |
| Docs | `README.md` (new), `.env.example`, this report, `docs/EXECUTION_QUEUE.md`, `AUTHENTICATION.md`, `REQUEST_CANCELLATION.md`, `BROWSER_TEST_REPORT.md`, `VALIDATION_STATUS.md`, `PENDING_VALIDATION.md`, `POSTGRES_VALIDATION.md`, `EXTENSION.md`, `API.md`, `GITHUB_API_STRATEGY.md`, `OAUTH_DESIGN.md`, `SANDBOX_*.md`, `docs/evidence/*` |

## 3. Features and their status
| Feature | Classification | Basis |
|---|---|---|
| Pairing-code sign-in, rotating refresh tokens, reuse detection, sign out, sign out everywhere | Implemented and verified | `test_sessions.py` (34); browser steps 2-3, 18-23b (Chrome, Edge) |
| Auth endpoint rate limiting, memory backend | Implemented and verified | `test_auth_ratelimit.py`; browser step 24b |
| Auth endpoint rate limiting shared across workers (database backend) | Implemented but partially verified | tested with two limiters on one database (SQLite and PostgreSQL); not with several live server processes |
| "Keep me signed in" bounds (72 h idle / 14 d) and storage separation | Implemented but partially verified | unit + browser (Chrome, Edge). Refresh token is plaintext at rest in the browser profile; restart persistence of the storage access level not verified |
| Request cancellation / deadlines / partial results | Implemented but partially verified | fake GitHub + real server (control 9 calls vs 3). Not verified against real GitHub or behind a proxy |
| GitHub resilience: single-flight, ETag/304, rate-limit fail-fast | Implemented but partially verified | mocked tests; real unauthenticated runs; authenticated use and real 304 reuse not tested |
| Heuristic matching (`heuristic_v1`, `v2`), unknown ≠ perfect match | Implemented and verified (logic) | named tests in `test_matches_api.py`, `test_recommend_scoring.py`, `test_match_store.py`. **Quality of recommendations: not evaluated** |
| Difficulty: maintainer-label hint | Implemented and verified (logic, UI) | browser step 5 |
| ML difficulty estimate | Implemented but **no usable model**; experimental, off by default, labelled so in the UI | no human labels exist; never claimed as accurate |
| Repository health / readiness (with partial results) | Implemented but partially verified | mocked tests, controlled browser steps 7-8, real-site screenshot observation in Stage 14 |
| Profile editor, saved issues list, save/unsave, feedback | Implemented and verified (controlled page) | steps 9-11, 16, 17. Feedback on the real site not tested |
| Sidebar placement, SPA navigation, light/dark, contrast, keyboard, narrow widths | Implemented and verified (controlled page, Chrome and Edge); real GitHub observed in Stage 13-14 | steps 4b-4f, 13; GitHub changes its markup, so placement can break |
| Production extension build pinned to one https backend | Implemented and verified (build level) | `build.test.ts` (6). No production backend exists |
| Placeholder actions (Explain, Similar issues, Learning path) | Not implemented, and no longer shown at all. What exists instead is built from real data: "Why it matches", "You may want to learn this first" (the match's missing skills), and Save/Unsave. **"Find similar issues" does not exist** | step 12 |
| Find issues (recommendations list with language/topic/difficulty filters, optional repository lookup, save/unsave, details) | Implemented and verified (unit tests with a fake worker; browser: renders, details/difficulty load, contrast, keyboard) | repository lookup and saving from this list were not exercised in a real browser |
| Tag-based profile with completion indicator (reflects saved data only) | Implemented and verified | steps 16, unit tests |
| ESLint | Not implemented (tooling incompatibility) | see EXECUTION_QUEUE Q-C4 |
| GitHub OAuth | Not implemented (design only) | [OAUTH_DESIGN.md](OAUTH_DESIGN.md); out of scope by instruction |
| Server-side GitHub token architecture | Not implemented (analysis only) | [GITHUB_API_STRATEGY.md](GITHUB_API_STRATEGY.md) |
| Execution sandbox / Project Lab | Not implemented; **Blocked by external requirements** (isolated host, Docker + gVisor/Kata) | [SANDBOX_DESIGN.md](SANDBOX_DESIGN.md); no untrusted code was ever run |
| Chrome Web Store / Edge Add-ons publication | Not implemented | no listing, signing or privacy policy prepared |

## 4. Security fixes and remaining risks
Fixed and verified this round:
1. Content scripts (beside GitHub's page) could read `chrome.storage.local` by default: restricted to trusted contexts; verified before and after in Chrome 156 and Edge 156; the page's own scripts cannot see credentials (step 15).
2. Unauthenticated `/auth/exchange`, `/auth/refresh`, `/auth/logout` were unlimited: now limited per client address with lockout, answered before any lookup, identical 401s (no oracle, including for empty codes).
3. Abandoned requests kept spending GitHub quota: now cancelled, bounded by deadlines (25 s / 60 s), with partial results.
4. Persistent sessions are now bounded and server-revocable (sign out everywhere).
5. A production build cannot talk to localhost or to another backend, and ships no source maps.

Remaining risks (highest first):
1. **No TLS, reverse proxy, secret management or monitoring exist** because nothing is deployed. `COMPASS_AUTH_SECRET` must be provided by the operator; no secret values are in the repository (token-shaped-string scan found none; the only env file is `.env.example`).
2. Rate-limit key is the client address: users behind one NAT share a budget; behind a proxy `COMPASS_TRUSTED_PROXY_HOPS` must be set correctly or the limit is wrong. Memory backend is per process. The database backend is approximate under concurrency.
3. Refresh token is stored in plaintext in the browser profile when "Keep me signed in" is on. Anyone with the profile on disk can use it until it expires or is revoked.
4. Sessions are created only through an administrator CLI (`issue-pairing-code`); there is no self-service sign-up and no OAuth.
5. GitHub quota: unauthenticated GitHub access is 60 requests/hour per IP, so heavy use will hit "rate limited".
6. API hardening headers beyond `Cache-Control: no-store` on token responses (e.g. `X-Content-Type-Options`) are not set. Low risk for a JSON API behind an extension, but not done.
7. Logging was reviewed by searching log calls for tokens/codes (none logged; session ids and request ids only). It was not audited with a log-capture test of every code path.
8. The GitHub page markup can change and silently remove the sidebar placement (the panel then floats rather than disappears).
9. PostgreSQL was validated on a third-party embedded build, not the official image.

## 5. Backend results
| Check | Result |
|---|---|
| `pytest -q` (SQLite, in memory), 2026-11-02 | **690 passed, 7 skipped** (5 PostgreSQL integration tests need a database URL; 2 database-limiter-only cases skip for the memory backend). Took 1 h 42 min; cause of the slowness not investigated |
| Same suite on PostgreSQL 16.2, `scripts/validate_postgres.py` | **8/8 steps; 695 passed, 2 skipped, 4 min**; log: [evidence/postgres-validation-2026-11-02-final.md](evidence/postgres-validation-2026-11-02-final.md) |
| Rate limiter, memory and database | 51 cases + 2 by-design skips, on both SQLite and PostgreSQL |

## 6. Frontend results (`extension/`, `npm run check`, 2026-11-02)
| Check | Result |
|---|---|
| Type-check (`tsc --noEmit`) | pass |
| Unit/component tests (vitest) | **317 passed**, 0 failed, 0 skipped, in 10 files (includes 6 production-build tests and the redesigned options page, panel, handler and API tests) |
| Lint | **not run: ESLint is not configured** (typescript-eslint incompatible with the TypeScript 7 toolchain) |
| Development build + `verify-dist` (no source maps, no token-shaped strings, packaging invariants) | pass |
| Production build | covered by `build.test.ts` (refusals, pinned manifest, no localhost, verify-dist rejection). Built with an example host only; no real deployment |

## 7. Database and CI results
- Alembic migrations 0001-0008: upgrade, `alembic check` (no model drift), downgrade to base, upgrade again, JSONB/timestamptz/constraint/index/FK inspection: **pass on PostgreSQL 16.2** (local embedded Windows build installed through the `pgserver` Python package, **not** the official `postgres:16` image). The server and its data were deleted afterwards.
- SQLite is used for the fast suite only; it is **not** counted as PostgreSQL validation.
- CI (`.github/workflows/ci.yml`): the `migrations-postgres` job runs the same script. **It has never run**: the project is not in a GitHub repository and no Docker is available here. Status: blocked by external requirements.

## 8. Browser results and evidence
Full per-scenario table (scenarios 1-17 with steps, expected, actual, status): [BROWSER_TEST_REPORT.md](BROWSER_TEST_REPORT.md) sections 6-9.

| Browser (headless, Playwright) | Scheme | Result |
|---|---|---|
| Chrome 156.0.8078.48 | light | 35/35 |
| Chrome 156.0.8078.48 | dark | 35/35 |
| Edge 156.0.4314.42 | light | 35/35 |
| Edge 156.0.4314.42 | dark | 35/35 |

The table is the final run on the redesigned UI (each configuration passed on its first run; earlier runs, including an Edge dark failure of the slow-backend step on the pre-redesign build, are in BROWSER_TEST_REPORT.md).
Evidence: the run outputs (kept in the temporary directory of the machine, not in the repository) and screenshots under `extension/e2e/screenshots/<browser>-<scheme>/`.
Limits: a controlled stand-in for github.com (request-intercepted, fictional data) plus earlier observations of the real site as a logged-out visitor
with unauthenticated GitHub access. **Not done:** logged-in GitHub; GitHub's non-default themes; loading through the `chrome://extensions` UI; Chrome's
"Reload" button (in Chrome the service worker is restarted instead; `chrome.runtime.reload()` was exercised in Edge only); Chromium 151 this round; real-site
re-run this round; screen readers; macOS/Linux; Firefox/Safari (unsupported).

## 9. Exact commands
Backend (Windows PowerShell shown; see README.md for Linux/macOS):
```
cd backend
python -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy ..\.env.example .env        # edit: set COMPASS_AUTH_SECRET (>= 32 random chars) and COMPASS_DATABASE_URL
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m app.cli issue-pairing-code --github-id <numeric id> --username <name>     # one-time code, valid 10 minutes
```
Extension:
```
cd extension
npm install
npm run check
npm run build                                                              # development build, backend http://localhost:8000
COMPASS_BACKEND_URL=https://<your-backend> npm run build:production       # production build, backend pinned
```
**Load unpacked directory: `extension/dist`** (open `chrome://extensions` or `edge://extensions`, enable Developer mode, Load unpacked, choose that folder).
Browser tests: `cd extension ; $env:BROWSER="chrome" ; $env:COLOR_SCHEME="dark" ; npm run e2e` (needs the backend virtualenv; `BROWSER` is `chrome`, `msedge` or `chromium`).
PostgreSQL validation: `python scripts/validate_postgres.py postgresql://USER:PASSWORD@HOST:PORT` (creates and drops two scratch databases).

## 10. Environment variables (names and meaning only; no values are stored in the repository)
| Variable | Meaning |
|---|---|
| `COMPASS_DATABASE_URL` | database; PostgreSQL for anything real |
| `COMPASS_AUTH_SECRET` | signing key, >= 32 random characters; without it nobody can sign in |
| `COMPASS_ENVIRONMENT` | `production` refuses legacy CLI tokens and warns about the memory limiter |
| `COMPASS_AUTH_RATE_LIMIT_ENABLED`, `_BACKEND` (`memory`/`database`), `COMPASS_AUTH_ATTEMPTS_PER_MINUTE`, `_MAX_FAILURES`, `_FAILURE_WINDOW_SECONDS`, `_GLOBAL_ATTEMPTS_PER_MINUTE` | limiter settings (`.env.example`) |
| `COMPASS_TRUSTED_PROXY_HOPS` | number of trusted proxies for `X-Forwarded-For` |
| `COMPASS_AUTH_PERSISTENT_REFRESH_TTL_HOURS`, `COMPASS_AUTH_PERSISTENT_SESSION_MAX_DAYS` | "Keep me signed in" bounds |
| `COMPASS_REQUEST_CANCELLATION_ENABLED`, `COMPASS_REQUEST_DEADLINE_SECONDS`, `COMPASS_SLOW_REQUEST_DEADLINE_SECONDS` | cancellation and deadlines |
| `COMPASS_GITHUB_TOKEN` (optional) | server-side GitHub read credential; **not used in any test here**; the owner's personal token was never used |
| `COMPASS_CORS_ORIGINS` | leave empty for the extension |
| `COMPASS_BACKEND_URL` | build-time only, for `build:production` |
| `COMPASS_TEST_DATABASE_URL`, `COMPASS_TEST_SUITE_DATABASE_URL` | tests only; the second one is destructive to the database it names |

## 11. Known limitations and blocked tasks
- Blocked by external requirements: official PostgreSQL image / CI run; Docker sandbox host; a deployed HTTPS backend; logged-in GitHub test account; store publication.
- Not implemented (by instruction): GitHub OAuth, server-side token architecture, execution sandbox, Stage 16.
- Not evaluated: recommendation quality, the ML model (no human labels), `heuristic_v2` against `v1`.
- Unexplained: slow SQLite suite (1 h 42 min), one Edge dark failure of the slow-backend step, one earlier unit-test flake, intermittent test-client `ECONNRESET` in step 23b earlier in the day.
- No ESLint.

## 12. Prioritised remaining work
1. P0 before any shared deployment: choose hosting, TLS and a reverse proxy; set `COMPASS_TRUSTED_PROXY_HOPS`; use the database limiter backend; run the CI PostgreSQL job on the official image.
2. P0: decide the sign-in model for other users (GitHub OAuth per [OAUTH_DESIGN.md](OAUTH_DESIGN.md)) and whether to run a server-side GitHub token; until then access is administrator-issued only.
3. P1: manual pass on real Chrome and Edge: Load unpacked through the UI, logged-in GitHub, GitHub's other themes, Reload button, screen reader.
4. P1: evaluate recommendations with human-labelled data before any quality claim; label data for the ML experiment.
5. P1: add ESLint once the toolchain supports it.
6. P2: investigate the slow SQLite suite and the two unexplained intermittent failures; add API hardening headers; test the database limiter with several live worker processes.
7. P2: store listing materials (privacy policy, permissions justification) if publication is wanted.
8. Future, separate decision: the isolated execution sandbox (Project Lab).
