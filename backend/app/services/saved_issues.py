"""Saved issues. Saving is a bookmark plus a system-observed `saved` event; it is NOT feedback that
the issue was good and NOT evidence of a contribution."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import Issue, Repository, SavedIssue, User
from app.schemas.saved_issues import SavedIssueListOut, SavedIssueOut, SavedLookupOut, SaveIssueRequest
from app.services import feedback as feedback_service
from app.services import repositories as repo_service
from app.services.github import GitHubClient
from app.services.targets import resolve_stored_target


def _out(saved: SavedIssue, repo: Repository, issue: Issue, created: bool | None = None) -> SavedIssueOut:
    return SavedIssueOut(
        id=saved.id, saved_at=saved.created_at, created=created, owner=repo.owner, repo=repo.name,
        number=issue.issue_number, title=issue.title, state=issue.state,
        html_url=f"https://github.com/{repo.owner}/{repo.name}/issues/{issue.issue_number}",
        issue_fetched_at=issue.fetched_at,
    )


def _existing(db: Session, user_id: int, issue_id: int) -> SavedIssue | None:
    return db.scalar(select(SavedIssue).where(SavedIssue.user_id == user_id, SavedIssue.issue_id == issue_id))


def save_issue(db: Session, gh: GitHubClient, settings: Settings, user: User, req: SaveIssueRequest) -> SavedIssueOut:
    """Idempotent save. Issues already stored locally are saved without contacting GitHub, so a
    since-deleted or temporarily unreachable issue can still be bookmarked from a match the user saw.
    Unknown issues are fetched (and rejected with 404 if GitHub no longer has them)."""
    ref = (req.owner, req.repo, req.number)
    try:
        repo, issue, rec = resolve_stored_target(db, user.id, match_id=req.match_id, ref=ref)
    except AppError as exc:
        if exc.code != "issue_not_found":
            raise
        repo_service.get_issue(db, gh, settings, req.owner, req.repo, req.number)  # may raise 404 / upstream errors
        repo, issue, rec = resolve_stored_target(db, user.id, match_id=req.match_id, ref=ref)

    existing = _existing(db, user.id, issue.id)
    if existing is not None:
        return _out(existing, repo, issue, created=False)
    saved = SavedIssue(user_id=user.id, issue_id=issue.id)
    db.add(saved)
    feedback_service.add_event(db, user.id, issue.id, "saved", 1, rec.id if rec else None)
    try:
        db.commit()  # one transaction: bookmark + observed event
    except IntegrityError:  # concurrent identical save won the unique(user_id, issue_id) race
        db.rollback()
        winner = _existing(db, user.id, issue.id)
        if winner is None:
            raise
        return _out(winner, repo, issue, created=False)
    return _out(saved, repo, issue, created=True)


def list_saved(db: Session, user_id: int, limit: int, offset: int) -> SavedIssueListOut:
    base = (select(SavedIssue, Issue, Repository)
            .join(Issue, SavedIssue.issue_id == Issue.id)
            .join(Repository, Issue.repository_id == Repository.id)
            .where(SavedIssue.user_id == user_id))
    total = db.scalar(select(func.count()).select_from(SavedIssue).where(SavedIssue.user_id == user_id)) or 0
    rows = db.execute(base.order_by(SavedIssue.created_at.desc(), SavedIssue.id.desc()).limit(limit).offset(offset)).all()
    return SavedIssueListOut(items=[_out(s, r, i) for s, i, r in rows], total=total, limit=limit, offset=offset)


def unsave(db: Session, user_id: int, saved_id: int) -> None:
    """Delete one of the caller's saved entries (404 if it doesn't exist or isn't theirs)."""
    saved = db.scalar(select(SavedIssue).where(SavedIssue.id == saved_id, SavedIssue.user_id == user_id))
    if saved is None:
        raise AppError("saved_issue_not_found", "Saved issue not found", 404)
    feedback_service.add_event(db, user_id, saved.issue_id, "unsaved", 0)
    db.delete(saved)
    db.commit()


def lookup_saved(db: Session, user_id: int, owner: str, repo_name: str, number: int) -> SavedLookupOut:
    """Is this issue in the caller's saved list? Reads stored data only (never calls GitHub) and only the caller's rows."""
    found = repo_service.find_issue(db, owner, repo_name, number)
    if found is None:
        return SavedLookupOut(saved=False)
    repo, issue = found
    saved = _existing(db, user_id, issue.id)
    return SavedLookupOut(saved=True, saved_issue=_out(saved, repo, issue)) if saved else SavedLookupOut(saved=False)
