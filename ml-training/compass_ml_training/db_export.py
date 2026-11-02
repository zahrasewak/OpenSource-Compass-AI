"""Export issues already stored by the backend (public repositories only, by construction) as raw records."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Issue, Repository
from app.ml.features import redact_personal
from app.ml.schema import IssueRecord
from compass_ml_training.dataset import make_record_id


def export_issues(db: Session) -> list[IssueRecord]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    out = []
    for issue, repo in db.execute(select(Issue, Repository).join(Repository, Issue.repository_id == Repository.id)):
        full = f"{repo.owner}/{repo.name}"
        out.append(IssueRecord(
            record_id=make_record_id(full, issue.issue_number), repo=full, issue_number=issue.issue_number,
            html_url=f"https://github.com/{full}/issues/{issue.issue_number}", title=redact_personal(issue.title),
            body=redact_personal(issue.body), labels=[l.get("name", "") for l in (issue.labels or []) if isinstance(l, dict)],
            author_association=issue.author_association, repo_language=repo.primary_language,
            created_at=issue.created_at.isoformat(), fetched_at=fetched_at, source="database_export"))
    return out
