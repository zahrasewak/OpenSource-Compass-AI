"""Repository and issue synchronisation + queries.

All GitHub access goes through GitHubClient (HTTP, retries, rate limits and ETag caching live
there). This module only decides *when* to call it and persists the result idempotently.
It has no knowledge of users or profiles.
"""

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import Issue, Repository
from app.schemas.github_data import IssueListOut, IssueOut, LabelOut, RepositoryOut
from app.services.github import GitHubClient
from app.services.github.errors import GitHubNotFoundError
from app.services.github.models import Issue as GHIssue
from app.services.singleflight import locks
from app.services.github.models import Repository as GHRepository

IssueState = Literal["open", "closed", "all"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite returns naive datetimes


def _is_fresh(ts: datetime | None, max_age_seconds: float) -> bool:
    return ts is not None and (utcnow() - _aware(ts)).total_seconds() < max_age_seconds


def _max_age(settings: Settings, refresh: bool) -> float:
    """A refresh request is still ignored for very recent data, so clients cannot burn GitHub quota."""
    return settings.github_min_refresh_seconds if refresh else settings.github_sync_ttl_seconds


def _repo_not_found() -> AppError:
    return AppError("repository_not_found", "Repository not found, or it is not public", 404)


# ------------------------------------------------------------------ repositories


def _find_repo(db: Session, owner: str, name: str) -> Repository | None:
    """GitHub names are case-insensitive, so look up case-insensitively."""
    return db.scalar(
        select(Repository).where(func.lower(Repository.owner) == owner.lower(), func.lower(Repository.name) == name.lower())
    )


def find_repository(db: Session, owner: str, name: str) -> Repository | None:
    """Public lookup (case-insensitive) of a stored repository."""
    return _find_repo(db, owner, name)


def find_issue(db: Session, owner: str, name: str, number: int) -> tuple[Repository, Issue] | None:
    """Stored (repository, issue) for a GitHub reference, or None. Never calls GitHub."""
    repo = _find_repo(db, owner, name)
    if repo is None:
        return None
    issue = db.scalar(select(Issue).where(Issue.repository_id == repo.id, Issue.issue_number == number))
    return (repo, issue) if issue is not None else None


def _repo_values(data: GHRepository, now: datetime) -> dict:
    return dict(
        owner=data.owner,
        name=data.name,
        default_branch=data.default_branch,
        description=data.description,
        primary_language=data.language,
        archived=data.archived,
        stargazers_count=data.stargazers_count,
        forks_count=data.forks_count,
        open_issues_count=data.open_issues_count,
        license_spdx=data.license_spdx,
        topics=list(data.topics),
        pushed_at=data.pushed_at,
        last_fetched_at=now,
    )


def _upsert_repo(db: Session, data: GHRepository) -> Repository:
    values = _repo_values(data, utcnow())
    for _ in range(2):  # second pass handles losing an insert race to a concurrent request
        row = _find_repo(db, data.owner, data.name)
        if row is None:
            row = Repository(**values)
            db.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        try:
            db.commit()
            return row
        except IntegrityError:
            db.rollback()
    raise AppError("sync_conflict", "Concurrent synchronisation conflict; please retry", 503)


def release_snapshot(db: Session) -> None:
    """End the current read transaction before waiting on another request, so no DB locks/snapshot are held while
    blocked, and later reads see what the other request committed. Skipped if the caller has unflushed changes."""
    if not (db.new or db.dirty or db.deleted):
        db.rollback()
    db.expire_all()


def get_or_sync_repository(
    db: Session, gh: GitHubClient, settings: Settings, owner: str, repo: str, *, refresh: bool = False
) -> Repository:
    """Return the stored repository, calling GitHub only if it is missing or stale."""
    row = _find_repo(db, owner, repo)
    if row is not None and _is_fresh(row.last_fetched_at, _max_age(settings, refresh)):
        return row
    release_snapshot(db)
    with locks.hold(("repo", owner.lower(), repo.lower())):  # concurrent requests for this repo share ONE GitHub call
        release_snapshot(db)
        row = _find_repo(db, owner, repo)
        if row is not None and _is_fresh(row.last_fetched_at, _max_age(settings, refresh)):
            return row  # another request refreshed it while we waited
        try:
            data = gh.get_repository(owner, repo)
        except GitHubNotFoundError:
            raise _repo_not_found() from None
        # Fail closed: only an explicit `private: false` is accepted. Private data is never stored or returned.
        if data.private is not False:
            raise _repo_not_found()
        return _upsert_repo(db, data)


def repository_out(row: Repository) -> RepositoryOut:
    return RepositoryOut(
        owner=row.owner,
        name=row.name,
        full_name=f"{row.owner}/{row.name}",
        html_url=f"https://github.com/{row.owner}/{row.name}",
        description=row.description,
        default_branch=row.default_branch,
        primary_language=row.primary_language,
        topics=row.topics or [],
        stargazers_count=row.stargazers_count,
        forks_count=row.forks_count,
        open_issues_count=row.open_issues_count,
        archived=row.archived,
        license_spdx=row.license_spdx,
        pushed_at=row.pushed_at,
        fetched_at=row.last_fetched_at,
        issues_synced_at=row.issues_synced_at,
    )


# ------------------------------------------------------------------ issues


def _issue_values(data: GHIssue, now: datetime) -> dict:
    return dict(
        title=data.title,
        body=data.body,
        state=data.state,
        labels=[l.model_dump() for l in data.labels],
        created_at=data.created_at,
        updated_at=data.updated_at,
        closed_at=data.closed_at,
        author=data.author,
        author_association=data.author_association,
        assignees=list(data.assignees),
        comments_count=data.comments,
        fetched_at=now,
    )


def _upsert_issues(db: Session, repo: Repository, issues: list[GHIssue], *, mark_issues_synced: tuple[bool] | None = None) -> None:
    """Insert or update issues in one transaction, safely repeatable and race-tolerant."""
    now = utcnow()
    for _ in range(2):
        existing = {}
        if issues:
            numbers = [i.number for i in issues]
            existing = {
                r.issue_number: r
                for r in db.scalars(select(Issue).where(Issue.repository_id == repo.id, Issue.issue_number.in_(numbers)))
            }
        for data in issues:
            values = _issue_values(data, now)
            row = existing.get(data.number)
            if row is None:
                db.add(Issue(repository_id=repo.id, issue_number=data.number, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        if mark_issues_synced is not None:
            repo.issues_synced_at = now
            repo.issues_truncated = mark_issues_synced[0]
        try:
            db.commit()
            return
        except IntegrityError:
            db.rollback()
    raise AppError("sync_conflict", "Concurrent synchronisation conflict; please retry", 503)


def sync_open_issues(db: Session, gh: GitHubClient, settings: Settings, repo: Repository) -> None:
    """Fetch open issues (not PRs) from GitHub and upsert them. Nothing is written if GitHub fails.

    Only *open* issues are synchronised; rows for issues that have since closed keep their last
    stored state until they are fetched individually (see docs/API.md).
    """
    result = gh.list_issues(repo.owner, repo.name, state="open", max_pages=settings.github_issue_sync_max_pages)
    _upsert_issues(db, repo, result.items, mark_issues_synced=(result.truncated,))


def issue_out(repo: Repository, row: Issue) -> IssueOut:
    return IssueOut(
        repository=f"{repo.owner}/{repo.name}",
        number=row.issue_number,
        title=row.title,
        body=row.body,
        state=row.state,
        labels=[LabelOut(**l) for l in (row.labels or [])],
        author=row.author,
        author_association=row.author_association,
        assignees=row.assignees or [],
        comments_count=row.comments_count,
        html_url=f"https://github.com/{repo.owner}/{repo.name}/issues/{row.issue_number}",
        created_at=row.created_at,
        updated_at=row.updated_at,
        closed_at=row.closed_at,
        fetched_at=row.fetched_at,
    )


def ensure_repository_issues(
    db: Session, gh: GitHubClient, settings: Settings, owner: str, repo: str, *, refresh: bool = False
) -> Repository:
    """Make sure the repository and its open issues are stored and fresh; GitHub is called only if not."""
    repo_row = get_or_sync_repository(db, gh, settings, owner, repo, refresh=refresh)
    if not _is_fresh(repo_row.issues_synced_at, _max_age(settings, refresh)):
        release_snapshot(db)
        with locks.hold(("issues", repo_row.id)):
            release_snapshot(db)
            if not _is_fresh(repo_row.issues_synced_at, _max_age(settings, refresh)):  # attributes were reloaded above
                sync_open_issues(db, gh, settings, repo_row)
    return repo_row


def list_repository_issues(
    db: Session,
    gh: GitHubClient,
    settings: Settings,
    owner: str,
    repo: str,
    *,
    state: IssueState,
    limit: int,
    offset: int,
    refresh: bool,
) -> IssueListOut:
    repo_row = ensure_repository_issues(db, gh, settings, owner, repo, refresh=refresh)

    conditions = [Issue.repository_id == repo_row.id]
    if state != "all":
        conditions.append(Issue.state == state)
    total = db.scalar(select(func.count()).select_from(Issue).where(*conditions)) or 0
    rows = db.scalars(
        select(Issue).where(*conditions).order_by(Issue.updated_at.desc(), Issue.id).limit(limit).offset(offset)
    ).all()
    return IssueListOut(
        items=[issue_out(repo_row, r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        issues_synced_at=repo_row.issues_synced_at,
        sync_truncated=repo_row.issues_truncated,
    )


def get_issue(
    db: Session, gh: GitHubClient, settings: Settings, owner: str, repo: str, number: int, *, refresh: bool = False
) -> IssueOut:
    repo_row = get_or_sync_repository(db, gh, settings, owner, repo)
    row = db.scalar(select(Issue).where(Issue.repository_id == repo_row.id, Issue.issue_number == number))
    if row is not None and _is_fresh(row.fetched_at, _max_age(settings, refresh)):
        return issue_out(repo_row, row)
    release_snapshot(db)
    with locks.hold(("issue", repo_row.id, number)):  # concurrent requests for this issue share ONE GitHub call
        release_snapshot(db)
        row = db.scalar(select(Issue).where(Issue.repository_id == repo_row.id, Issue.issue_number == number))
        if row is not None and _is_fresh(row.fetched_at, _max_age(settings, refresh)):
            return issue_out(repo_row, row)  # another request fetched it while we waited
        try:
            data = gh.get_issue(repo_row.owner, repo_row.name, number)
        except GitHubNotFoundError:
            raise AppError("issue_not_found", "Issue not found", 404) from None
        if data.is_pull_request:
            raise AppError("not_an_issue", "This number refers to a pull request, not an issue", 404)
        _upsert_issues(db, repo_row, [data])
        row = db.scalar(select(Issue).where(Issue.repository_id == repo_row.id, Issue.issue_number == number))
        return issue_out(repo_row, row)
