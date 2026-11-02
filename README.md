# OpenSource Compass AI

A tool that helps developers find GitHub issues that fit them. A **FastAPI backend** keeps a developer profile, reads GitHub data and scores
issues against that profile. A **Chrome / Edge Manifest V3 extension** shows the result as a panel on GitHub issue pages and as a "Find issues"
page of recommendations.

**Status: development / pre-release, not deployed anywhere.** Read [docs/RELEASE_READINESS_REPORT.md](docs/RELEASE_READINESS_REPORT.md) for
what is verified and what is not. GitHub OAuth and the Project Lab execution sandbox are **not implemented**.

## Problem

Finding a first (or next) open-source contribution means reading many issues to judge whether the skills, the effort and the project's health
suit you. Compass reads the issue, the repository and your profile, and shows a score with the reasons behind it, the skills you may still
need, and how active and welcoming the repository looks.

## How recommendations work (read this before relying on a score)

The scoring is **heuristic and rule-based** (`heuristic_v1` / `v2`): it compares your saved skills, interests and preferences with the
issue's text, labels and the repository's language and topics, and it reports "not enough information" instead of guessing when data is missing.
**No trained machine-learning model is used for recommendations, and recommendation quality has not been evaluated** against human judgement.

There is also an *experimental* ML difficulty estimate. It is **off by default**, **no usable trained model exists** in this repository (no
human-labelled data was ever collected), and the interface labels it experimental. The `ml-training/` folder is tooling only. See
[docs/RECOMMENDATIONS.md](docs/RECOMMENDATIONS.md) and [docs/ML.md](docs/ML.md).

## Main features

- **Panel on a GitHub issue page**: title and repository, match score with a plain-words reading, reasons it matches, skills you may want to
  learn first, Save / Unsave, difficulty (maintainer label; ML estimate marked experimental), and collapsed sections for the score breakdown,
  repository health, contribution readiness and feedback.
- **Settings page** with four tabs: *Find issues* (ranked list with language / topic / difficulty filters and an optional repository lookup),
  *Saved issues*, *Profile* (searchable skill tags with a three-step level and a completion indicator based on saved data) and *Account*.
- **Sign-in with one-time pairing codes**, short-lived access tokens, rotating refresh tokens with reuse detection, sign out, and sign out everywhere.
- **Safety measures**: rate limiting of the sign-in endpoints, request deadlines and cancellation of abandoned GitHub work, GitHub response caching
  with single-flight, a production extension build pinned to one https backend.
- Not offered because not implemented: explaining an issue in free text, finding similar issues, a learning path.

## Technology stack

| Layer | Technology |
|---|---|
| Extension | React 19, TypeScript, Vite, zod, Chrome Extension Manifest V3 (service worker + content script + options page) |
| Backend | Python 3.12, FastAPI, SQLAlchemy, Alembic, PyJWT |
| Database | PostgreSQL 16 intended for real use; SQLite for tests and quick local trials |
| Tests | pytest (backend); vitest + Testing Library (extension); Playwright (real Chrome/Edge end-to-end) |
| CI | GitHub Actions workflow in `.github/workflows/ci.yml` |

## Architecture

```
backend/      FastAPI app (app/), Alembic migrations (alembic/), tests (tests/), scripts/
extension/    React + TypeScript + Vite MV3 extension (src/), unit tests (tests/), browser e2e (e2e/), build scripts (scripts/)
docs/         architecture, API, authentication, validation status, test reports, designs
ml-training/  data collection / labelling / training tooling (experimental; no usable model exists)
```

The extension's **background service worker is the only component that holds tokens and talks to the backend**. The content script (the panel on
github.com) and the settings page ask the service worker through validated messages. The backend stores users, profiles, issues, matches, saved
issues, feedback and sessions, and fetches repository and issue data from the GitHub REST API on demand (unauthenticated GitHub access is limited
to 60 requests/hour per IP; an optional server-side read-only token raises that). More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/EXTENSION.md](docs/EXTENSION.md), [docs/API.md](docs/API.md), [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md).

