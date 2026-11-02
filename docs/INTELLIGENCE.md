# Repository intelligence (Stage 9)

Two **separate** explainable reports, both heuristic summaries of observed GitHub data. They are not ML
predictions, not calibrated probabilities, and their formulas have **not been validated** against any outcome.

| Report | Question it answers | Endpoint |
|---|---|---|
| Repository health | Is the project maintained and responsive? | `GET /api/v1/repositories/{owner}/{repo}/health` |
| Issue readiness | How well does this issue + repository prepare a newcomer to start? | `GET /api/v1/issues/{owner}/{repo}/{number}/readiness` |

Code: `app/intelligence/` (pure analyzers, no I/O), `app/services/intelligence.py` (collection + persistence),
`app/services/intel_api.py` (responses). Both endpoints require auth; the data is public and identical for every user.

## Signal statuses (applies to both reports)
| Status | Meaning | Effect on the score |
|---|---|---|
| `known` | Computed from retrieved data. A score of 0 means **genuinely low**. | `effective = confidence*score + (1-confidence)*25` |
| `unknown` | Could not be retrieved / never collected. | counts as the fill value 25, coverage 0 |
| `stale` | Retrieved but too old to trust. | same as unknown |
| `not_applicable` | Data retrieved and shows the signal does not apply (e.g. a project with no GitHub releases, or no issue traffic in the window). | **excluded**; remaining weights renormalised |

`score = sum(w*effective) / sum(w over applicable signals)`; `coverage = sum(w*confidence) / sum(w over applicable)`.
Unknown data therefore cannot raise a score: it is pulled toward 25, below any good evidence and above only
genuinely bad evidence. Because confidences are capped (0.35-0.8), even a perfect repository scores below 100.

## Repository health

**Windows.** `COMPASS_INTEL_WINDOW_DAYS` (default 90, 14-365) ending at collection time. Absolute timestamps are
stored; ages are computed at read time. Pagination is bounded: commits 3 pages, issues 3, PRs 3, comments 3 (100 per
page); the response says when a window count is only a lower bound. A complete analysis costs about 10-16 GitHub
requests (commits 1-3, releases 1, issues 1-3, PRs 1-3, comments 1-3, file tree 1, README/CONTRIBUTING up to 2).
Unauthenticated GitHub allows 60 requests/hour, so use a token for more than a few repositories.

| Signal (weight) | Calculation | Confidence |
|---|---|---|
| Commit activity (0.30) | 0.5 x recency (last commit <=30d 100, <=90d 75, <=180d 45, <=365d 20, else 0) + 0.5 x consistency (active weeks / 4, capped at 1, of the 13 weeks in a 90-day window). | 0.7 if 0 commits in window, 0.4 for 1-2, 0.6 for 3-9, 0.8 for 10+ |
| Release cadence (0.10) | Days since latest published (non-draft) release: <=90d 100, <=180d 75, <=365d 50, <=730d 25, else 10. Median gap of the latest releases is reported. **No releases = not applicable**, not a penalty. | 0.5 (3+ releases) else 0.35 |
| Issue/PR flow (0.15) | `min(1, handled/opened)` over issues+PRs: handled = issues closed + PRs merged + PRs closed unmerged in the window. No traffic at all = not applicable. | 0.8/0.6/0.4 by volume (opened >=10 / >=3 / fewer), x0.75 if only partly retrieved |
| Maintainer responsiveness (0.30) | 0.5 x response rate x 100 + 0.5 x speed (median hours to first maintainer comment: <=24h 100, <=72h 85, <=168h 65, <=720h 35, else 10). Based on **issues** opened in the window by non-maintainers. | 0.8 (8+ items), 0.6 (3+), else 0.35; minus 0.2 if comments were truncated |
| Maintenance indicators (0.15) | Archived = 0. Otherwise license (+35), CI config (+35), dependency-update bot config (+30); needs a complete file listing. | 0.5 |

Definitions: *maintainer* = comment author association OWNER, MEMBER or COLLABORATOR, excluding bots (`[bot]`) and the
item's own author. An unanswered item younger than 7 days is "pending" and excluded from the response rate. The
issue-resolution time only covers issues that are already closed (optimistic for slow projects). PR response times
(reported, not scored) count PR *conversation comments only*: formal reviews are a different API and are not counted,
so they understate review responsiveness.

**Not treating one commit as health / small repositories.** Consistency needs several active weeks, so a single
recent commit scores 62.5 at confidence 0.4 and warns. The label `healthy` requires score >= 70 **and** at least
three signals with confidence >= 0.5 and score >= 60. If only one or two signals exist the label is
`insufficient_data` (coverage < 0.4). Small projects (full history seen, fewer than 50 commits) with sparse commits
get a warning; if the score is low but maintainers respond well (score >= 70) and a commit exists within a year the
label is `stable_or_slow` rather than `low_activity`. Labels: `healthy`, `moderate`, `stable_or_slow`,
`low_activity`, `inactive`, `archived`, `insufficient_data`.

