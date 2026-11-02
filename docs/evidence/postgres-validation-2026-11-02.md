# PostgreSQL validation run - raw output (2026-11-02, local machine)

Server: PostgreSQL 16.2 on x86_64-pc-mingw64 (gcc 12.2.0, 64-bit), embedded build from the PyPI package pgserver 0.1.4, listening on 127.0.0.1:55484.
Client: Python 3.12.6, SQLAlchemy 2.1.6, Alembic 1.20.1, psycopg 3.3.6, Windows 11 (10.0.26200).
Database: empty scratch database compass_test (migrations) and compass_suite (whole test suite; dropped/recreated per test).

## Migrations: upgrade / current / check / downgrade / upgrade / current / check
```

$ ./.venv/Scripts/python -m alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, profile fields: skill category, contribution goals, experience level
INFO  [alembic.runtime.migration] Running upgrade 0002 -> 0003, repository/issue columns needed for GitHub synchronisation
INFO  [alembic.runtime.migration] Running upgrade 0003 -> 0004, persisted matches, feedback categories
INFO  [alembic.runtime.migration] Running upgrade 0004 -> 0005, repository intelligence: snapshot data column, one issue_features row per (issue, version)
INFO  [alembic.runtime.migration] Running upgrade 0005 -> 0006, auth sessions and pairing codes
[exit 0]

$ ./.venv/Scripts/python -m alembic current
0006 (head)
[exit 0]

$ ./.venv/Scripts/python -m alembic check
No new upgrade operations detected.
[exit 0]

$ ./.venv/Scripts/python -m alembic downgrade base
INFO  [alembic.runtime.migration] Running downgrade 0006 -> 0005, auth sessions and pairing codes
INFO  [alembic.runtime.migration] Running downgrade 0005 -> 0004, repository intelligence: snapshot data column, one issue_features row per (issue, version)
INFO  [alembic.runtime.migration] Running downgrade 0004 -> 0003, persisted matches, feedback categories
INFO  [alembic.runtime.migration] Running downgrade 0003 -> 0002, repository/issue columns needed for GitHub synchronisation
INFO  [alembic.runtime.migration] Running downgrade 0002 -> 0001, profile fields: skill category, contribution goals, experience level
INFO  [alembic.runtime.migration] Running downgrade 0001 -> , initial schema
[exit 0]

$ ./.venv/Scripts/python -c 
import psycopg
c = psycopg.connect('postgresql://postgres:@127.0.0.1:55484/compass_test')
print('tables after downgrade:', [r[0] for r in c.execute("select table_name from information_schema.tables where table_schema='public' order by 1")])
tables after downgrade: ['alembic_version']
[exit 0]

$ ./.venv/Scripts/python -m alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, profile fields: skill category, contribution goals, experience level
INFO  [alembic.runtime.migration] Running upgrade 0002 -> 0003, repository/issue columns needed for GitHub synchronisation
INFO  [alembic.runtime.migration] Running upgrade 0003 -> 0004, persisted matches, feedback categories
INFO  [alembic.runtime.migration] Running upgrade 0004 -> 0005, repository intelligence: snapshot data column, one issue_features row per (issue, version)
INFO  [alembic.runtime.migration] Running upgrade 0005 -> 0006, auth sessions and pairing codes
[exit 0]

$ ./.venv/Scripts/python -m alembic current
0006 (head)
[exit 0]

$ ./.venv/Scripts/python -m alembic check
No new upgrade operations detected.
[exit 0]
```