## 1. Backend setup

Prerequisites: Python 3.12, and for anything beyond local experiments a PostgreSQL 16 database.

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1 ; Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt                      # add -r requirements-ml.txt only to run the ML tests/pipeline
cp ../.env.example .env                                  # then edit .env (never commit it)
```

Required settings (all `COMPASS_` environment variables, listed with explanations in [.env.example](.env.example); **no secret values live in the
repository**):

| Variable | Purpose |
|---|---|
| `COMPASS_DATABASE_URL` | `postgresql+psycopg://USER:PASSWORD@HOST:5432/DB` (a SQLite URL such as `sqlite:///./compass_demo.db` works for quick local trials only) |
| `COMPASS_AUTH_SECRET` | >= 32 random characters used to sign access tokens: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Without it nobody can sign in |
| `COMPASS_ENVIRONMENT` | `production` refuses legacy CLI tokens and warns about the per-process rate limiter |
| `COMPASS_AUTH_RATE_LIMIT_BACKEND` | `memory` (default, one process) or `database` (**use this with more than one worker/instance**) |
| `COMPASS_TRUSTED_PROXY_HOPS` | number of trusted reverse proxies in front of the API (0 = ignore `X-Forwarded-For`) |
| `COMPASS_GITHUB_TOKEN` | optional server-side read-only GitHub credential (unauthenticated access works but is limited to 60 requests/hour per IP); see [docs/GITHUB_API_STRATEGY.md](docs/GITHUB_API_STRATEGY.md) |
| `COMPASS_CORS_ORIGINS` | leave empty: the extension does not need CORS |

Migrate the database and start the server (from `backend/`, virtualenv active):

```bash
alembic upgrade head                                     # create/upgrade the schema (revision 0008)
uvicorn app.main:app --host 127.0.0.1 --port 8000        # serve (put TLS termination/a reverse proxy in front for anything non-local)
curl http://127.0.0.1:8000/ready                         # {"status":"ready",...}
```

Create a user and sign them in (there is no self-service sign-up; an administrator issues a one-time pairing code):

```bash
python -m app.cli issue-pairing-code --github-id 12345 --username octocat     # prints a code, valid 10 minutes, usable once
python -m app.cli revoke-sessions --github-id 12345                          # ends every session of that user
```

## 2. Extension: build and install

Prerequisites: Node 20+ (developed on 24), Chrome or Edge 110+.

```bash
cd extension
npm install
npm run check                                   # type-check + unit tests + development build (with packaging/security checks)
npm run build                                   # development build -> extension/dist  (default backend http://localhost:8000)
COMPASS_BACKEND_URL=https://compass.example.com npm run build:production   # production build -> extension/dist, backend PINNED
```

Install it in the browser:

1. Start the backend (section 1) so `http://127.0.0.1:8000/ready` answers.
2. Open `chrome://extensions` (Edge: `edge://extensions`) and turn on **Developer mode**.
3. Click **Load unpacked** and select the folder **`extension/dist`** (it exists after `npm run build`).
4. The settings page opens. Under **Account**, enter a pairing code from `python -m app.cli issue-pairing-code ...` and sign in.
5. Add a few skills under **Profile**, then open any GitHub issue (`https://github.com/<owner>/<repo>/issues/<number>`). The panel appears in the
   sidebar. **Find issues** in the settings page lists recommendations among the issues you have already opened.

A production build talks only to its pinned https backend and declares no localhost permission; a development build also accepts another https
backend after Chrome's permission prompt.

## 3. Tests and verified results

Commands:

```bash
cd backend && python -m pytest -q                         # SQLite in memory; a full run is slow (see below)
cd extension && npm run check                             # type-check + 317 unit tests + build
cd extension && BROWSER=chrome COLOR_SCHEME=light npm run e2e   # real browser + real backend + controlled GitHub page (BROWSER=chrome|msedge|chromium, COLOR_SCHEME=light|dark)
```

