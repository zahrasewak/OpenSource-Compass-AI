# Browser test report (Stage 14, 2026-11-01/02)

How to read this: **PASS** = executed and the expected result was observed; **NOT TESTED** = not executed (reason given);
**DEFECT (fixed)** = a problem found by testing and corrected. Nothing is marked PASS from unit tests alone; where a result rests on
a script that only *observed* (did not assert), it says so. The reusable manual checklist is in [EXTENSION.md](EXTENSION.md).

## 1. Environment
| Item | Value |
|---|---|
| OS / machine | Windows 11 Home 10.0.26200, one developer machine, one network |
| Browsers | **Google Chrome 156.0.8078.48**, **Microsoft Edge 156.0.4314.42**, Playwright Chromium 151.0.7922.34 (all headless) |
| Driver | Playwright 1.64 (`playwright-core`); no human operated these browsers. Chrome >= 137 ignores `--load-extension`, so Chrome was loaded through the DevTools method `Extensions.loadUnpacked`; Edge 156 still honours the flag with `--disable-features=DisableLoadExtensionCommandLineSwitch`. **Loading through the `chrome://extensions` "Load unpacked" UI was NOT tested** |
| Extension | build from `extension/` after Stage 14 changes (263 unit tests passing) |
| Backend | the real FastAPI backend on `localhost:8000`, SQLite, access tokens shortened to 45 s for session tests |
| GitHub | (A) a request-intercepted `github.com` page with fictional data; (B) the **real github.com** as a **logged-out visitor**, with the backend using GitHub's **unauthenticated** API (no personal token) |
| Not available / not done | a logged-in GitHub account (the owner's credentials were not used), Firefox/Safari, macOS/Linux, other Chrome/Edge versions, screen readers, mobile |

Scripts: `extension/e2e/run.mjs` (A; `BROWSER=chrome|msedge|chromium`, `COLOR_SCHEME=light|dark`), `extension/e2e/real-github.mjs` and
`extension/e2e/real-github-extra.mjs` (B). Screenshots are git-ignored under `extension/e2e/screenshots*/`.

## 2. Automated suite on the controlled page (A): 29 checks per configuration
Covers sign-in/pairing, wrong code, credential storage layout, match/difficulty/health/readiness, keyboard explanation, save/unsave,
saved indicator on a fresh load, feedback (linked to one stored match), disabled actions, in-page navigation, floating vs sidebar placement,
page-script isolation, profile editing, saved list, token renewal/rotation/expiry/reuse, backend down and recovery, sign-out
revocation, "keep me signed in" and restart, server-side revocation, plus (new in Stage 14) colour-scheme and contrast checks, narrow-width
overflow, and keyboard reachability.

| Browser | Colour scheme | Result |
|---|---|---|
| Chrome 156.0.8078.48 | light | **29/29 PASS** |
| Chrome 156.0.8078.48 | dark | **29/29 PASS** |
| Edge 156.0.4314.42 | light | **29/29 PASS** |
| Edge 156.0.4314.42 | dark | **29/29 PASS** |
| Chromium 151 (Playwright) | light | **29/29 PASS** |

Limits: these pages are controlled stand-ins, so they do not prove anything about GitHub's real markup (see section 3).

## 3. Scenario matrix
Browsers abbreviated: C = Chrome 156, E = Edge 156.

### 3.1 GitHub light and dark modes
| Scenario | Expected | Actual | Status |
|---|---|---|---|
| Real GitHub, light (C, E) | Panel uses GitHub's colours; readable | GitHub `data-color-mode=auto`, theme light; panel bg `rgb(255,255,255)` text `rgb(31,35,40)`, contrast 15.8:1; uses GitHub's CSS variables | **PASS** (observed) |
| Real GitHub, dark (C, E; OS scheme dark) | Panel follows GitHub dark | theme dark; panel bg `rgb(13,17,23)` = GitHub's page, contrast 17.4:1; screenshot reviewed | **PASS** (observed) |
| Settings page, dark and light (C, E, controlled) | Every text element >= 4.5:1 | 46 elements sampled; lowest 6.11:1 (light) and 6.50:1 (dark) | **PASS** after fix |
| Settings page, dark, first run | Readable | **Active tab label was dark text on a dark tab; help text too dim** (found from a screenshot) | **DEFECT (fixed)**: colours moved to variables with a full dark set; automated contrast check added |
| Panel when GitHub's CSS variables are absent | Follow the OS scheme | had light-only fallbacks | **DEFECT (fixed)**: dark fallbacks added; checked on the controlled page (no GitHub variables there): dark and light PASS |
| GitHub's other themes (dark dimmed, high contrast, colour-blind) and explicit light/dark overrides | readable | needs a logged-in appearance setting | **NOT TESTED** |

### 3.2 Rate-limit errors and partial results
| Scenario | Expected | Actual | Status |
|---|---|---|---|
| Real GitHub quota exhausted on purpose (C) | Clear error, wait shown, no retry loop | Match for a not-yet-stored issue: "Rate limited by the backend or GitHub. Try again in about 49 min."; **Try again disabled, labelled "Try again in 49 min"** | **PASS** (observed) |
| GitHub requests while limited (C) | None | Quota 0 before and after, across the panel, a readiness expansion and a second issue | **PASS** (observed) |
| Readiness for a *stored* issue while limited (C) | Partial results, not an error | Screenshot shows readiness with known signals (contribution guide 100, tests 60, issue description 33...), "Linked pull requests: Unknown", "Linked pull requests have not been checked." The script's text probes did **not** match, so this is evidenced by the screenshot, not an assertion | **PASS** (screenshot-verified) |
| Repository health for a stored analysis while limited (C) | Stored analysis shown | Script probe timed out; not otherwise verified on the real site | **NOT VERIFIED** (the stored-analysis path is covered by mocked tests only) |
| Same on Edge | as above | quota was already exhausted by the Chrome run; not repeated | **NOT TESTED** live on Edge (mocked tests cover the logic) |
| Rate-limit UI logic | countdown holds the button | unit tests (handler, cache, panel) | PASS (unit only) |
| Observation | | One first-time health analysis (`psf/requests`) did not finish within 90 s while others did, and the quota kept dropping afterwards, suggesting the backend continues work after the panel stops waiting | **FINDING** (inferred; recorded in [GITHUB_API_STRATEGY.md](GITHUB_API_STRATEGY.md)) |

### 3.3 Logged-in, logged-out, expired-session and re-authentication
| Scenario | Expected | Actual | Status |
|---|---|---|---|
| GitHub visitor logged out, extension signed out (real, C/E/Chromium) | Panel in the sidebar with a sign-in notice | observed | **PASS** |
| **GitHub logged in** | panel works with GitHub's logged-in layout | requires a GitHub account | **NOT TESTED** (the owner's credentials were not used) |
| Extension signed in (pairing code) (real C, E, Chromium) | scores shown | observed | **PASS** |
| Access token expires (45 s) during use (real, C) | action still works, token renewed in the background | "Remove" clicked 50 s after sign-in succeeded; `last_refreshed_at` set | **PASS** (observed) |
| Same on controlled pages (C, E, Chromium) | renewal, rotation, expiry refused by server, reuse detected | steps 18-20 | **PASS** |
| Administrator revokes the session (real, C) | next action shows an ended-session message; reload says "Your session ended." | observed both | **PASS** |
| Re-authentication with a new code (real, C) | panel works again and saving works | observed | **PASS** |
| Sign-out revokes at once; "keep me signed in" and restart; server-side revocation noticed (C, E, Chromium) | per design | steps 22-24 | **PASS** |