## Schema inspection (information_schema / pg_constraint / pg_indexes)
```

-- json/jsonb columns
   ('execution_results', 'test_summary', 'jsonb', None)
   ('issue_features', 'extracted_skills', 'jsonb', None)
   ('issue_features', 'structured_features', 'jsonb', None)
   ('issue_predictions', 'class_probabilities', 'jsonb', None)
   ('issue_predictions', 'explanation', 'jsonb', None)
   ('issues', 'assignees', 'jsonb', "'[]'::jsonb")
   ('issues', 'labels', 'jsonb', None)
   ('recommendations', 'components', 'jsonb', None)
   ('recommendations', 'data_snapshot', 'jsonb', "'{}'::jsonb")
   ('repositories', 'topics', 'jsonb', "'[]'::jsonb")
   ('repository_snapshots', 'data', 'jsonb', "'{}'::jsonb")
   ('user_preferences', 'contribution_goals', 'jsonb', "'[]'::jsonb")
   ('user_preferences', 'interests', 'jsonb', None)
   ('user_preferences', 'preferred_task_types', 'jsonb', None)

-- named constraints (unique/check/fk) by table
   ('auth_sessions', 'fk_auth_sessions_user_id_users', 'f', 'c')
   ('auth_sessions', 'uq_auth_sessions_refresh_hash', 'u', ' ')
   ('auth_sessions', 'uq_auth_sessions_sid', 'u', ' ')
   ('execution_events', 'fk_execution_events_execution_job_id_execution_jobs', 'f', 'c')
   ('execution_jobs', 'ck_execution_jobs_status_valid', 'c', ' ')
   ('execution_jobs', 'fk_execution_jobs_repository_id_repositories', 'f', 'c')
   ('execution_jobs', 'fk_execution_jobs_user_id_users', 'f', 'c')
   ('execution_results', 'fk_execution_results_execution_job_id_execution_jobs', 'f', 'c')
   ('execution_results', 'uq_execution_results_execution_job_id', 'u', ' ')
   ('feedback', 'fk_feedback_issue_id_issues', 'f', 'c')
   ('feedback', 'fk_feedback_recommendation_id_recommendations', 'f', 'n')
   ('feedback', 'fk_feedback_user_id_users', 'f', 'c')
   ('issue_features', 'fk_issue_features_issue_id_issues', 'f', 'c')
   ('issue_features', 'uq_issue_features_issue_version', 'u', ' ')
   ('issue_predictions', 'fk_issue_predictions_issue_id_issues', 'f', 'c')
   ('issues', 'fk_issues_repository_id_repositories', 'f', 'c')
   ('issues', 'uq_issues_repository_id', 'u', ' ')
   ('pairing_codes', 'fk_pairing_codes_user_id_users', 'f', 'c')
   ('pairing_codes', 'uq_pairing_codes_code_hash', 'u', ' ')
   ('recommendations', 'fk_recommendations_issue_id_issues', 'f', 'c')
   ('recommendations', 'fk_recommendations_user_id_users', 'f', 'c')
   ('recommendations', 'uq_recommendations_user_issue_model_input', 'u', ' ')
   ('repositories', 'uq_repositories_owner', 'u', ' ')
   ('repository_snapshots', 'fk_repository_snapshots_repository_id_repositories', 'f', 'c')
   ('repository_snapshots', 'uq_repository_snapshots_repository_id', 'u', ' ')
   ('saved_issues', 'fk_saved_issues_issue_id_issues', 'f', 'c')
   ('saved_issues', 'fk_saved_issues_user_id_users', 'f', 'c')
   ('saved_issues', 'uq_saved_issues_user_id', 'u', ' ')
   ('user_preferences', 'ck_user_preferences_difficulty_range', 'c', ' ')
   ('user_preferences', 'fk_user_preferences_user_id_users', 'f', 'c')
   ('user_preferences', 'uq_user_preferences_user_id', 'u', ' ')
   ('user_skills', 'ck_user_skills_proficiency_range', 'c', ' ')
   ('user_skills', 'fk_user_skills_user_id_users', 'f', 'c')
   ('user_skills', 'uq_user_skills_user_id', 'u', ' ')
   ('users', 'uq_users_github_user_id', 'u', ' ')

-- indexes
   ('alembic_version', 'alembic_version_pkc')
   ('auth_sessions', 'ix_auth_sessions_prev_refresh_hash')
   ('auth_sessions', 'ix_auth_sessions_user_id')
   ('auth_sessions', 'pk_auth_sessions')
   ('auth_sessions', 'uq_auth_sessions_refresh_hash')
   ('auth_sessions', 'uq_auth_sessions_sid')
   ('execution_events', 'ix_execution_events_execution_job_id')
   ('execution_events', 'pk_execution_events')
   ('execution_jobs', 'ix_execution_jobs_repository_id')
   ('execution_jobs', 'ix_execution_jobs_user_created')
   ('execution_jobs', 'pk_execution_jobs')
   ('execution_results', 'pk_execution_results')
   ('execution_results', 'uq_execution_results_execution_job_id')
   ('feedback', 'ix_feedback_issue_id')
   ('feedback', 'ix_feedback_recommendation_id')
   ('feedback', 'ix_feedback_user_id')
   ('feedback', 'ix_feedback_user_issue_type')
   ('feedback', 'pk_feedback')
   ('issue_features', 'ix_issue_features_issue_id')
   ('issue_features', 'pk_issue_features')
   ('issue_features', 'uq_issue_features_issue_version')
   ('issue_predictions', 'ix_issue_predictions_issue_id')
   ('issue_predictions', 'pk_issue_predictions')
   ('issues', 'ix_issues_repository_id')
   ('issues', 'pk_issues')
   ('issues', 'uq_issues_repository_id')
   ('pairing_codes', 'ix_pairing_codes_user_id')
   ('pairing_codes', 'pk_pairing_codes')
   ('pairing_codes', 'uq_pairing_codes_code_hash')
   ('recommendations', 'ix_recommendations_issue_id')
   ('recommendations', 'ix_recommendations_user_created')
   ('recommendations', 'ix_recommendations_user_id')
   ('recommendations', 'ix_recommendations_user_score')
   ('recommendations', 'pk_recommendations')
   ('recommendations', 'uq_recommendations_user_issue_model_input')
   ('repositories', 'pk_repositories')
   ('repositories', 'uq_repositories_owner')
   ('repository_snapshots', 'ix_repository_snapshots_repository_id')
   ('repository_snapshots', 'pk_repository_snapshots')
   ('repository_snapshots', 'uq_repository_snapshots_repository_id')
   ('saved_issues', 'ix_saved_issues_issue_id')
   ('saved_issues', 'ix_saved_issues_user_id')
   ('saved_issues', 'pk_saved_issues')
   ('saved_issues', 'uq_saved_issues_user_id')
   ('user_preferences', 'pk_user_preferences')
   ('user_preferences', 'uq_user_preferences_user_id')
   ('user_skills', 'ix_user_skills_user_id')
   ('user_skills', 'pk_user_skills')
   ('user_skills', 'uq_user_skills_user_id')
   ('users', 'pk_users')
   ('users', 'uq_users_github_user_id')

-- timestamp defaults
   ('auth_sessions', 'created_at', 'timestamp with time zone', 'now()')
   ('execution_events', 'created_at', 'timestamp with time zone', 'now()')
   ('execution_jobs', 'created_at', 'timestamp with time zone', 'now()')
   ('feedback', 'created_at', 'timestamp with time zone', 'now()')
   ('issue_features', 'created_at', 'timestamp with time zone', 'now()')
   ('issue_predictions', 'created_at', 'timestamp with time zone', 'now()')
   ('issues', 'fetched_at', 'timestamp with time zone', 'now()')
   ('pairing_codes', 'created_at', 'timestamp with time zone', 'now()')
   ('recommendations', 'created_at', 'timestamp with time zone', 'now()')
   ('repository_snapshots', 'collected_at', 'timestamp with time zone', 'now()')
   ('saved_issues', 'created_at', 'timestamp with time zone', 'now()')
   ('user_preferences', 'updated_at', 'timestamp with time zone', 'now()')
   ('user_skills', 'created_at', 'timestamp with time zone', 'now()')
   ('users', 'created_at', 'timestamp with time zone', 'now()')
   ('users', 'updated_at', 'timestamp with time zone', 'now()')

-- non-timestamptz timestamps

-- version
   ('PostgreSQL 16.2 on x86_64-pc-mingw64, compiled by gcc.exe (x86_64-posix-seh-rev2, Built by MinGW-W64 project) 12.2.0, 64-bit',)
```

