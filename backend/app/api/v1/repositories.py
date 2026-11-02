"""Repository routes (public GitHub repositories only)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_github_client
from app.core.config import Settings, get_settings
from app.db.models import User
from app.db.session import get_db
from app.schemas.github_data import IssueListOut, OwnerPath, RepoPath, RepositoryOut
from app.schemas.intelligence import HealthOut
from app.services import intel_api
from app.services import repositories as service
from app.services.github import GitHubClient

router = APIRouter(prefix="/repositories", tags=["repositories"])


@router.get("/{owner}/{repo}", response_model=RepositoryOut)
def get_repository(
    owner: OwnerPath,
    repo: RepoPath,
    refresh: bool = False,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
):
    """Repository metadata. Served from the database unless missing or stale."""
    row = service.get_or_sync_repository(db, gh, settings, owner, repo, refresh=refresh)
    return service.repository_out(row)


@router.get("/{owner}/{repo}/issues", response_model=IssueListOut)
def list_issues(
    owner: OwnerPath,
    repo: RepoPath,
    state: service.IssueState = "open",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    refresh: bool = False,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
):
    """Paginated issues (never pull requests). Open issues are synchronised from GitHub when stale."""
    return service.list_repository_issues(
        db, gh, settings, owner, repo, state=state, limit=limit, offset=offset, refresh=refresh
    )


@router.get("/{owner}/{repo}/health", response_model=HealthOut)
def get_health(
    owner: OwnerPath,
    repo: RepoPath,
    refresh: bool = False,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
):
    """Repository health (activity, responsiveness, maintenance) from stored observations; collected from
    GitHub only if missing or stale. A heuristic summary, not an ML prediction."""
    return intel_api.repository_health(db, gh, settings, owner, repo, refresh)