The report also gives group scores (`activity`, `responsiveness`, `maintenance`) so responsiveness is not blended
into activity unseen.

## Issue readiness
| Signal (weight) | Calculation | Confidence |
|---|---|---|
| Contribution guide (0.20) | CONTRIBUTING.{md,rst,txt} (root, `.github/`, `docs/`) 100; equivalent doc (e.g. `docs/development.md`) 75; README "Contributing" section 55; confirmed absence (complete file listing) 15; truncated listing = unknown. | 0.8 / 0.6 / 0.5 / 0.6 |
| Tests (0.20) | Test files **detected by path** (`tests/`, `test_*.py`, `*_test.go`, `*.test.ts`, ... vendored dirs ignored) 60, +40 if README/CONTRIBUTING mention a test command (pytest, npm test, make test, cargo test, go test, ...). None detected 10 (30 if a command is mentioned). Truncated listing = unknown. | 0.6 (0.45 if README/CONTRIBUTING unreadable) |
| Issue description (0.20) | Share of rubric checks found. Bug rubric (label `bug` or bug-like title): length >= 200 chars, reproduction steps, expected vs actual, code/log snippet, environment/version. Otherwise: length, acceptance criteria/"should"/checklist, example or link. Missing checks are listed as warnings. | 0.6 |
| Linked pull requests (0.20) | From the issue timeline's cross-references: none 100; only closed-unmerged 70; merged (issue still open) 40; any open 25. Never labelled a duplicate. Not checked = unknown; data older than 3 days = stale. | 0.6 |
| Labels (0.05) | `good first issue`-style 100, `help wanted`-style 75; otherwise unknown (absence is not evidence). | 0.5 |
| Availability (0.10) | Open + unassigned 100; assigned 30; closed -> whole report `not_available` (score 0). Issue data older than 7 days = stale. | 0.6-0.7 |
| Other documentation (0.05) | README 40, PR template 20, issue templates 15, code of conduct 15, security policy 10. | 0.5 |

Labels: `well_prepared` (>= 65), `partially_prepared` (>= 45), `poorly_prepared`, `insufficient_data` (coverage < 0.4),
`not_available`. The thresholds are lower than they look because the confidence caps make ~72 the practical maximum.

**Detection is not execution.** "Tests detected" means matching file paths exist. Nothing was run, so the report cannot
say tests pass, that dependencies install, or that the project builds; every tests signal says so. Running tests belongs
to the (not yet built) sandboxed Project Lab.

**Linked PRs are cross-references.** GitHub "cross-referenced" timeline events show that a PR *mentioned* the issue.
They can miss manually linked PRs, include PRs that do not fix the issue, and are limited to 3 timeline pages.
Only same-repository PRs are classified; other-repository references are counted separately.

## Persistence and freshness
- Repository analysis: `repository_snapshots` (`analysis_version = intel_v1`, unique per repository + head commit SHA +
  version; `data` JSONB holds observations, file-detection results, per-signal errors, `collected_at`). The head
  commit SHA pins the analysis (file tree is fetched by SHA); if commits cannot be read the tree SHA, then a 40-zero
  sentinel, is used.
- Linked PRs: `issue_features` (`feature_version = readiness_v1`, unique per issue + version, updated in place).
- Reuse: complete analyses are served without GitHub calls for 24h (`COMPASS_INTEL_SNAPSHOT_TTL_SECONDS`); partial ones
  for 15 min so a rate-limited collection is retried soon; linked PRs for 1h. `?refresh=true` is ignored for data younger
  than 30s. Analyses older than 14 days are flagged stale and treated as unknown when scoring. A partial collection never
  overwrites a complete analysis of the same commit.
- Failure handling: collection tolerates per-signal failure and records error codes. After a rate-limit response the
  remaining requests are skipped (`skipped_after_rate_limit`). If nothing at all can be fetched the mapped error is
  returned (429 with Retry-After, 502, 504...) unless an older snapshot exists, which is then served with
  `freshness.refresh_error` set. Readiness still returns the locally computable signals with `upstream_errors`.

## Known limitations
- Never run against live GitHub; only mocked responses. Real payload quirks may appear.
- Response analysis uses issue/PR conversation comments only, oldest first; very busy repositories hit the comment page
  limit and the rate is understated (flagged).
- Weights, bands and confidences are untuned judgement calls.
- Test detection is path-based; monorepos, unusual layouts and non-English docs are poorly handled. Instruction
  detection reads only the README and the contribution guide (<= 200 KB each).
- Issue-quality checks are word patterns; they reward formulaic text and miss good prose.
- The file tree call can be truncated by GitHub for very large repositories (flagged, absence not concluded).
- A search cannot trigger per-issue linked-PR lookups (one request each); use the readiness endpoint first.
