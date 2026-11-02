# Recommendation baseline (Stage 7)

`POST /api/v1/matches/search` ranks stored open issues for the **authenticated** user with a
**heuristic baseline. It is not an ML prediction.** Scores are on an arbitrary 0-100 scale, are
not probabilities, and have **not been validated** against any contribution outcome. Passing tests
shows the rules are implemented as documented, not that the rankings are good.

## Code layout (ML-ready)
- `app/recommend/types.py` — `UserFeatures`, `IssueFeatures`, `ScoredIssue`, and the `Scorer`
  protocol (`info` + `score(user, issue, now)`). Plain data only: no DB or HTTP objects.
- `app/recommend/components.py`, `heuristic.py` — the baseline. Pure functions, no I/O.
- `app/services/matches.py` — loads data, filters, calls the scorer, sorts, paginates.
- `app/api/deps.py::get_scorer` — the single place to swap in another scorer. A trained model only
  has to implement `Scorer`; the same `UserFeatures`/`IssueFeatures` inputs let it be evaluated
  against the baseline on one dataset (a test swaps in a fake scorer to prove the endpoint doesn't
  depend on the heuristic).

## Components (each 0-100, or `null` = unknown)
| Component (weight) | Computation | Evidence limits |
|---|---|---|
| Skill compatibility (35%) | Technologies detected in issue title/body/labels, repo topics, plus repo primary language (counts double). Score = weighted share of those technologies the user has, using self-reported proficiency. Skills below 0.3 are listed as `missing_skills`. | Keyword vocabulary of ~45 technologies; misses anything unlisted. Confidence 0.4+0.2·n (max 0.85). |
| Interest similarity (25%) | Share of the user's interests (denominator capped at 3) whose words all appear in issue/repo text. | Lexical, not semantic; a 0 may mean different wording. Confidence 0.6. |
| Difficulty suitability (20%) | Difficulty implied by labels (good first issue=1, easy=2, medium=3, hard=4, expert=5; highest wins) vs the user's target (request > profile `preferred_difficulty` > experience level beginner/intermediate/advanced = 2/3/4). Score = 100 − 30·distance. | Labels are noisy; **no trained difficulty model exists yet**. Unlabelled issues are unknown. Confidence 0.4. |
| Repository activity (15%) | Days since last push: ≤14→100, ≤30→85, ≤90→65, ≤180→40, ≤365→20, else 5; archived→0. | Last push only. Commit rate, releases, maintainer response time are not analysed. Confidence 0.7. |
| Contribution readiness (5%) | Open (+40), unassigned (+30), substantive description (+15), repo not archived (+15). | Linked PRs, contribution guide and test availability are **not checked**. Confidence 0.5, so it can never count as perfect. |

Weights are untuned defaults (`COMPASS_WEIGHT_*`, must sum to 1.0 or the app refuses to start).

## Missing-data strategy
Each component has a `confidence` in [0,1]; an unknown component has `score: null`, confidence 0.

```
effective_i = confidence_i * score_i + (1 - confidence_i) * FILL      (score known)
effective_i = FILL                                                      (unknown / stale)
final       = sum(weight_i * effective_i)         FILL = 25 (COMPASS_RECO_UNKNOWN_FILL)
coverage    = sum(weight_i * confidence_i)        (0 for unknown components)
```
- Missing information is **never** treated as perfect, and is **not dropped** (renormalising over
  known components would inflate scores built on a single signal).
- It is also not treated as a hard 0: unknown (25) ranks above a *genuine* 0 and below a good
  match, so "we don't know" and "known to be a poor fit" stay distinct. Genuine lows are returned
  as numbers (e.g. skill 0.0 with `missing_skills`), unknowns as `null` with a warning.
- With every confidence equal to 1 the formula is exactly the spec's weighted sum.
- No profile / no skills → skill unknown. No interests → interest unknown. Unlabelled issue or no
  difficulty target → difficulty unknown. No push date → activity unknown. Repo data older than 7
  days (`COMPASS_RECO_MAX_DATA_AGE_DAYS`) → activity `stale` and treated as unknown. Issue data older
  than 7 days → readiness `stale`/unknown.
