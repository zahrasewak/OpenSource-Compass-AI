"""Resolve the issue a request refers to, always scoped to the authenticated user."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import Issue, Recommendation, Repository
from app.services import repositories as repo_service


def get_owned_match(db: Session, user_id: int, match_id: int) -> Recommendation:
    """A match belonging to this user; someone else's id looks exactly like a missing one (404)."""
    rec = db.scalar(select(Recommendation).where(Recommendation.id == match_id, Recommendation.user_id == user_id))
    if rec is None:
        raise AppError("match_not_found", "Match not found", 404)
    return rec


def resolve_stored_target(
    db: Session, user_id: int, *, match_id: int | None, ref: tuple[str, str, int] | None
) -> tuple[Repository, Issue, Recommendation | None]:
    """Resolve to an issue already stored locally (no GitHub calls). `ref` is (owner, repo, number)."""
    rec = get_owned_match(db, user_id, match_id) if match_id is not None else None
    if ref is not None:
        found = repo_service.find_issue(db, *ref)
        if found is None:
            raise AppError("issue_not_found", "Issue is not known to this service; open or search it first", 404)
        repo, issue = found
        if rec is not None and rec.issue_id != issue.id:
            raise AppError("target_mismatch", "match_id and the issue reference refer to different issues", 422)
        return repo, issue, rec
    if rec is None:  # unreachable via the request schemas, kept as a guard
        raise AppError("target_required", "match_id or an issue reference is required", 422)
    issue = db.get(Issue, rec.issue_id)
    return db.get(Repository, issue.repository_id), issue, rec
