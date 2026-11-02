# Execution queue (master directive: end-to-end completion)

Living document. Status values: **DONE-VERIFIED** (implemented and its verification supports it), **DONE-PARTIAL** (implemented, verification incomplete: say what),
**TODO**, **IN-PROGRESS**, **BLOCKED** (needs something external). Evidence = a test, a command and its actual output, or a recorded browser run.

## Phase 0: baseline (recorded before the directive's work began, 2026-11-02)
| Item | Result |
|---|---|
| Repository | Not a Git repository (`git status`: "not a git repository"); no history to preserve or diff. Nothing was deleted; user files untouched |
| Layout | `backend/` (FastAPI, SQLAlchemy, Alembic, ML in `app/ml`), `extension/` (React 19 + TypeScript + Vite, MV3), `docs/`, `ml-training/`, `.github/workflows/ci.yml` |
| Backend baseline (SQLite) | `pytest -q`: **664 passed, 5 skipped** (the 5 are PostgreSQL integration tests needing a DB URL) |
| Backend on PostgreSQL 16.2 (local embedded build) | `scripts/validate_postgres.py`: **8/8 steps, 669 passed** (docs/evidence/postgres-validation-stage15.md) |
| Extension baseline | `npm run check`: type-check + **275 tests** + build (with packaging/security checks) all pass; no ESLint configured |
| Browser baseline | Controlled-page suite: Chrome 156 dark 32/32; Chrome 156 light 31/32 on two runs and 32/32 on one (see Q-E2: test-client socket artefact in step 23b) |
| Pre-existing failures recorded | None in unit/integration suites. One unexplained one-off unit-test flake (Stage 14: `shows 'checking' while the saved state loads…`) |
| Stage 13/14/15 reports | Read: docs/PENDING_VALIDATION.md, VALIDATION_STATUS.md, AUTHENTICATION.md, REQUEST_CANCELLATION.md, BROWSER_TEST_REPORT.md, OAUTH_DESIGN.md, GITHUB_API_STRATEGY.md, SANDBOX_*.md |

