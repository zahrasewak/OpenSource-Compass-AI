# OpenSource Compass AI — Architecture (Phase 1)

Status legend: **[built]** implemented and tested, **[planned]** designed only.

## 1. Components

```
Chrome extension (MV3, React/TS) ──HTTPS──▶ FastAPI /api/v1
                                              │ auth, validation, rate limit
        ┌─────────────────────────────────────┼───────────────────────────┐
        ▼                                     ▼                           ▼
 GitHub service                        Intelligence services        Execution API
 (REST/GraphQL, ETag cache,            features → difficulty →      (validate, enqueue only)
  backoff, rate-limit aware)           ranking → explanation              │
        │                                     │                           ▼
        ▼                                     ▼                      Redis queue
 Redis (cache, queue, limits)          PostgreSQL (SQLAlchemy 2)          │
                                                                          ▼
                                                              Execution worker (separate
                                                              process/host) → SandboxRunner
                                                              interface → DockerRunner
                                                              (later: Firecracker/gVisor)
```

The API process never runs repository code. The worker holds no GitHub tokens or DB credentials beyond job status updates.

## 2. Stack decisions

| Area | Choice | Reason |
|---|---|---|
| API | FastAPI, Pydantic v2, pydantic-settings | typed validation, OpenAPI |
| DB | PostgreSQL, SQLAlchemy 2.x, Alembic | migrations are mandatory; JSONB only for flexible blobs |
| Queue | Celery + Redis | requested; worker isolation |
| Tests | pytest; SQLite for unit tests, Postgres in CI for integration | fast local loop, real-DB check in CI |
| ML (Phase 3) | scikit-learn baselines, XGBoost, SHAP, sentence-transformers, HF Transformers (CPU) | each compared against baselines |
| Extension (Phase 5) | MV3, TS, React, Vite, Vitest | |
| Sandbox (Phase 6) | Docker with hardening behind `SandboxRunner` | microVM swap-in later |

Torch/transformers are **optional extras** (`requirements-ml.txt`) so the Phase 2 backend installs fast and CPU-only.

## 3. Folder structure

```
backend/
  app/
    main.py              # app factory only
    core/                # config, logging, errors, security
    db/                  # base, session, models/
    api/v1/              # thin routers
    schemas/             # Pydantic I/O models
    services/            # business logic (github, profile, recommend, ...)
    ml/                  # inference only (Phase 3)
    execution/           # job API logic + SandboxRunner interface (Phase 6)
  alembic/ , alembic.ini
  tests/
ml-training/             # data collection, labeling, training, eval (separate from inference)
extension/               # MV3 extension (Phase 5)
docs/
docker-compose.yml, .env.example, .github/workflows/ci.yml
```

## 4. Database

Tables exactly as specified in the master prompt (users … execution_results). Additions and conventions:

- `users.github_user_id` unique; **no tokens stored** in tables (tokens live in server-side secret store / session, never in rows).
- Unique constraints: `repositories(owner,name)`, `issues(repository_id,issue_number)`, `saved_issues(user_id,issue_id)`, `user_skills(user_id,skill_name)`, `repository_snapshots(repository_id,commit_sha,analysis_version)`.
- Indexes on every FK plus `recommendations(user_id, final_score DESC)`, `execution_jobs(user_id, created_at DESC)`.
- `execution_jobs.status` is a constrained enum: QUEUED, PREPARING, RUNNING, COMPLETED, FAILED, TIMED_OUT, CANCELLED, UNSUPPORTED.
- `execution_results.sanitized_log_reference` points to object/file storage; logs are not stored inline. Retention: results purged after `EXECUTION_RETENTION_DAYS` (default 14) by a scheduled task [planned].
- Added `execution_events` (job_id, event_type, detail, created_at) for the audit trail — no secrets.
- Recommendations store `components` (JSONB) for uncertainty flags/penalties besides the five numeric scores.

## 5. API contracts

Base `/api/v1`, plus `/health`, `/ready`. Error envelope for every failure:

```json
{"error": {"code": "validation_error", "message": "...", "details": [...], "request_id": "..."}}
```

