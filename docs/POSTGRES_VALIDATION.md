# Validating the Alembic migrations against a real PostgreSQL

**Status (Stage 14, 2026-11-02): VALIDATED LOCALLY on PostgreSQL 16.2; NOT YET on the official `postgres:16` image / Linux (CI).**
All steps below were executed against a real PostgreSQL 16.2 server (an embedded Windows build, see "No Docker?" below):
migrations `0001`-`0006` upgrade, `alembic check` (no drift), downgrade to base, re-upgrade; schema inspection (all JSON columns are
`jsonb`, defaults, 36 named unique/check/foreign-key constraints, indexes, `timestamptz` everywhere, FK delete rules); the 5 integration
tests; and the whole backend suite (617 passed). Raw output: [evidence/postgres-validation-2026-11-02.md](evidence/postgres-validation-2026-11-02.md).
The CI job `migrations-postgres` (official image) is written but **has not run** (the project is not on GitHub), so validation on that
environment is still pending. Do not generalise the local result to other PostgreSQL versions or production configuration.

## One command (Stage 15): the same steps as the CI job
`backend/scripts/validate_postgres.py` creates two scratch databases on a server you give it and runs: upgrade, `current` == head,
`alembic check`, schema checks (JSONB, `timestamptz`, defaults, constraints, indexes, FK delete rules), downgrade to base, re-upgrade, the
integration tests, and the whole test suite on PostgreSQL; it prints PASS/FAIL per step and the server's `select version()`, and exits non-zero
on any failure. It **drops and recreates** the databases `compass_validate` and `compass_validate_suite`.
```
cd backend
python scripts/validate_postgres.py postgresql://USER:PASSWORD@HOST:PORT --log ../docs/evidence/postgres-run.md
```
The CI job `migrations-postgres` runs exactly this against the official `postgres:16` service image and uploads the log as an artifact.
**Final run (2026-11-02, same local PostgreSQL 16.2 embedded Windows build, migrations through 0008): 8/8 steps passed, whole suite 695 passed, 2 skipped** (raw log: [evidence/postgres-validation-2026-11-02-final.md](evidence/postgres-validation-2026-11-02-final.md)).

Earlier **Stage 15 run (same environment, before migration 0008): 8/8 steps passed, whole suite 669 passed**; raw log:
[evidence/postgres-validation-stage15.md](evidence/postgres-validation-stage15.md). The first attempt of that day failed 2 tests
(1 integration test, counted twice) because my test hard-coded migration revision `0006` while the head had become `0007`: a stale test, not
a database problem; the test now reads the head from the migration scripts. That is the kind of mistake only running against the real thing finds.

## No Docker? Real PostgreSQL without installing anything system-wide
This is how the Stage 14 run was done on a Windows machine with no Docker and no PostgreSQL. It uses the PyPI package `pgserver`,
which bundles a real PostgreSQL server (build provenance: a third-party MinGW build, *not* EDB/Docker/distribution packages; use
an official build for anything beyond a smoke test). In a throwaway virtualenv outside the project:
```
python -m venv %TEMP%\pgvenv
%TEMP%\pgvenv\Scripts\python -m pip install pgserver==0.1.4
%TEMP%\pgvenv\Scripts\python -c "import pgserver; s = pgserver.get_server(r'%TEMP%\pgdata', cleanup_mode=None); print(s.get_uri())"
```
It prints a URI such as `postgresql://postgres:@127.0.0.1:55484/postgres` (the port varies). Create scratch databases with psycopg
(`create database compass_test`, `create database compass_suite`), then use `postgresql+psycopg://postgres:@127.0.0.1:<port>/<db>` below.
Stop it with `get_server(...).cleanup()` or by ending the `postgres` processes; delete the data directory afterwards.

## 1. Start PostgreSQL 16
Either Docker:
```
docker run --name compass-pg -e POSTGRES_USER=compass -e POSTGRES_PASSWORD=compass -e POSTGRES_DB=compass -p 5432:5432 -d postgres:16
docker exec compass-pg pg_isready -U compass
```
or a native PostgreSQL 16 install with an empty database `compass` owned by a user `compass`.
The CI job `migrations-postgres` in `.github/workflows/ci.yml` performs steps 3-6 automatically on every push once
the repository is on GitHub; its green run is equivalent evidence.

## 2. Configure the backend
From `backend/` with the virtual environment active:
```
set COMPASS_DATABASE_URL=postgresql+psycopg://compass:compass@localhost:5432/compass      (Windows cmd)
$env:COMPASS_DATABASE_URL = "postgresql+psycopg://compass:compass@localhost:5432/compass"  (PowerShell)
export COMPASS_DATABASE_URL=postgresql+psycopg://compass:compass@localhost:5432/compass    (bash)
```

## 3. Upgrade, check, round-trip
```
alembic upgrade head          # must finish with no error
alembic current               # must print 0006 (head)   [run 2026-11-02: OK]
alembic check                 # must print "No new upgrade operations detected." (models == migrations)
alembic downgrade base        # must finish with no error and leave only alembic_version
alembic upgrade head          # must succeed again
```
A failure in `upgrade` or `downgrade` (for example a constraint or batch-mode statement PostgreSQL rejects) is the
kind of problem SQLite cannot reveal; record the exact error and fix the migration.

