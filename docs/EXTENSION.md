# Chrome extension (Stage 12, extended in Stage 13)

A Manifest V3 extension (React + TypeScript + Vite) in `extension/` that adds an **OpenSource Compass AI** panel to GitHub
issue pages and talks to your own OpenSource Compass backend.

## What it shows (all from existing backend endpoints)
| Panel part | Endpoint | Notes |
|---|---|---|
| Match score, breakdown, "Why this score?" | `POST /api/v1/issues/{o}/{r}/{n}/match` **(added this stage)** | Labelled "Heuristic v1/v2, not an ML prediction". Unknown components show **Unknown**, never 0. Viewing stores nothing |
| Difficulty | `POST /api/v1/issues/{o}/{r}/{n}/difficulty` | Maintainer-label hint (deterministic) and the ML estimate are shown **separately**. The ML part always carries an **Experimental** badge: trained on provisional labels, not validated on real data, uncalibrated scores. Usually "no ML model is configured" today |
| Repository health (on demand) | `GET /api/v1/repositories/{o}/{r}/health` | Heuristic; shows freshness, staleness, incomplete collection |
| Contribution readiness (on demand) | `GET /api/v1/issues/{o}/{r}/{n}/readiness` | Detection only; says files/commands were not executed; linked PRs listed or "not checked" |
| Save issue / Remove | `POST /api/v1/saved-issues`, `DELETE /api/v1/saved-issues/{id}` | Linked to a stored match |
| Feedback (relevance, difficulty) | `POST /api/v1/feedback` | Linked to a stored match; duplicates reported |
| **Saved indicator** (Stage 13) | `GET /api/v1/saved-issues/lookup/{o}/{r}/{n}` | Shown when the page opens ("Already in your saved issues"); database only, never calls GitHub; own rows only |
| **Profile view/edit** (Stage 13, settings page) | `GET/POST/PUT /api/v1/profile` | Skills, interests, goals, task types, difficulty, experience; validated locally and by the server |
| **Saved-issues list** (Stage 13, settings page) | `GET /api/v1/saved-issues`, `DELETE .../{id}` | Paged, with remove; titles/states are as last fetched |

**Unavailable on purpose (no backend support; shown disabled with the reason, no code runs):** *Explain This Issue* (no
explanation endpoint exists), *Find Similar Issues* (no embedding/duplicate search), *View Learning Path* (no learning-path
generator). To enable one, build its endpoint first, then add the action in `extension/src/content/Actions.tsx` and the
message in `src/shared/messages.ts`.

Not in the extension: the dashboard. Without a profile the panel says so and marks the score low-confidence; it never presents it
as a match, and the settings page's Profile tab explains that missing skills/interests are *unknown*, not a perfect match.

## Backend dependencies
Running backend (`uvicorn app.main:app`), `COMPASS_AUTH_SECRET` set, and a one-time pairing code for sign-in (below). New endpoint added:
`POST /issues/{owner}/{repo}/{number}/match` (body `{scorer?: "heuristic_v1"|"heuristic_v2", persist?: bool}`, default v1,
persist default true). It reuses the existing scorers, user features and match storage; `/matches/search` output is unchanged.
**No CORS change is needed**: requests are made by the extension's background worker, which has host permission.