### 3.4 Navigation between GitHub issue pages
| Scenario | Expected | Actual | Status |
|---|---|---|---|
| Controlled page, pushState navigation issue -> issue -> list -> PR -> back (C, E, Chromium) | one panel; follows; removed on non-issue pages | step 13 | **PASS** |
| Real GitHub: issue -> issue list -> back (Chromium earlier; polled placement 4 more times) | panel removed, then restored | restored every time | **PASS** |
| After back-navigation the panel ends up in the sidebar | not floating | **DEFECT (fixed)**: it mounted before GitHub rendered the sidebar and stayed floating; now moves into the sidebar. Re-observed on the real site: in 3 of 4 runs it floated first and was in the sidebar 0.5-1.1 s later; in 1 immediately | **PASS** (observed after fix) |
| Real GitHub: clicking a link from one issue straight to another issue | follows | not performed (no linking pair of stored issues; a new issue would spend quota) | **NOT TESTED** |

### 3.5 Saved-issue state, save/unsave, feedback
| Scenario | Expected | Actual | Status |
|---|---|---|---|
| Save then reload (real, C, Chromium) | "Already in your saved issues." | observed | **PASS** |
| Unsave (real, C) | returns to "Save issue" | observed | **PASS** |
| Saved indicator on a fresh tab; unsaved issue not shown as saved (controlled, C, E, Chromium) | per design | step 10 | **PASS** |
| Feedback buttons and duplicates (controlled, C, E, Chromium) | stored, linked to one match, repeat = duplicate | step 11 | **PASS** |
| Feedback on the real site | stored | not performed | **NOT TESTED** (real site) |