## 4. Inspect the schema
```
psql -h localhost -U compass -d compass
```
Check at least:
```
-- JSON columns must be JSONB
select table_name, column_name, data_type from information_schema.columns
 where table_schema='public' and data_type in ('json','jsonb') order by 1,2;
-- expected: every flexible column listed as jsonb, none as json

-- unique constraints added in later stages
select conname from pg_constraint where conname in (
  'uq_recommendations_user_issue_model_input','uq_issue_features_issue_version');
-- expected: 2 rows

-- defaults
select column_name, column_default from information_schema.columns
 where table_name='repository_snapshots' and column_name in ('data','collected_at');
-- expected: data default '{}'::jsonb ; collected_at default now()

-- foreign key delete rules
select conname, confdeltype from pg_constraint where conname like 'fk_feedback_recommendation%';
-- expected: confdeltype = 'n' (SET NULL)

-- status check constraint
select conname from pg_constraint where conname = 'ck_execution_jobs_status_valid';

-- migration 0006 (sessions): tables, unique hashes, timestamp defaults
select table_name from information_schema.tables where table_name in ('auth_sessions','pairing_codes');   -- expected: 2 rows
select indexname from pg_indexes where tablename in ('auth_sessions','pairing_codes') order by 1;
-- expected: unique indexes/constraints on auth_sessions.sid, auth_sessions.refresh_hash, pairing_codes.code_hash,
--           and indexes on auth_sessions.user_id, auth_sessions.prev_refresh_hash, pairing_codes.user_id
select column_name, column_default from information_schema.columns
 where table_name = 'auth_sessions' and column_name = 'created_at';          -- expected: default now()
```

## 5. Behavioural checks the SQLite tests cannot make
Run these against the migrated database (a scratch database is fine):
1. Start the API (`uvicorn app.main:app`) with the same `COMPASS_DATABASE_URL`, set `COMPASS_AUTH_SECRET`, and call
   `GET /ready`: expect `{"status":"ready","checks":{"database":"up"}}`.
2. Sign in with a pairing code (`python -m app.cli issue-pairing-code --github-id 1 --username me`, then
   `POST /api/v1/auth/exchange {"code": "..."}`), create a profile with the returned access token, then insert a
   repository/issue by hand or via a GitHub-enabled run, and run `POST /api/v1/matches/search` twice. The second
   call must return the same `match_id`s (the unique `(user_id, issue_id, ranking_model_version, input_hash)` constraint
   is what makes retries idempotent).
3. Two concurrent identical searches (for example two terminals at once) must not produce duplicate rows or a 500:
   `select count(*), count(distinct input_hash) from recommendations;` should show equal counts per issue/user/model.
4. JSON containment queries are not used by the code today, so no JSONB operator behaviour is relied on.
5. **Sessions (migration 0006), automated:** the integration test races real threads against the conditional UPDATEs that
   make a pairing code and a refresh token single-use. It needs a scratch database and is skipped otherwise:
   ```
   export COMPASS_TEST_DATABASE_URL=postgresql+psycopg://compass:compass@localhost:5432/compass_test    (bash)
   $env:COMPASS_TEST_DATABASE_URL = "postgresql+psycopg://compass:compass@localhost:5432/compass_test"  (PowerShell)
   pytest tests/test_postgres_integration.py -v
   ```
   Expect 4 passed (schema at head and drift-free; all JSON columns are `jsonb`; exactly one of 8 concurrent exchanges of one
   code succeeds; exactly one of 8 concurrent refreshes of one refresh token succeeds and the session is then revoked). The
   file was only dry-run against SQLite (3 passed, JSONB check skipped); **it has never run on PostgreSQL**.
6. **Run the whole backend suite against PostgreSQL** (Stage 14). The shared `db_session` fixture switches to PostgreSQL when
   `COMPASS_TEST_SUITE_DATABASE_URL` is set. **Destructive:** it drops and recreates every table around each test, so use a scratch
   database that holds nothing you want:
   ```
   export COMPASS_TEST_SUITE_DATABASE_URL=postgresql+psycopg://compass:compass@localhost:5432/compass_suite    (bash)
   $env:COMPASS_TEST_SUITE_DATABASE_URL = "postgresql+psycopg://compass:compass@localhost:5432/compass_suite"  (PowerShell)
   pytest -q
   ```
   Run 2026-11-02: 617 passed (325 of them touch the database through this fixture; `tests/test_caching.py` also uses it for its
   multi-connection concurrency tests). Tests that create their own SQLite files (CLI, migrations) and pure-logic/ML tests are unaffected.

Prerequisites summary: PostgreSQL 16, `psycopg` (already in `requirements`), an empty scratch database, a user that can create
tables, network access to `localhost:5432`, and the backend virtualenv.

## 6. Record the result
Record the date, PostgreSQL version (`select version();`), commands and results in `docs/VALIDATION_STATUS.md` and, for raw output,
in `docs/evidence/`. Only the environment actually used may be called validated.

## Known PostgreSQL-specific risks to watch for
- `batch_alter_table` is used for SQLite compatibility; on PostgreSQL it emits plain `ALTER TABLE`. Constraint and
  index names are explicit; any name over 63 characters would be truncated by PostgreSQL (the longest current one,
  `uq_recommendations_user_issue_model_input`, is within the limit).
- Server defaults for JSONB columns are written as `'{}'` / `'[]'` strings; confirm PostgreSQL stores them as jsonb.
- SQLite reuses deleted row ids, PostgreSQL sequences do not; tests avoid depending on either behaviour.