- If `coverage < 0.4` (`COMPASS_RECO_MIN_COVERAGE`) the item gets `insufficient_information: true` and
  a leading warning. Items are still ordered by score alone (a sparse item with a perfect skill
  match must not sink below a known mismatch); the flag tells the client to trust it less.
- `ranking_reliability.status`: `insufficient` (profile missing or has no skills/interests),
  `no_candidates`, `limited` (some items insufficient, upstream errors, or truncation), `ok`.

## Request / response
Body (all optional): `repositories` (≤5 `owner/name`, synced from GitHub only if missing/stale),
`language`, `topic`, `preferred_difficulty` (1-5, this search only), `max_inactive_days`
(repos with no push date are excluded when set), `include_archived` (default false), `limit` (1-50,
default 20), `offset`. Unknown fields such as `user_id` are rejected with 422; the user is always
the bearer-token identity.

Response: `model` (name, `is_ml_prediction: false`, note), `weights`, `ranking_reliability`,
`total`, `items[]` (rank, repository, issue number/title/URL, `final_score`, `coverage`,
`insufficient_information`, per-component score/status/confidence/effective_score/evidence/warnings,
`reasons`, `missing_skills`, `warnings`), `upstream_errors[]`.

Ordering: score descending, ties by owner, repo name (case-insensitive), then issue number.

## Data and upstream behaviour
- Without `repositories`, **no GitHub calls are made**; only stored data is searched (at most the
  2000 most recently updated matching issues; `candidates_truncated` says if cut).
- With `repositories`, existing sync logic runs (skipped when data is fresh). A failure for one
  repository is reported in `upstream_errors` and stored (possibly stale) data is still used. If
  there is nothing to show and an upstream error occurred, the mapped error (404/429/502/504) is returned.
- Private repositories are never stored or scored.

## Known limitations
- Not an ML model and not validated; weights and bands are guesses.
- Keyword skill detection and lexical interest matching are crude (semantic matching: later stage).
- Difficulty, activity and readiness rest on thin signals (see table).
- Only the returned page is persisted (see EVALUATION_DATA.md); history grows with each distinct result and has no retention policy yet.
- `topic` and `max_inactive_days` filters run in Python after the SQL query.
- A requested repository's issue list is capped at 300 open issues per sync.


## heuristic_v2 (Stage 9) - experimental, not shown to be better than v1
Select with `"scorer": "heuristic_v2"` (default stays `heuristic_v1`). Stored results are tagged `heuristic_v2`;
`heuristic_v1` code and stored results are unchanged (a test pins the v1 source digest, another pins v2's).

| Component | v1 | v2 |
|---|---|---|
| Skill compatibility | 0.35 | 0.30 (same computation) |
| Interest similarity | 0.25 | 0.20 (same computation) |
| Difficulty suitability | 0.20 | 0.15 (same computation, label hint only) |
| Repository activity -> **repository health** | 0.15 (last push date only) | 0.20 (health report: commits, releases, flow, maintainer responsiveness, maintenance) |
| Contribution readiness | 0.05 (open, unassigned, description length >= 80, not archived) | 0.15 (readiness report: guide, detected tests, description quality, linked PRs, labels, availability, docs) |

**Why the weights moved (judgement, not a tuned result).** Health and readiness now rest on several measured signals
instead of one date and four weak flags, so they deserve more influence; the weight comes from difficulty (label-only
hint) and from skill/interest (keyword/lexical matching), which are the least reliable inputs. Nothing has validated
this split, and the numbers are not claimed to be better. They are stored with every result so v1 and v2 can be compared
on the same inputs once an evaluation dataset exists.

Mechanics: the component's score is the report score and its confidence is the report's evidence **coverage**, so
sparse reports shrink toward the fill value (25) exactly like every other component. Missing, stale (> 14 days) or
unanalysed intelligence is `unknown`: it never raises the score, and a ranking shows "Repository health has not been
analysed for N repositories" when relevant. v2 search makes **no** GitHub calls except for repositories listed in
`repositories`, whose health analysis is ensured. Per-issue linked-PR data is only present after
`GET /issues/{owner}/{repo}/{number}/readiness` has been called for that issue (otherwise that signal is unknown).
The stored match keeps the health/readiness reports it used in `data_snapshot.issue.intel`, and `component_scores`
for v2 rows use the v2 component names (`repository_health`, kept in the existing `activity_score` column).
