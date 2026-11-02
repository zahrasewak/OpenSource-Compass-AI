"""Saved-issue routes (all scoped to the authenticated user)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_github_client
from app.core.config import Settings, get_settings
from app.db.models import User
from app.db.session import get_db
from app.schemas.github_data import IssueNumberPath, OwnerPath, RepoPath
from app.schemas.saved_issues import SavedIssueListOut, SavedIssueOut, SavedLookupOut, SaveIssueRequest
from app.services import saved_issues as service
from app.services.github import GitHubClient

router = APIRouter(prefix="/saved-issues", tags=["saved-issues"])


@router.post("", response_model=SavedIssueOut)
def save(
    body: SaveIssueRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
):
    """201 when newly saved, 200 (`created: false`) if it was already saved."""
    out = service.save_issue(db, gh, settings, user, body)
    response.status_code = 201 if out.created else 200
    return out


@router.get("", response_model=SavedIssueListOut)
def list_saved(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.list_saved(db, user.id, limit, offset)


@router.delete("/{saved_id}", status_code=204)
def unsave(
    saved_id: Annotated[int, Path(ge=1, le=2_147_483_647, description="Saved-entry id from the list endpoint")],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service.unsave(db, user.id, saved_id)
    return Response(status_code=204)


@router.get("/lookup/{owner}/{repo}/{number}", response_model=SavedLookupOut)
def lookup(
    owner: OwnerPath,
    repo: RepoPath,
    number: IssueNumberPath,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Whether the caller has saved this issue. Never contacts GitHub; an issue not stored yet is simply not saved."""
    return service.lookup_saved(db, user.id, owner, repo, number)