## What the interface contains
- **Settings page** (opens on install; Chrome toolbar icon opens it too): *Find issues* (ranked list from `POST /matches/search` with `persist:false`, filters language / topic / difficulty target, optional "look in a specific repository" which makes the backend fetch it from GitHub, Save/Unsave, expandable details loading difficulty on demand), *Saved issues* (from `GET /saved-issues`, with a score where `GET /matches` has a stored one), *Profile* (tag-style languages, tools, interests with a three-step level; optional preferences folded away; completion indicator computed from the saved profile), *Account* (backend URL, pairing-code sign-in, sign out, sign out everywhere).
- **Panel on a GitHub issue page**: title and repository, match score with a plain-words reading and the first reasons, "You may want to learn this first" (the match's missing skills), Save/Unsave, difficulty (maintainer label; ML estimate marked experimental), and collapsed sections for the score breakdown, repository health, contribution readiness and feedback.
- Not offered because not implemented: explaining an issue in free text, finding similar issues, a learning path.
- New background messages (options page only): `matches.search`, `matches.stored`, `issue.save`, `issue.difficulty`.

## Build
Requires Node 20+ (developed on 24). From `extension/`:
```
npm install
npm run check          # type-check + 317 unit/component tests + development build (build self-verifies security/packaging invariants)
npm run build          # development build -> extension/dist (backend http://localhost:8000 by default)
COMPASS_BACKEND_URL=https://compass.example.com npm run build:production   # production build -> extension/dist
```
**Production build:** `COMPASS_BACKEND_URL` is required, must be an `https` origin with no path or credentials, and is pinned: the manifest has that one
host permission (no localhost, no optional wildcard), the settings page cannot point elsewhere, a stored development URL is ignored, no source maps
are emitted, and `scripts/verify-dist.mjs` fails the build if `dist` contains a localhost URL, a source-map reference or a token-shaped string
(tests: `extension/tests/build.test.ts`). Replace the example host with your own deployment; it is not a real service. A development build is
what the browser e2e suite loads.

## Install (Chrome "Load unpacked")
1. `npm run build` (development) or the `build:production` command above, in `extension/`.
2. Open `chrome://extensions`, switch on **Developer mode**.
3. **Load unpacked** and select the `extension/dist` folder. The settings page opens.
4. Ask whoever runs the backend for a **pairing code** (on the backend machine): `python -m app.cli issue-pairing-code --github-id <id> --username <name>`.
   It is single use and expires in 10 minutes.
5. In the settings page's **Account** tab enter the backend URL (default `http://localhost:8000`), paste the code, leave
   **Keep me signed in** off unless you accept its trade-off (below), and press **Sign in**. The code is never stored.
6. Optionally fill in the **Profile** tab so scores are personalised, then open any GitHub issue
   (`https://github.com/<owner>/<repo>/issues/<n>`); the panel appears in the sidebar (or floats at the top right if GitHub has not
   rendered its sidebar).

Using a backend other than localhost needs **https**; Chrome will ask permission for that one origin when you Sign in or Save.
Changing the backend URL signs you out. Note: branded Chrome 137+ ignores the `--load-extension` command-line flag (irrelevant to
"Load unpacked", but relevant to automation).

## Security design and authentication
Full lifecycle, storage matrix, failure-mode tests and **residual risks**: [AUTHENTICATION.md](AUTHENTICATION.md). Summary:
- **Short-lived access token (15 min, memory-only `chrome.storage.session`) + rotating refresh token + server-side revocation.**
  The refresh token is also memory-only unless you tick **Keep me signed in**, which writes it **unencrypted** to the browser profile
  (`chrome.storage.local`). Chrome offers extensions no secure storage; this extension does not claim otherwise.
- Tokens are used **only by the background service worker**. The options page and the panel talk to it by validated messages and never
  receive a token. The content script (beside GitHub's page) never imports credential code: the build fails if `content.js` contains
  `Authorization`, `Bearer`, `chrome.storage`, any storage key, `refresh_token`, `access_token` or an auth endpoint.
- The worker sends credentials only to the configured origin (https, or http for localhost; origin only, no credentials/path), with
  `credentials: omit`, `redirect: error`, `cache: no-store`. It validates every message (schema; sender is this extension; account,
  profile and saved-list messages only from the extension's own pages; panel messages only from a GitHub issue tab and only for
  *that* issue) and every response (zod; malformed => error state, nothing guessed). Errors never include tokens or response bodies.
- 401 handling: renew once and retry once; a refused renewal ends the session, wipes credentials and the panel says "Your session
  ended". No retry loops; a rate-limit answer holds the **Try again** button until the stated wait elapses.
- Permissions: `storage`; host access to `http://localhost:8000` and `127.0.0.1:8000`; content script on `https://github.com/*`
  (GitHub navigates without page loads, so the script must already be there; it reads only `location.href` and adds one element).
  Any other backend origin is an **optional** https permission requested at runtime for that one origin. No tabs, history, cookies,
  scripting, web requests or `<all_urls>` (a test enforces this).
- Data sent: owner, repo and issue number of the issue you open; profile edits and saved/feedback records are stored under your
  account. No private-repository content is read or transmitted.
- The panel lives in a shadow DOM; links from backend data are rendered only if they are `https://github.com/...`.

## Browser-level verification

### A. Controlled page (run 2026-11-01, repeated after the Stage 13 changes)
`node e2e/run.mjs` (after `npm run build`) loads the built extension into Playwright's Chromium, starts the real backend on
localhost with a throwaway SQLite database of fictional data (`acme/ml-api`), and serves a controlled `github.com` issue page by
request interception (the real github.com is not contacted). It signs in with a real pairing code and a **45 s access-token TTL**
so renewal and expiry are real. **Stage 14: 29/29 checks passed on Chrome 156, Edge 156 and Chromium 151, in light and dark (`BROWSER=… COLOR_SCHEME=… node e2e/run.mjs`; full matrix and what was not tested: [BROWSER_TEST_REPORT.md](BROWSER_TEST_REPORT.md)); the Stage 13 run was 25/25 and the new checks cover colour-scheme contrast, narrow widths and keyboard reachability:** sign-in prompt; wrong/mistyped pairing codes rejected and nothing stored;
one-time code (reuse refused), credentials only in the documented storage areas, hashed on the server; score, breakdown, difficulty
labelling, keyboard-expandable explanation; on-demand health and readiness; save/unsave and feedback written to the database and linked to
**one** stored match; **saved state shown on a fresh page load**; unavailable actions send nothing; in-page navigation; floating vs sidebar;
page scripts cannot see tokens or `chrome.storage`; **profile edit** persisted (and invalid input blocked locally); **saved-issues list**
with safe links and remove; **background token renewal with refresh-token rotation**; an **expired access token refused** by the
server; **refresh-token reuse revokes the session**; backend-down message and recovery; **sign-out revokes an unexpired access token
immediately**; "Keep me signed in" persists the refresh token only and survives a simulated browser restart; **server-side revocation
is noticed and shown as "session ended"**; no page script errors. Screenshots: `extension/e2e/screenshots/` (git-ignored).

### B. Real github.com (run 2026-11-01, one page, one network, one machine)
`node e2e/real-github.mjs psf/requests#1` (nothing intercepted; backend on localhost using GitHub's **unauthenticated** API, no
personal token). **Observed, not asserted:**
- The real issue page loaded (HTTP 200); the panel injected and showed the sign-in notice; sign-in with a pairing code worked.
- **Placed in GitHub's real sidebar** (visible in the screenshot; one of the extension's three sidebar selectors matched, which one was not recorded, and the script's first placement probe was too strict to say), GitHub's own page intact (screenshot).
- Match score shown with the honest "no profile" low-confidence notice; difficulty section showed "no ML model is configured"
  (ML stays absent/experimental); repository health loaded on demand; save worked and **"Already in your saved issues" appeared
  after a reload**; no page script or extension console errors.
- GitHub API requests used: **2** for sign-in + first open, **0** for a reload, **0** for navigating to the issue list and back,
  **14** for the first repository-health analysis (detail: [CACHING_AND_RATE_LIMITS.md](CACHING_AND_RATE_LIMITS.md)).
- **Defect found only on the real site, then fixed and re-observed there:** after GitHub's in-page *back* navigation the panel mounted
  before GitHub had rendered its sidebar (`data-testid="issue-viewer-metadata-pane"`, absent for the first ~0.6 s) and stayed floating
  over the page. `createController` now moves a floating panel into the sidebar once it appears (unit-tested). Four later real
  runs polled the placement: in 3 the panel started floating and was inside the sidebar 0.5-1.1 s after mounting; in 1 it was in
  the sidebar immediately. (Repeating a measurement 4 times on one page is not a statistical claim; it shows the mechanism works.)
- **Not observed on the real site:** rate-limit errors in the panel (the quota was never exhausted during a run), authentication expiry
  and re-auth (covered on the controlled page), dark mode, other Chrome versions/OSes, Edge/Firefox, GitHub's logged-in UI (the run
  was a logged-out visitor), other layouts (pull requests are intentionally ignored), issues from private repositories.
  These remain **unverified on real github.com**; unit tests and the controlled-page run do not substitute for them.

## Manual testing checklist (real browser, real github.com)
Mark each item only after doing it. Items marked (B) were observed in run B above for one logged-out page.
- [ ] Load unpacked works; settings page opens; Sign in with a pairing code shows "Signed in as <you>". (B: pairing sign-in)
- [ ] On a real issue page the panel appears (sidebar or floating) and GitHub's own page still works (comment box, labels, sidebar). (B: sidebar placement only)
- [ ] Navigate issue to issue via links without reloading: the panel follows; on a list or PR page it disappears. (B: issue -> list -> back)
- [ ] After back/forward navigation the panel ends up in the sidebar, not floating over the page. (B: observed on one page, within ~1 s)
- [ ] Dark mode (GitHub appearance settings): panel is readable. (B/Stage 14: real GitHub `auto` light and dark observed in Chrome 156 and Edge 156, contrast 15.8:1 / 17.4:1; GitHub's other themes not tested)
- [ ] Narrow window / zoom 200%: no horizontal overflow; keyboard-only: Tab reaches every button, Enter/Space toggles sections. (Stage 14: automated on the settings page and the floating panel; not on the real GitHub page; no screen reader)
- [ ] Session ended (sign out from another device / `revoke-sessions`): panel says "Your session ended" and offers Open settings. Stop the backend: panel says it cannot reach it.
- [ ] Let the access token pass 15 minutes of use: no prompt, requests keep working (renewal).
- [ ] Save, Remove, feedback buttons work; the saved indicator shows on reload; repeat clicks do not create duplicates. (B: save + indicator)
- [ ] Profile tab: create, edit, invalid input is refused; a changed profile changes scores.
- [ ] Saved tab: list, open link in a new tab, remove.
- [ ] Issue with no profile: low-confidence notice, unknown components shown as Unknown. (B)
- [ ] Force a GitHub rate limit (use an exhausted unauthenticated backend): the panel shows the wait, **Try again** stays disabled until it ends, partial results still show. (Stage 14: observed on real GitHub in Chrome 156; not on Edge)
- [ ] Microsoft Edge: Load unpacked through `edge://extensions`, then repeat the items above. (Stage 14: automated suite 29/29 and real-page rendering only; sideloading UI untested)
- [ ] Logged-in GitHub account (Chrome and Edge): panel placement and colours with GitHub's logged-in layout. (NOT TESTED)
- [ ] A repository the backend cannot read (private/nonexistent): clear error, no data shown.

## Remaining limitations
- Real-github.com coverage is one logged-out page on one day (section B); GitHub's markup changes, so sidebar selectors may need updating (fallback: floating panel).
- Chrome only (Manifest V3); Edge/Firefox untested. Requires a recent Chrome (110+ declared).
- Sign-in needs an administrator-issued pairing code; no GitHub OAuth yet. See residual risks in [AUTHENTICATION.md](AUTHENTICATION.md).
- Health/readiness cost ~14-16 GitHub requests on first use of a repository; without a server-side GitHub token a quota of 60/hour allows only a few.
- Cached answers (5-10 min, worker memory) can lag behind a backend change; they are cleared on sign-in/out, settings and profile changes.
- The ML difficulty estimate is experimental and usually unavailable (no approved model exists); heuristic labels are not verified ground truth.
- No automated visual-regression or cross-browser test suite; the end-to-end scripts are local tools, not part of CI.
