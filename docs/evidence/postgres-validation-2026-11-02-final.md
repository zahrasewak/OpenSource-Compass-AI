# PostgreSQL validation run (2026-11-02 01:31 UTC)

Server: `PostgreSQL 16.2 on x86_64-pc-mingw64, compiled by gcc.exe (x86_64-posix-seh-rev2, Built by MinGW-W64 project) 12.2.0, 64-bit`

Client: Python 3.12.6, psycopg 3.3.6

### alembic upgrade head
```
$ C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Scripts\python.exe -m alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, profile fields: skill category, contribution goals, experience level
INFO  [alembic.runtime.migration] Running upgrade 0002 -> 0003, repository/issue columns needed for GitHub synchronisation
INFO  [alembic.runtime.migration] Running upgrade 0003 -> 0004, persisted matches, feedback categories
INFO  [alembic.runtime.migration] Running upgrade 0004 -> 0005, repository intelligence: snapshot data column, one issue_features row per (issue, version)
INFO  [alembic.runtime.migration] Running upgrade 0005 -> 0006, auth sessions and pairing codes
INFO  [alembic.runtime.migration] Running upgrade 0006 -> 0007, auth sessions: persistent flag (client keeps the refresh token on disk)
INFO  [alembic.runtime.migration] Running upgrade 0007 -> 0008, auth throttle events (database-backed rate limiting shared by all workers)
[exit 0, 2.0s]
```

### alembic current
```
$ C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Scripts\python.exe -m alembic current
0008 (head)
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
[exit 0, 1.4s]
```

### alembic heads
```
$ C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Scripts\python.exe -m alembic heads
0008 (head)
[exit 0, 0.8s]
```

### alembic check
```
$ C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Scripts\python.exe -m alembic check
No new upgrade operations detected.
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.plugins] setting up autogenerate plugin alembic.autogenerate.schemas
INFO  [alembic.runtime.plugins] setting up autogenerate plugin alembic.autogenerate.tables
INFO  [alembic.runtime.plugins] setting up autogenerate plugin alembic.autogenerate.types
INFO  [alembic.runtime.plugins] setting up autogenerate plugin alembic.autogenerate.constraints
INFO  [alembic.runtime.plugins] setting up autogenerate plugin alembic.autogenerate.defaults
INFO  [alembic.runtime.plugins] setting up autogenerate plugin alembic.autogenerate.comments
INFO  [alembic.ddl.postgresql] Detected sequence named 'issues_id_seq' as owned by integer column 'issues(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'repositories_id_seq' as owned by integer column 'repositories(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'issue_predictions_id_seq' as owned by integer column 'issue_predictions(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'issue_features_id_seq' as owned by integer column 'issue_features(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'feedback_id_seq' as owned by integer column 'feedback(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'saved_issues_id_seq' as owned by integer column 'saved_issues(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'users_id_seq' as owned by integer column 'users(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'repository_snapshots_id_seq' as owned by integer column 'repository_snapshots(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'user_preferences_id_seq' as owned by integer column 'user_preferences(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'recommendations_id_seq' as owned by integer column 'recommendations(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'execution_events_id_seq' as owned by integer column 'execution_events(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'execution_jobs_id_seq' as owned by integer column 'execution_jobs(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'execution_results_id_seq' as owned by integer column 'execution_results(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'pairing_codes_id_seq' as owned by integer column 'pairing_codes(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'user_skills_id_seq' as owned by integer column 'user_skills(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'auth_throttle_events_id_seq' as owned by integer column 'auth_throttle_events(id)', assuming SERIAL and omitting
INFO  [alembic.ddl.postgresql] Detected sequence named 'auth_sessions_id_seq' as owned by integer column 'auth_sessions(id)', assuming SERIAL and omitting
[exit 0, 1.5s]
```

### schema checks
all passed

### alembic downgrade base
```
$ C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Scripts\python.exe -m alembic downgrade base
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running downgrade 0008 -> 0007, auth throttle events (database-backed rate limiting shared by all workers)
INFO  [alembic.runtime.migration] Running downgrade 0007 -> 0006, auth sessions: persistent flag (client keeps the refresh token on disk)
INFO  [alembic.runtime.migration] Running downgrade 0006 -> 0005, auth sessions and pairing codes
INFO  [alembic.runtime.migration] Running downgrade 0005 -> 0004, repository intelligence: snapshot data column, one issue_features row per (issue, version)
INFO  [alembic.runtime.migration] Running downgrade 0004 -> 0003, persisted matches, feedback categories
INFO  [alembic.runtime.migration] Running downgrade 0003 -> 0002, repository/issue columns needed for GitHub synchronisation
INFO  [alembic.runtime.migration] Running downgrade 0002 -> 0001, profile fields: skill category, contribution goals, experience level
INFO  [alembic.runtime.migration] Running downgrade 0001 -> , initial schema
[exit 0, 1.5s]
```

### alembic upgrade head (again)
```
$ C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Scripts\python.exe -m alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, profile fields: skill category, contribution goals, experience level
INFO  [alembic.runtime.migration] Running upgrade 0002 -> 0003, repository/issue columns needed for GitHub synchronisation
INFO  [alembic.runtime.migration] Running upgrade 0003 -> 0004, persisted matches, feedback categories
INFO  [alembic.runtime.migration] Running upgrade 0004 -> 0005, repository intelligence: snapshot data column, one issue_features row per (issue, version)
INFO  [alembic.runtime.migration] Running upgrade 0005 -> 0006, auth sessions and pairing codes
INFO  [alembic.runtime.migration] Running upgrade 0006 -> 0007, auth sessions: persistent flag (client keeps the refresh token on disk)
INFO  [alembic.runtime.migration] Running upgrade 0007 -> 0008, auth throttle events (database-backed rate limiting shared by all workers)
[exit 0, 1.6s]
```

### integration tests
```
$ C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_postgres_integration.py
.....                                                                    [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1
  C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
5 passed, 1 warning in 1.26s
[exit 0, 3.8s]
```

### whole test suite on PostgreSQL
```
$ C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
........................................................................ [ 10%]
.........s.s............................................................ [ 20%]
........................................................................ [ 30%]
........................................................................ [ 41%]
........................................................................ [ 51%]
........................................................................ [ 61%]
........................................................................ [ 72%]
........................................................................ [ 82%]
........................................................................ [ 92%]
.................................................                        [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1
  C:\Users\ZAHRA\OneDrive\Desktop\AI Open Source Contribution Matcher\backend\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
695 passed, 2 skipped, 1 warning in 243.73s (0:04:03)
[exit 0, 248.3s]
```

## Summary
- PASS 1. alembic upgrade head 
- PASS 2. alembic current is head INFO  [alembic.runtime.migration] Will assume transactional DDL.
- PASS 3. alembic check: models == migrations 
- PASS 4. schema checks (JSONB, timestamptz, defaults, constraints, indexes, FK rules) 
- PASS 5. alembic downgrade base leaves only alembic_version ['alembic_version']
- PASS 6. alembic upgrade head again 
- PASS 7. tests/test_postgres_integration.py 5 passed, 1 warning in 1.26s
- PASS 8. whole test suite on PostgreSQL 695 passed, 2 skipped, 1 warning in 243.73s (0:04:03)