Results (details, dates and caveats in [docs/VALIDATION_STATUS.md](docs/VALIDATION_STATUS.md) and [docs/RELEASE_READINESS_REPORT.md](docs/RELEASE_READINESS_REPORT.md)):

| Check | Result | When |
|---|---|---|
| Extension type-check (`tsc --noEmit`) | pass | re-run when preparing this upload |
| Extension unit/component tests (vitest) | **317 passed**, 0 failed | re-run when preparing this upload |
| Extension build with packaging/security checks | pass | re-run when preparing this upload |
| Backend subset (system, scoring, profile, saved issues, migrations) | **134 passed** | re-run when preparing this upload |
| Backend full suite, SQLite | 690 passed, 7 skipped (took about 1 h 42 min; cause not investigated) | recorded 2026-11-02, not re-run for this upload |
| Backend full suite plus migrations, PostgreSQL 16.2 (third-party embedded build, not the official image) | 695 passed, 2 skipped | recorded 2026-11-02 ([evidence](docs/evidence/postgres-validation-2026-11-02-final.md)) |
| Browser end-to-end, Chrome 156 and Edge 156, light and dark | 35/35 in each of the four runs | recorded 2026-11-02, not re-run for this upload |

The browser suite uses a request-intercepted stand-in for github.com with fictional data. A separate script
(`extension/e2e/real-github.mjs`) observed the panel on the real github.com as a logged-out visitor; see [extension/README.md](extension/README.md).
The CI workflow has **not run yet** on GitHub. Against PostgreSQL: [docs/POSTGRES_VALIDATION.md](docs/POSTGRES_VALIDATION.md).

## 4. Current limitations

- Pre-release and not deployed; no TLS, reverse proxy, monitoring or hosted backend exist.
- Sign-in needs an administrator-issued pairing code. There is no self-service sign-up and no GitHub OAuth.
- Recommendation quality has not been evaluated; scores are heuristics. The ML difficulty estimate has no usable model.
- Not yet verified: loading through the browser's own "Load unpacked" screen on another machine, logged-in GitHub and GitHub's non-default themes,
  screen readers, macOS/Linux, the official `postgres:16` image and the CI PostgreSQL job. No ESLint is configured.
- Unauthenticated GitHub access allows 60 requests/hour per IP, so heavy use reports "rate limited" unless the server has its own token.
- The refresh token is stored in plain text in the browser profile when "Keep me signed in" is on.
- GitHub can change its page markup, which can move the panel from the sidebar to a floating position.
- Chrome and Edge (Chromium, Manifest V3) only; Firefox and Safari are not supported.

## 5. Future scope

GitHub OAuth sign-in; a hosted deployment with TLS and secret management; a labelled dataset and an evaluation of recommendation quality before
any trained model is used; the Project Lab execution sandbox (design only, see [docs/SANDBOX_DESIGN.md](docs/SANDBOX_DESIGN.md)); lint
configuration; browser-store publication.

## 6. Documentation

[docs/RELEASE_READINESS_REPORT.md](docs/RELEASE_READINESS_REPORT.md) (status, evidence, limitations) -
[docs/EXECUTION_QUEUE.md](docs/EXECUTION_QUEUE.md) - [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) -
[docs/REQUEST_CANCELLATION.md](docs/REQUEST_CANCELLATION.md) - [docs/BROWSER_TEST_REPORT.md](docs/BROWSER_TEST_REPORT.md) -
[docs/VALIDATION_STATUS.md](docs/VALIDATION_STATUS.md) - [docs/PENDING_VALIDATION.md](docs/PENDING_VALIDATION.md) - [docs/API.md](docs/API.md).

## License

No license has been chosen yet, so all rights are reserved by default. Add a `LICENSE` file before others reuse the code.
