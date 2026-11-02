# Pending validation (kept separate on purpose)

Items below are not done, or only partly done, as stated in each item. Do not describe any of it as passed or working beyond what
is written here until it has actually been run and its result recorded in `docs/VALIDATION_STATUS.md`.

## 1. PostgreSQL migrations 0001-0008 - VALIDATED LOCALLY; official-image/CI validation PENDING
**Final run (2026-11-02):** 8/8 steps, whole suite 695 passed / 2 skipped on the same local PostgreSQL 16.2 build, covering migrations up to 0008
(evidence: [evidence/postgres-validation-2026-11-02-final.md](evidence/postgres-validation-2026-11-02-final.md)). The embedded server and its data were
deleted afterwards. The statements below about Stage 14 remain true for what is still pending.
**Stage 14 (2026-11-02):** the migrations, schema checks, integration tests and the whole backend suite (617 tests) were run against a
real PostgreSQL 16.2 server (an embedded third-party Windows build via the `pgserver` package; evidence in
[evidence/postgres-validation-2026-11-02.md](evidence/postgres-validation-2026-11-02.md)). **Still pending:** the same on the official
`postgres:16` image on Linux (the CI job `migrations-postgres`, which now also runs the whole suite, has not run because the project
is not on GitHub), other PostgreSQL versions, and production configuration (TLS, least-privilege roles, pooling under load).
Before Stage 14 the migrations had only been applied to SQLite and rendered to PostgreSQL SQL offline. Exact steps, expected outputs and the checks SQLite cannot make: [POSTGRES_VALIDATION.md](POSTGRES_VALIDATION.md).
Validated in that local environment: the migration round trip, `alembic check`, JSONB/default/constraint/index inspection, the unique
constraints behind idempotent match persistence, single-use pairing codes and refresh tokens under 8-thread races
(`backend/tests/test_postgres_integration.py`), and the whole suite. Not validated: the official image / Linux, other versions.

## 2. Docker sandbox (Project Lab) - NOT BUILT / NOT VALIDATED
Docker is not installed on the development machine; Phase 6 has not started. A security **design** ([SANDBOX_DESIGN.md](SANDBOX_DESIGN.md)) and host
requirements for a Docker + gVisor/Kata execution host ([SANDBOX_HOST_REQUIREMENTS.md](SANDBOX_HOST_REQUIREMENTS.md)) exist; nothing is
implemented, no host exists, no untrusted code has been run, and isolation is **not** claimed to be secure.

## 3. Live GitHub - partly tested (see 3b)
The GitHub client, repository/issue sync and repository intelligence were built against mocked responses; live use so far is
unauthenticated only (3b).

## 3b. Live GitHub - partly tested
Data collection ran once against live GitHub, unauthenticated, for 19 public repositories (2026-11-01), and the backend served
real GitHub data to the extension on a real issue page in Stage 13 (also unauthenticated). Authenticated use, the real API at quota
exhaustion, and ETag/304 behaviour against the real API are still only tested with mocked responses.

## 4. ML on real data - NOT DONE (pilot only)
A real dataset exists (1,485 issues) but no human has labeled anything. 80 issues carry AI annotations (provisional); the
default-gate training run was refused; an exploratory run (15 test issues) did not beat its baselines. No usable model,
no verified labels, no inter-annotator agreement. What a person must do: [PILOT_LABELING_REPORT.md](PILOT_LABELING_REPORT.md)
section 8. Recommendation quality of `heuristic_v1`, `heuristic_v2` and any ML component remains unevaluated.

## 5. Browser extension - partly verified in real browsers
Automated suite 35/35 in Chrome 156 and Edge 156 (light and dark; one unexplained intermittent failure of the slow-backend step in Edge dark's first run) on a controlled page (Chromium 151: 29/29 at Stage 14, not re-run); observed on the **real github.com**
as a logged-out visitor in Chrome 156 and Edge 156 (light/dark rendering, sign-in, score) and in Chrome 156 for rate limits, session
expiry/revocation/re-authentication and save/unsave ([BROWSER_TEST_REPORT.md](BROWSER_TEST_REPORT.md)). **Still pending (by hand):** a
logged-in GitHub account, GitHub's other themes, the rate-limit and session scenarios on Edge, clicking from issue to issue on the real
site, feedback on the real site, "Load unpacked" through the browser UI, a screen-reader pass, other OSes and browser versions.
Authentication is session based ([AUTHENTICATION.md](AUTHENTICATION.md)); GitHub OAuth is **designed but not implemented**
([OAUTH_DESIGN.md](OAUTH_DESIGN.md)); a server-side GitHub token is **not needed for development and is recommended only for a public
multi-user deployment** ([GITHUB_API_STRATEGY.md](GITHUB_API_STRATEGY.md)).

