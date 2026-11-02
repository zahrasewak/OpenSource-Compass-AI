"""Run the PostgreSQL validation steps (the same ones as the `migrations-postgres` CI job) against a real server.

    python scripts/validate_postgres.py postgresql://USER:PASSWORD@HOST:PORT [--log ../docs/evidence/postgres-run.md]

The URL must point at a server where the user may create and DROP databases (it is used to create two scratch
databases, `compass_validate` and `compass_validate_suite`; any existing ones with those names are dropped).

Steps, in order (each is a pass/fail line; the script exits non-zero if any fails):
  1. alembic upgrade head        2. alembic current == head      3. alembic check (models == migrations)
  4. schema checks               5. alembic downgrade base       6. alembic upgrade head again
  7. tests/test_postgres_integration.py                           8. the whole test suite on PostgreSQL

Nothing here proves anything about a server you did not run it against: record the server's `select version()` output,
which this script prints. SQLite results and offline migration rendering are NOT PostgreSQL validation.
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

BACKEND = Path(__file__).resolve().parents[1]
PY = sys.executable
DB_MIG, DB_SUITE = "compass_validate", "compass_validate_suite"


def run(cmd, env, log, label):
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout + proc.stderr).strip()
    log.append(f"### {label}\n```\n$ {' '.join(str(c) for c in cmd)}\n{out[-6000:]}\n[exit {proc.returncode}, {time.time() - t0:.1f}s]\n```\n")
    return proc.returncode, out


def schema_checks(url: str) -> list[str]:
    """Things SQLite cannot show. Returns a list of problems (empty = all good)."""
    problems = []
    with psycopg.connect(url) as c:
        q = lambda sql: c.execute(sql).fetchall()  # noqa: E731
        json_cols = q("select table_name, column_name, data_type from information_schema.columns where table_schema='public' and data_type in ('json','jsonb')")
        if not json_cols:
            problems.append("no JSON columns found")
        problems += [f"{t}.{col} is {dt}, expected jsonb" for t, col, dt in json_cols if dt != "jsonb"]
        naive = q("select table_name, column_name from information_schema.columns where table_schema='public' and data_type = 'timestamp without time zone'")
        problems += [f"{t}.{col} is timestamp without time zone" for t, col in naive]
        for table, cols in {"auth_sessions": {"persistent"}, "pairing_codes": {"code_hash"}}.items():
            have = {r[0] for r in q(f"select column_name from information_schema.columns where table_name='{table}'")}
            problems += [f"{table}.{col} missing" for col in cols - have]
        persistent = q("select column_default, is_nullable from information_schema.columns where table_name='auth_sessions' and column_name='persistent'")
        if persistent and (persistent[0][1] != "NO" or "false" not in str(persistent[0][0]).lower()):
            problems.append(f"auth_sessions.persistent should be NOT NULL DEFAULT false, got {persistent}")
        want = {"uq_recommendations_user_issue_model_input", "uq_issue_features_issue_version", "uq_auth_sessions_refresh_hash",
                "uq_auth_sessions_sid", "uq_pairing_codes_code_hash", "ck_execution_jobs_status_valid"}
        have = {r[0] for r in q("select conname from pg_constraint where connamespace='public'::regnamespace")}
        problems += [f"constraint {n} missing" for n in sorted(want - have)]
        fk = q("select confdeltype from pg_constraint where conname like 'fk_feedback_recommendation%'")
        if fk != [("n",)]:
            problems.append(f"feedback->recommendation FK should be ON DELETE SET NULL, got {fk}")
        idx = {r[0] for r in q("select indexname from pg_indexes where schemaname='public'")}
        problems += [f"index {n} missing" for n in ("ix_auth_sessions_user_id", "ix_auth_sessions_prev_refresh_hash", "ix_pairing_codes_user_id") if n not in idx]
        defaults = q("select table_name, column_name from information_schema.columns where table_schema='public' and data_type like 'timestamp%' "
                     "and column_name in ('created_at') and column_default is null and table_name not in ('alembic_version')")
        problems += [f"{t}.{col} has no default" for t, col in defaults if t in {"users", "auth_sessions", "pairing_codes", "saved_issues", "feedback", "recommendations"}]
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("server_url", help="postgresql://user:password@host:port (a database in the URL is ignored)")
    ap.add_argument("--log", help="write a markdown log of every command and its output to this file")
    args = ap.parse_args()
    admin = args.server_url.rstrip("/")
    host_part = admin.split("//", 1)[1]
    if "/" in host_part:  # strip a database name if one was given
        admin = admin.split("//", 1)[0] + "//" + host_part.split("/", 1)[0]
    admin = admin.replace("postgresql+psycopg://", "postgresql://")

    log, results = [], []
    with psycopg.connect(admin + "/postgres", autocommit=True) as c:
        version = c.execute("select version()").fetchone()[0]
        for db in (DB_MIG, DB_SUITE):
            c.execute(f'drop database if exists {db} with (force)')
            c.execute(f"create database {db}")
    print(f"server: {version}")
    log.append(f"# PostgreSQL validation run ({datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})\n\nServer: `{version}`\n\nClient: Python {sys.version.split()[0]}, psycopg {psycopg.__version__}\n")

    mig_url, suite_url = admin.replace("postgresql://", "postgresql+psycopg://") + f"/{DB_MIG}", admin.replace("postgresql://", "postgresql+psycopg://") + f"/{DB_SUITE}"
    env = {**os.environ, "COMPASS_DATABASE_URL": mig_url, "PYTHONIOENCODING": "utf-8"}
    env.pop("COMPASS_TEST_DATABASE_URL", None)
    env.pop("COMPASS_TEST_SUITE_DATABASE_URL", None)

    def step(label, ok, detail=""):
        results.append((label, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {label}{('  - ' + detail) if detail else ''}")
        return ok

    rc, _ = run([PY, "-m", "alembic", "upgrade", "head"], env, log, "alembic upgrade head")
    step("1. alembic upgrade head", rc == 0)
    rc, out = run([PY, "-m", "alembic", "current"], env, log, "alembic current")
    heads_rc, heads = run([PY, "-m", "alembic", "heads"], env, log, "alembic heads")
    step("2. alembic current is head", rc == 0 and heads_rc == 0 and heads.split()[0] in out, out.splitlines()[-1] if out else "")
    rc, out = run([PY, "-m", "alembic", "check"], env, log, "alembic check")
    step("3. alembic check: models == migrations", rc == 0 and "No new upgrade operations detected" in out)
    problems = schema_checks(mig_url.replace("+psycopg", ""))
    log.append("### schema checks\n" + ("all passed\n" if not problems else "\n".join(f"- {p}" for p in problems) + "\n"))
    step("4. schema checks (JSONB, timestamptz, defaults, constraints, indexes, FK rules)", not problems, "; ".join(problems))
    rc, _ = run([PY, "-m", "alembic", "downgrade", "base"], env, log, "alembic downgrade base")
    with psycopg.connect(mig_url.replace("+psycopg", "")) as c:
        left = [r[0] for r in c.execute("select table_name from information_schema.tables where table_schema='public'").fetchall()]
    step("5. alembic downgrade base leaves only alembic_version", rc == 0 and left == ["alembic_version"], str(left))
    rc, _ = run([PY, "-m", "alembic", "upgrade", "head"], env, log, "alembic upgrade head (again)")
    step("6. alembic upgrade head again", rc == 0)
    rc, out = run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_postgres_integration.py"], {**env, "COMPASS_TEST_DATABASE_URL": mig_url}, log, "integration tests")
    step("7. tests/test_postgres_integration.py", rc == 0 and "skipped" not in out.splitlines()[-1], out.splitlines()[-1] if out else "")
    rc, out = run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], {**env, "COMPASS_TEST_SUITE_DATABASE_URL": suite_url, "COMPASS_TEST_DATABASE_URL": mig_url}, log, "whole test suite on PostgreSQL")
    step("8. whole test suite on PostgreSQL", rc == 0, out.splitlines()[-1] if out else "")

    failed = [r for r in results if not r[1]]
    log.append("## Summary\n" + "\n".join(f"- {'PASS' if ok else 'FAIL'} {label} {detail}" for label, ok, detail in results) + "\n")
    if args.log:
        Path(args.log).write_text("\n".join(log), encoding="utf-8")
        print(f"log written to {args.log}")
    print(f"\n{len(results) - len(failed)}/{len(results)} steps passed on: {version}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