### 3.6 Microsoft Edge
| Scenario | Status |
|---|---|
| Controlled suite, light and dark (29 checks each) | **PASS** |
| Real GitHub: light and dark rendering, sign-in, match score | **PASS** (observed) |
| Real GitHub: rate limit, session expiry/re-auth, save/unsave | **NOT TESTED** on Edge (done on Chrome only) |
| Edge installed from the store / sideloading UI, enterprise policies, Edge-specific `chromiumapp.org` host (needed later for OAuth) | **NOT TESTED** |

### 3.7 Loading, empty and error states
| State | Evidence | Status |
|---|---|---|
| Loading (each panel section, profile, saved list) | unit/component tests; seen live on real pages | PASS |
| Empty: saved list "Nothing saved yet" | controlled step 17 asserts it after the last removal | **PASS** |
| Empty: no profile ("unknown, not a perfect match") | real page shows the low-confidence notice; options text unit-tested | **PASS** (real) / unit |
| Error: backend unreachable, recovery | controlled step 21 | **PASS** |
| Error: authentication / session ended | real (C) and controlled | **PASS** |
| Error: rate limited | real (C) | **PASS** |
| Error: unknown issue (404) | controlled step 14 | **PASS** |
| Error: malformed response, validation errors | unit tests | PASS (unit only) |

### 3.8 Accessibility and layout (automated checks only)
| Check | Result |
|---|---|
| Keyboard: every control on each settings tab reachable with Tab and shows a focus outline (C, E, Chromium, light and dark) | **PASS** |
| Panel explanation toggle by keyboard | **PASS** (step 6) |
| No horizontal overflow of the settings page at 640 px and 400 px, and the floating panel stays inside a 400 px viewport | **PASS** |
| Screen readers (NVDA/VoiceOver), full panel tab order on the real page (inside the shadow DOM), 200% zoom on real GitHub, reduced motion | **NOT TESTED** |

## 4. Defects and findings from this round
1. **Settings page unreadable parts in dark mode** (invisible active tab, dim help text): fixed, automated contrast check added.
2. **Panel lacked dark fallbacks** when GitHub's CSS variables are missing: fixed.
3. **Panel stayed floating after GitHub's back-navigation** (found on the real site in Stage 13): fix re-observed.
4. **Backend work continues after the client gives up** (inferred from quota readings): documented, not fixed (Stage 14 is validation only).
5. **One unit test (`shows 'checking' while the saved state loads…`) failed once** during a `npm run check` and then passed in 5 consecutive
   full runs and 4 parallel runs. The cause was not found; it is reported as an unexplained flake.
6. My first contrast probe misreported a stale build (a check bug, not a product result); the reported numbers are from the corrected run.

## 5. Still to do by hand (owner)
Logged-in GitHub in Chrome and Edge, GitHub's other themes, "Load unpacked" through the browser UI on each browser, clicking from issue to issue
on the real site, feedback on the real site, rate-limit and session scenarios on Edge, a screen-reader pass, other OSes and browser
versions. Mark each item in the [EXTENSION.md](EXTENSION.md) checklist only after doing it.

---

# Phase 7 re-run (master directive, 2026-11-02): scenarios 1-17

Sections 1-5 above are the Stage 14 record and stay as written (29 checks then). The suite has since grown to **35 checks** (new: step 0
fresh install, 23b sign out everywhere, 23c slow backend, 23d worker restart, 24b lockout, plus the Stage 15 additions) and was run again.

## 6. Environment of this round
| Item | Value |
|---|---|
| OS | Windows 11 Home 10.0.26200, one machine |
| Browsers | Google Chrome 156.0.8078.48, Microsoft Edge 156.0.4314.42 (both headless, driven by Playwright 1.64). **Playwright Chromium 151 was NOT re-run this round** (its Stage 14 result above is older) |
| Extension | development build of `extension/` (default backend `http://localhost:8000`), loaded from `extension/dist`; Chrome through `Extensions.loadUnpacked`, Edge through `--load-extension`. **The `chrome://extensions` "Load unpacked" UI was not used** |
| Backend | the real FastAPI backend (SQLite), started by the test; `slow_app.py` wrapper only in step 23c |
| GitHub | request-intercepted `github.com` page with fictional data (controlled). Real github.com results are the Stage 13-15 observations in sections 3.1-3.6 and were **not repeated** in this round (unauthenticated quota is 60/hour and was not spent again) |
| Operator | none: automated; no human looked at the screenshots except where stated |