## Queue (final state, 2026-11-02)
| ID | Pri | Task | Depends | Status | Implementation notes / test evidence / blockers |
|---|---|---|---|---|---|
| Q-A1 | P0 | Rate-limit `/auth/exchange`, `/auth/refresh`, `/auth/logout` | | DONE-VERIFIED | Sliding windows per client address, 5 failures/15 min lockout, global ceiling, 429 + Retry-After, checked before lookup. `tests/test_auth_ratelimit.py`: 51 cases (every behaviour test runs on both backends); real-browser lockout step 24b (Chrome, Edge) |
| Q-A1b | P0 | Rate limiting that holds across workers/instances | Q-A1 | DONE-PARTIAL | `COMPASS_AUTH_RATE_LIMIT_BACKEND=database` (table `auth_throttle_events`, migration 0008). Verified with two limiter objects sharing one database, on SQLite and on PostgreSQL 16.2 (695 passed). **Not verified with several real server processes under concurrent load**; counting is approximate under concurrency (documented). Memory backend stays the default and warns in production |
| Q-A2 | P0 | Persistent refresh token audit + mitigations | | DONE-PARTIAL | content-script access to `storage.local` denied (before/after verified in Chrome 156 and Edge 156); persistent sessions 72 h idle / 14 d absolute; sign-out everywhere (step 23b). Remaining: the persisted refresh token is plaintext at rest in the profile (documented in AUTHENTICATION.md); restart persistence of the access-level restriction not verified |
| Q-A3 | P0 | Abandoned backend work: reproduce, bound, cancel | | DONE-VERIFIED (fake GitHub) | `tests/test_abandoned_work.py` 17; control 3 -> 9 GitHub calls vs fix 3 -> 3. **Not verified against real GitHub or behind a proxy** |
| Q-A4 | P1 | GitHub resilience review | | DONE-VERIFIED (mocked + unauthenticated real) | `test_github_client.py` 62, `test_caching.py` 12; real runs in Stages 13-15 |
| Q-B1 | P0 | Real PostgreSQL validation + reproducible CI path | | DONE-PARTIAL | local PostgreSQL 16.2 (third-party embedded Windows build): 8/8 steps, 695 passed / 2 skipped, migrations 0001-0008 ([evidence](evidence/postgres-validation-2026-11-02-final.md)). **Blocked by external requirements:** official `postgres:16` image / Linux / CI job never run (no Docker; project not on GitHub) |
| Q-C1 | P1 | Frontend audit vs directive | | DONE-VERIFIED | sidebar placement on 5 layouts (step 4f), profile (16), saved (9, 10, 17), auth states (1-3, 18-24), light/dark + contrast (4b, 4c), narrow widths (4d), keyboard (4e) |
| Q-C2 | P1 | Remove look-alike placeholder buttons | Q-C1 | DONE-VERIFIED | "Explain this issue / Find similar issues / View learning path" are list items with the reason; e2e step 12 asserts no button and the "Not available yet" note (Chrome, Edge) |
| Q-C3 | P1 | Environment-specific backend URL for production builds | Q-C1 | DONE-VERIFIED (build level) | `COMPASS_BACKEND_URL=https://... npm run build:production`; `tests/build.test.ts` (6): refuses missing/insecure/malformed URLs, manifest has only that host, no localhost, pinned settings. **No production deployment exists or is claimed** |
| Q-C4 | P1 | Lint + type-check + build | Q-C1 | DONE-PARTIAL | type-check (`tsc --noEmit`), 281 unit tests and build in `npm run check`: pass. **ESLint is NOT configured**: typescript-eslint did not support the TypeScript 7 toolchain in use, so none was added (no lint claim is made) |
| Q-C5 | P1 | Build hygiene (no source maps / secrets / localhost in a production dist) | Q-C3 | DONE-VERIFIED | `scripts/verify-dist.mjs` run on every build; tested with a planted map and a token-shaped string |
| Q-D1 | P1 | Backend/frontend integration review | Q-C3 | DONE-PARTIAL | CORS: off unless `COMPASS_CORS_ORIGINS` set, tests `test_cors.py` (3); extension needs none (service-worker fetch with host permission). Token responses `Cache-Control: no-store`; `X-Request-ID` on responses; logs do not contain tokens/codes (grep of `logger.*` calls). Per-user isolation tests exist (profile, saved, feedback, sessions). **Not added: `X-Content-Type-Options`/other hardening headers on the JSON API (low priority, listed in the release report)** |
| Q-D2 | P1 | Recommendation/ML integrity review | | DONE-VERIFIED | `test_no_profile_is_insufficient_not_a_perfect_match`, `test_unknown_uses_conservative_fill_not_perfect_and_not_zero`, `test_heuristic_scoring_code_is_frozen_per_version`; UI labels "Heuristic vX", "Experimental" ML estimate (off by default, "unavailable" in the e2e), unknown parts shown as unknown (step 4). No retraining, no new metrics. Recommendation quality remains NOT EVALUATED |
| Q-E1 | P0 | Browser scenarios 1-17 recorded per scenario (Chrome + Edge) | Q-C* | DONE-PARTIAL | [BROWSER_TEST_REPORT.md](BROWSER_TEST_REPORT.md) sections 6-9. 35/35 in Chrome light/dark and Edge light/dark (some after re-runs, see below). Not done: logged-in GitHub, other GitHub themes, UI Load-unpacked, screen readers, real-site re-run this round |
| Q-E2 | P2 | Intermittent e2e `ECONNRESET` in step 23b | | DONE-PARTIAL | not seen in the final four runs; helper retries once; cause inferred, not proven |
| Q-E3 | P1 | New scenarios: fresh install, extension reload, slow backend | | DONE-PARTIAL | steps 0, 23c, 23d added. Chrome cannot reload a CDP-loaded extension via `chrome.runtime.reload()`: Chrome uses a service-worker stop/restart instead (reload NOT exercised in Chrome; exercised in Edge). 23c failed once in Edge dark (cause not found) and passed on re-run |
| Q-F1 | P0 | Startup docs, env vars, limitations, readiness report | | DONE-VERIFIED (docs) | README.md, `.env.example`, [RELEASE_READINESS_REPORT.md](RELEASE_READINESS_REPORT.md). The commands were followed by the test harnesses, but the README was **not** followed on a clean machine |
| Q-G1 | P2 | Unexplained: full SQLite backend suite took 1 h 42 min vs 4 min on PostgreSQL | | TODO | not investigated; no failures |
| Q-G2 | P2 | Unexplained: Edge dark 23c failure on first run | Q-E3 | TODO | not reproduced in the immediate re-run |
| Q-G3 | P2 | One-off unit-test flake (`shows 'checking' while the saved state loads…`, Stage 14) | | TODO | not seen again in the final run |

Out of scope by instruction (stay future work): GitHub OAuth, server-side GitHub-token architecture, execution sandbox, Stage 16, executing untrusted code.

## Frontend redesign (follow-up request, 2026-11-02)
| ID | Pri | Task | Status | Notes / evidence |
|---|---|---|---|---|
| Q-H1 | P1 | Redesign: settings page (Find issues, Saved, Profile, Account), compact panel, warm off-white + green accent, light/dark | DONE-VERIFIED (automated) | `npm run check`: 317 unit tests; browser suite 35/35 in Chrome 156 and Edge 156, light and dark; contrast lowest 5.37:1 light / 4.63:1 dark on three tabs |
| Q-H2 | P1 | Recommendations view wired to the real backend (`POST /matches/search`, no persistence while browsing) | DONE-PARTIAL | unit + browser render verified; "look in a repository" (GitHub quota) and saving from the list not run in a browser |
| Q-H3 | P1 | Only working actions in the panel | DONE-VERIFIED | placeholder list removed; "Find similar issues" is not implemented |
| Q-H4 | P2 | Manual pass: Load unpacked via the browser UI, real github.com visual check, screen reader | BLOCKED (needs a person) | see BROWSER_TEST_REPORT.md |