## Tests against PostgreSQL
Commands (from `backend/`, PowerShell/bash variables as in POSTGRES_VALIDATION.md):
```
COMPASS_TEST_DATABASE_URL=postgresql+psycopg://postgres:@127.0.0.1:55484/compass_test  pytest tests/test_postgres_integration.py -v
    -> 5 passed (schema at head 0006 and drift-free; all JSON columns jsonb; one of 8 concurrent exchanges of a pairing code succeeds;
       one of 8 concurrent refreshes of a refresh token succeeds and the session is then revoked;
       8 concurrent identical match requests -> same match_id for all, exactly 1 recommendations row, no 500)

COMPASS_TEST_SUITE_DATABASE_URL=postgresql+psycopg://postgres:@127.0.0.1:55484/compass_suite \
COMPASS_TEST_DATABASE_URL=postgresql+psycopg://postgres:@127.0.0.1:55484/compass_test  pytest -q
    -> 617 passed, 1 warning in 336 s (0 failed, 0 skipped), ML packages installed
       (325 of the 617 use the db_session fixture and therefore ran on PostgreSQL; the other 292 are pure-logic / HTTP-mock /
        ML tests with no database, or CLI/migration tests that create their own SQLite files)
COMPASS_TEST_SUITE_DATABASE_URL=... pytest tests/test_caching.py
    -> 12 passed (the single-flight concurrency tests, with real PostgreSQL connections per thread)
```
Not exercised by this run: PostgreSQL 16 on Linux / the official `postgres:16` image, other PostgreSQL versions, `pg_upgrade`,
connection-pool exhaustion under load, replication, and any production configuration (TLS, roles with limited privileges).