## 7. Results per configuration (35 checks each)
| Browser | Scheme | Result | Notes |
|---|---|---|---|
| Chrome 156 | light | **35/35** | the third run of the day. Earlier runs the same day: 31/35 and 32/35, caused by defects in the new *test code* (step 12 read the host element's text instead of the shadow DOM; step 23d tried `chrome.runtime.reload()`, see 8) fixed in the test, not in the extension |
| Chrome 156 | dark | **35/35** | one run |
| Edge 156 | light | **35/35** | one run |
| Edge 156 | dark | **35/35** on the second run; first run **33/35** | first run: step 23c timed out waiting for the "did not answer in time" message (60 s), and step 23d failed as a consequence (backend left in slow mode). The same step passed in Edge light, Chrome light and Chrome dark and in the immediate re-run. **Cause not found; recorded as an unexplained intermittent failure of the slow-backend step, not as fixed** |

## 8. Scenario table (the directive's 17 scenarios)
Status: PASS = asserted by the automated suite in the stated browsers; PASS (observed) = real-site observation from Stages 13-15, not repeated;
NOT TESTED / BLOCKED as stated. Evidence for "controlled" rows is the step number in `extension/e2e/run.mjs` and the run output; screenshots are
git-ignored under `extension/e2e/screenshots/<browser>-<scheme>/`.

| # | Scenario | Steps / expected | Actual | Status |
|---|---|---|---|---|
| 1 | Fresh install | step 0: signed out, default backend shown, Profile and Saved tabs locked, no credentials stored | as expected. Settings page opened by itself on install in Chrome (`yes`, printed by the run) | PASS (Chrome, Edge; controlled) |
| 2 | Logged-out use | step 1: panel appears and says to sign in | as expected | PASS (controlled); PASS (observed) on real github.com logged out |
| 3 | Sign-in | steps 2-3: wrong code rejected by the real backend, nothing stored; right code works once, reuse fails; credentials only in the designed storage areas | as expected | PASS |
| 4 | Expired or invalid auth | steps 18-20, 24: renewal and rotation; expired access token refused; refresh reuse ends the session; admin revocation shows "session ended" | as expected | PASS (controlled); PASS (observed) real site, Chrome |
| 5 | Open an issue page | steps 1, 4, 4f: panel in the sidebar of each supported layout, floating when there is none, late sidebar picked up | as expected on the five imitated layouts | PASS (controlled); real GitHub: PASS (observed) for the layout seen at that time. **GitHub changes its markup; only the layouts listed were exercised** |
| 6 | SPA navigation | step 13: pushState issue to issue to list to PR and back; exactly one panel | as expected | PASS (controlled); PASS (observed) real site |
| 7 | Explanations and difficulty | steps 4-6: heuristic labelling, unknown shown as unknown, maintainer-label hint, ML estimate shown as unavailable and experimental, explanation by keyboard | as expected | PASS |
| 8 | Profile editing | step 16: view, change, save; a changed profile refreshes scores | as expected | PASS |
| 9 | Save, reopen, unsave | steps 9-10 | as expected | PASS (controlled); PASS (observed) real site, Chrome |
| 10 | Saved list | step 17: lists the saved issue, safe link, removal, empty state | as expected | PASS |
| 11 | Feedback | step 11: stored, linked to one match, repeat is a duplicate | as expected | PASS (controlled); real site NOT TESTED |
| 12 | Light and dark | steps 4b, 4c plus every configuration run in both schemes | computed colours and WCAG AA contrast of sampled text pass in both | PASS (controlled, Chrome and Edge). GitHub's other themes: NOT TESTED |
| 13 | Loading, empty, error states | steps 14, 17, 21, 23c plus unit tests | as expected | PASS |
| 14 | GitHub rate limits and partial results | real-site Stage 14 observations (section 3.2); logic covered by unit and backend tests | clear wait message, no retry loop, partial readiness | PASS (observed, Chrome); NOT re-run this round; NOT TESTED on Edge live |
| 15 | Backend unavailable or slow | step 21 (down and recovery), 23c (7 s answer shows the loading state then the result; an answer slower than the extension's 30 s limit shows "did not answer in time" with a retry that works once the backend is fast again) | as expected | PASS (Chrome light/dark, Edge light, Edge dark on re-run); one unexplained failure, see section 7 |
| 16 | Browser refresh and extension reload | steps 10, 23 (fresh page load shows saved state; "keep me signed in" persists, a browser restart does not sign out) and 23d | **In Edge: `chrome.runtime.reload()` was run and the user stayed signed in.** **In Chrome the Reload operation was NOT exercised**: an extension loaded through the debugging protocol stays disabled after `chrome.runtime.reload()` (its pages answer `ERR_BLOCKED_BY_CLIENT`, and loading the folder again did not recover it in 15 s). Chrome's 23d therefore stops the service worker through the debugging protocol (all in-memory state lost) and checks that the user is still signed in and the panel works | PASS (Edge: reload; Chrome: worker restart only). Chrome "Reload" from `chrome://extensions`: NOT TESTED |
| 17 | Chrome and Edge | all of the above in both | see section 7 | PASS with the exceptions above |

## 9. Known gaps in browser coverage
Logged-in GitHub; GitHub's non-default themes; the `chrome://extensions` Load-unpacked UI and Reload button; Firefox and Safari (not supported);
macOS/Linux; screen readers; other browser versions; real-site rate-limit, feedback and save flows on Edge; the Playwright Chromium run for this
round. None of these was done, so none is claimed.

---

# Frontend redesign verification (2026-11-02, after the UI redesign)

The settings page was reorganised (tabs: Find issues, Saved issues, Profile, Account; a recommendations list; tag-based profile) and the GitHub
panel was reworked (summary and save first, reasons and "learn first" up front, breakdown/health/readiness/feedback behind toggles; the
"not available yet" list was removed). The browser suite was updated to the new UI (steps 4, 4c, 4d, 4e, 6, 9, 11, 12, 16, 17, 22, 23b, 23d, 24) and run again.

## Environment and commands
Windows 11 Home 10.0.26200; Google Chrome 156.0.8078.48; Microsoft Edge 156.0.4314.42 (headless, Playwright 1.64); development build loaded from
`extension/dist` (Chrome through `Extensions.loadUnpacked`, Edge through `--load-extension`); real FastAPI backend on SQLite; controlled github.com page.
```
cd extension
npm run build
BROWSER=chrome  COLOR_SCHEME=light node --no-warnings e2e/run.mjs
BROWSER=chrome  COLOR_SCHEME=dark  node --no-warnings e2e/run.mjs
BROWSER=msedge  COLOR_SCHEME=light node --no-warnings e2e/run.mjs
BROWSER=msedge  COLOR_SCHEME=dark  node --no-warnings e2e/run.mjs
```
## Results (automated)
| Browser | Scheme | Result | Failed | Skipped / blocked |
|---|---|---|---|---|
| Chrome 156.0.8078.48 | light | **35/35** | 0 | 0 |
| Chrome 156.0.8078.48 | dark | **35/35** | 0 | 0 |
| Edge 156.0.4314.42 | light | **35/35** | 0 | 0 |
| Edge 156.0.4314.42 | dark | **35/35** | 0 | 0 |

Each of these four is the first run of the final build. (Earlier in the same session the Chrome light run on the half-updated suite failed 4e and 12 twice;
causes were in the test: controls inside a collapsed `<details>` counted as reachable, the form was not yet loaded when the details were opened, and
a step demanded a "learn first" section for an issue with no missing skills. Fixed in `e2e/run.mjs`, no product change was needed.)
Step 4c (contrast of every sampled text element on Find issues, Profile and Account): lowest ratio **5.37:1** (light) and **4.63:1** (dark, white text on the primary green button), 163 elements.
Chrome's `chrome.runtime.reload()` is still not exercised (Chrome uses a service-worker restart); Edge exercises it.

## What the automated run does NOT cover (needs a person)
- Loading `extension/dist` through the browser's own "Load unpacked" screen (the tests load it through the debugging protocol / command-line flag).
- Visual judgement on the real github.com (the panel's colours come from GitHub's variables; only the controlled page and earlier logged-out observations were checked), logged-in GitHub, GitHub's other themes.
- The recommendation list against a backend with real, larger data; "Look in a specific repository" (needs GitHub quota; covered by unit tests with a fake worker, not by a browser run).
- Screen reader behaviour, 200% zoom on the real site, reduced-motion preference, Firefox/Safari (unsupported), macOS/Linux.
