# API reference (implemented endpoints)

Base path `/api/v1`. All endpoints except `/health`, `/ready`, `/auth/exchange`, `/auth/refresh` and `/auth/logout` need
`Authorization: Bearer <access token>`. Tokens come from a session: see [AUTHENTICATION.md](AUTHENTICATION.md).
Errors always use `{"error": {"code", "message", "details", "request_id"}}`. Interactive docs: `/docs`.

## Repositories and issues (Stage 6)
Public GitHub data only. Data is shared between users (it contains nothing user-specific) and
never includes profile- or score-derived fields.

| Method & path | Purpose |
|---|---|
| `GET /repositories/{owner}/{repo}` | Repository metadata. `?refresh=true` asks for a re-sync. |
| `GET /repositories/{owner}/{repo}/issues` | Paginated issues (PRs excluded). Query: `state=open\|closed\|all` (default `open`), `limit` 1-100 (20), `offset` >= 0, `refresh`. |
| `GET /issues/{owner}/{repo}/{number}` | One issue. `?refresh=true` as above. |

Not yet implemented from the spec: `.../health`, `.../dna`, `POST .../analyze|explain|reproduce`.

### Sync behaviour
- Stored data younger than `COMPASS_GITHUB_SYNC_TTL_SECONDS` (900) is served without calling GitHub.
  `refresh=true` is ignored for data younger than `COMPASS_GITHUB_MIN_REFRESH_SECONDS` (30) so a
  user cannot burn the server's GitHub quota.
- Listing issues syncs **open** issues only, up to `COMPASS_GITHUB_ISSUE_SYNC_MAX_PAGES` (3) x 100.
  The response has `sync_truncated: true` if more existed. `state=closed|all` returns only what is
  already stored (e.g. issues previously fetched individually).
- Sync is an idempotent upsert keyed on `(owner,name)` (case-insensitive lookup) and
  `(repository_id, issue_number)`; a lost insert race is retried as an update. If GitHub fails,
  nothing is written.
- Known gap: an issue that closes on GitHub keeps its stored `open` state until it is fetched
  individually after the TTL; use `fetched_at` to judge freshness. A renamed repository creates a
  new row (no GitHub numeric id is stored yet).
- Pull requests are never stored; a PR number on the issue endpoint returns 404 `not_an_issue`.

### Privacy
A repository is accepted only if GitHub explicitly says `private: false`. Private or
unknown-visibility repositories return 404 `repository_not_found` and are never stored. Responses
are explicit allow-lists; raw upstream payloads, URLs and headers are never forwarded.

### Error mapping
| Situation | Status | `error.code` |
|---|---|---|
| Invalid owner/repo/number or query | 422 | `validation_error` |
| Repository missing or non-public | 404 | `repository_not_found` |
| Issue missing / is a PR | 404 | `issue_not_found` / `not_an_issue` |
| GitHub rate limit (wait too long to block) | 429 + `Retry-After` | `github_rate_limited` |
| GitHub denies access | 403 | `github_access_denied` |
| Server's GitHub credential invalid | 502 | `github_auth_failed` |
| GitHub 5xx / network failure | 502 | `github_unavailable` |
| GitHub timeout | 504 | `github_timeout` |
| Malformed GitHub response | 502 | `github_bad_response` |
| Other GitHub 4xx | 502 | `github_request_rejected` |

Operational note: the shared GitHub client blocks a worker thread while waiting out short rate
limits. For API processes set `COMPASS_GITHUB_MAX_RATE_LIMIT_WAIT_SECONDS` low (e.g. 5) so callers
get a fast 429 instead of a long wait.

## Matches (Stage 7)
`POST /matches/search` — heuristic-baseline ranking of stored open issues for the caller. Full
description, scoring formula and missing-data rules: [RECOMMENDATIONS.md](RECOMMENDATIONS.md).

### Persisted matches, saved issues, feedback (Stage 8)
All scoped to the authenticated user; another user's ids return 404. Data model, immutability rules
and bias notes: [EVALUATION_DATA.md](EVALUATION_DATA.md).

| Method & path | Purpose |
|---|---|
| `POST /matches/search` | now also stores the returned page unless `"persist": false`; items carry `match_id`, `generated_at` |
| `GET /matches` | stored matches, newest first. Query: `limit`, `offset`, `model` (e.g. `heuristic_v1`), `repository` (`owner/name`), `min_score`, `include_insufficient` |
| `GET /matches/{id}` | one stored match with its full historical breakdown, config and data snapshot (never recomputed) |
| `POST /saved-issues` | body `{owner, repo, number, match_id?}`. 201 new, 200 `created:false` if already saved. Stored issues are saved without calling GitHub; unknown ones are fetched (404 if gone). Also records a system `saved` event |
| `GET /saved-issues` | `limit`, `offset`; newest first |
| `GET /saved-issues/lookup/{owner}/{repo}/{number}` | (Stage 13) `{saved, saved_issue?}` for the caller only. Database only: never calls GitHub; an issue not stored yet is simply `saved:false` |
| `DELETE /saved-issues/{id}` | `{id}` is the saved-entry id from the list. 204; records an `unsaved` event |
| `POST /feedback` | body `{type, value?, match_id? | owner+repo+number}`. Types: `relevance`, `difficulty`, `interest`, `attempted`, `contributed`. 201 stored, 200 `duplicate:true` if it repeats your latest answer. `attempted`/`contributed` are self-reported and unverified |