List endpoints use `?limit=&offset=` (max 100) returning `{items, total, limit, offset}`. Endpoints are the list in the master prompt §9; analysis/reproduce/execution endpoints return `202 {job_id}` and are polled via `GET /executions/{job_id}`. Auth: bearer token → user id; each resource query is scoped by `user_id` (cross-user access returns 404).

## 6. ML architecture (Phase 3)

| Component | Input | Output | Training / eval | Limits |
|---|---|---|---|---|
| Heuristic baseline | structured features | difficulty + `model_status="heuristic_baseline"` | none | not a trained model; always labeled so |
| XGBoost difficulty | structured features | 5-class probs, SHAP | human-labeled set, split by repository; macro-F1, per-class P/R, confusion matrix, calibration | needs labeled data; labels are noisy |
| Transformer issue classifier | title+body | 8 categories + probs | fine-tune small pretrained model (e.g. DistilBERT/MiniLM) | CPU inference only; fine-tuning optional |
| Sentence embeddings | issue/user text | cosine similarity | pretrained, not fine-tuned | in-process numpy index; vector DB later |
| Ranker | 5 component scores | final score | fixed weights now; LambdaMART only after trustworthy feedback; NDCG@K/P@K | clicks ≠ success |

Until a trained artifact exists in the model registry, the API returns `model_status: "untrained"` and the baseline. Missing inputs lower confidence and apply penalties — never default to 100.

## 7. Secure execution (Phase 6)

Request → validate URL (https, github.com only, DNS-resolve and reject private/loopback/link-local/metadata ranges) → resolve commit SHA via API → verify public → inspect manifests statically → match against an **allowlist of command templates** (e.g. `pytest -x -q` for Python repos with a detected test config; README commands are never run) → enqueue.

Worker per job: fresh container, `--read-only` rootfs, tmpfs workspace, non-root uid, `--cap-drop=ALL`, `no-new-privileges`, seccomp default, `--pids-limit`, memory/CPU/disk/time limits, `--network=none` by default (dependency install uses a separate pre-step with a pinned-index proxy allowlist, then network is dropped for test execution), no mounts, no env secrets, no Docker socket. Source is fetched by the worker **outside** the sandbox as a SHA-pinned tarball, extracted with path-traversal/symlink checks, then copied in. Logs are size-capped and scrubbed. Cancellation kills the container by label; a janitor reaps orphans.

Honest limits: Docker shares the host kernel; this is not adequate for hostile code at scale. Production should use gVisor/Kata/Firecracker behind the same `SandboxRunner` interface. **Docker is not installed on the current dev machine, so Phase 6 can only be unit-tested here, not run end-to-end.**

## 8. Threat model (summary)

| Threat | Mitigation |
|---|---|
| SSRF via repo URL | strict host allowlist, DNS re-check, block private ranges |
| Malicious repo escapes sandbox | hardening above; documented residual risk |
| Secret exfiltration | no secrets in worker env/sandbox; network off |
| Archive path traversal / symlinks | validated extraction |
| Cross-user data access | user-scoped queries + tests |
| Log injection / secret leakage | sanitize, truncate |
| Resource abuse | per-user rate limit, concurrency cap, quotas |
| Malicious GitHub page content in extension | treat as data, render via React escaping, validate messages |
| Token leakage | tokens never persisted in DB or logged |

## 9. Milestones

1. **Phase 2** backend MVP — stages: (a) skeleton, config, errors, health, DB models [this commit]; (b) Alembic + Postgres verification; (c) profile API + auth; (d) GitHub client with mocks; (e) repo/issue endpoints; (f) basic recommendation (heuristic, labeled) + saved issues + feedback.
2. Phase 3 ML. 3. Phase 4 repo intelligence. 4. Phase 5 extension. 5. Phase 6 Project Lab. 6. Phase 7 integration/evaluation.

I will pause for confirmation between phases.

## 10. Standing invariants
- **Missing profile = unknown, never a match.** `GET /profile` returns 404 when no profile exists.
  Recommendation code must treat a missing profile (or missing skills/preferences) as *unknown*:
  skill/interest components are reported as unavailable with reduced confidence and a penalty, and
  must never default to a high or perfect score.
