"""Shared seeding helpers for API tests (SQLite session + stored GitHub data)."""

from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.core.security import issue_token
from app.db.models import Issue, Repository, User

SETTINGS = Settings(auth_secret="s" * 48, _env_file=None)
PROFILE = {
    "skills": [{"name": "Python", "category": "language", "proficiency": 0.9},
               {"name": "FastAPI", "category": "framework", "proficiency": 0.6}],
    "interests": ["machine learning"],
    "preferred_difficulty": 2,
}


def ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def seed_repo(db, owner="o", name="r", language="Python", topics=(), pushed_days=5, archived=False, fetched_days=0):
    repo = Repository(owner=owner, name=name, default_branch="main", primary_language=language, topics=list(topics),
                      archived=archived, pushed_at=ago(pushed_days) if pushed_days is not None else None,
                      last_fetched_at=ago(fetched_days), issues_synced_at=ago(fetched_days))
    db.add(repo)
    db.commit()
    return repo


def seed_issue(db, repo, number, title="Some bug", body=None, labels=(), assignees=(), state="open",
               fetched_days=0, updated_days=1):
    row = Issue(repository_id=repo.id, issue_number=number, title=title, body=body, state=state,
                labels=[{"name": l} for l in labels], assignees=list(assignees), created_at=ago(30),
                updated_at=ago(updated_days), fetched_at=ago(fetched_days))
    db.add(row)
    db.commit()
    return row


def make_user(db, gid, name):
    u = User(github_user_id=gid, username=name)
    db.add(u)
    db.commit()
    return {"Authorization": f"Bearer {issue_token(u.id, SETTINGS)}"}, u