Not implemented: `GET /feedback/summary`, any aggregation of feedback.

### Repository intelligence (Stage 9)
Details, formulas and limits: [INTELLIGENCE.md](INTELLIGENCE.md). Heuristic reports, not ML predictions.

| Method & path | Purpose |
|---|---|
| `GET /repositories/{owner}/{repo}/health` | Repository health report (activity, maintainer responsiveness, maintenance), per-signal evidence/status/confidence, raw observations and data freshness. Collects from GitHub only if no fresh stored analysis exists (about 10-16 requests); `?refresh=true` is throttled |
| `GET /issues/{owner}/{repo}/{number}/readiness` | Issue readiness report: contribution guide, detected tests (not executed), description quality, linked PRs (open/merged/closed-unmerged), labels, availability, other docs. Returns what it can with `upstream_errors` if GitHub fails |
| `POST /matches/search` with `"scorer": "heuristic_v2"` | Ranks with repository health + readiness (experimental; default scorer stays `heuristic_v1`). Listing a repository in `repositories` also ensures its health analysis |

### Difficulty estimate (Stage 10, optional ML)
`POST /issues/{owner}/{repo}/{number}/difficulty` returns the maintainer-label hint (deterministic, noisy) and, if
`COMPASS_ML_DIFFICULTY_MODEL_DIR` points at a usable artifact, an ML estimate reported separately (`class_scores` are
uncalibrated, not probabilities). Without a model it still returns 200 with `ml_prediction.status = "unavailable"`, a
reason code, and the hint as fallback. Details and caveats: [ML.md](ML.md).

### Single-issue match (Stage 12, used by the browser extension)
`POST /issues/{owner}/{repo}/{number}/match` body `{scorer?, persist?}` returns `{model, weights, ranking_reliability, match}` for ONE
issue (same scoring and explanation as `/matches/search`, no rank). `persist` (default true) stores it idempotently and returns a
`match_id` for feedback. Heuristic score, not an ML prediction. See [EXTENSION.md](EXTENSION.md).

## Authentication (Stage 13; replaces the Stage 3 manual token)
| Method & path | Purpose |
|---|---|
| `POST /auth/exchange` | body `{code, client_label?, persistent?}` (`code` 1-200 chars; `persistent: true` = the client keeps the refresh token on disk, so the session gets the tighter persistent limits). Spends a one-time pairing code and returns `{access_token, refresh_token, token_type:"Bearer", expires_in, refresh_expires_in, session_expires_at}` (`Cache-Control: no-store`). Unknown/expired/used/too-short codes: identical 401. **Rate limited per client address** (see below) |
| `POST /auth/refresh` | body `{refresh_token}`. Rotates the refresh token; returns a new pair. A previously rotated token revokes the whole session. Unknown/expired/revoked/reused: identical 401 |
| `POST /auth/logout` | 204. Body `{refresh_token}` or a bearer token; idempotent, reveals nothing about whether a session existed |
| `POST /auth/logout-all` | 204. Ends every session of the caller |
| `GET /auth/me` | The caller (`id`, `github_user_id`, `username`) |

**Rate limiting (Stage 15):** `/auth/exchange`, `/auth/refresh` and `/auth/logout` answer `429 rate_limited` with `Retry-After` and
`details.retry_after_seconds` when a client address exceeds 5 failed attempts per 15 minutes (a lockout that also refuses *valid* codes without
consuming them), 20 requests per minute, or the global ceiling. In-memory and per process: see [AUTHENTICATION.md](AUTHENTICATION.md) section 7.

Access tokens last 15 minutes (`COMPASS_AUTH_ACCESS_TTL_SECONDS`) and are bound to a revocable session; refresh tokens 7 days from
last use, sessions at most 30 days (**persistent sessions: 3 days / 14 days**). Pairing codes are created by an administrator only: `python -m app.cli issue-pairing-code
--github-id <id> --username <name>`; `python -m app.cli revoke-sessions --github-id <id>` ends a user's sessions. Legacy
`issue-token` tokens are rejected when `COMPASS_ENVIRONMENT=production`.

## Profile (Stage 4)
`POST|GET|PUT /profile`. A missing profile is a 404 and must be treated as
*unknown* by future recommendation code, never as a match.
