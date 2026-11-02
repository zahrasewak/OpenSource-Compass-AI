"""Recommendation routes. The user is always the authenticated caller."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_github_client, get_scorer, get_scorer_v2
from app.core.config import Settings, get_settings
from app.db.models import User
from app.db.session import get_db
from app.recommend import Scorer
from app.schemas.matches import MatchSearchOut, MatchSearchRequest, StoredMatchListOut, StoredMatchOut
from app.services import match_store
from app.services import matches as service
from app.services.github import GitHubClient

router = APIRouter(prefix="/matches", tags=["matches"])

_REPO_FILTER = r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"


@router.post("/search", response_model=MatchSearchOut)
def search(
    body: MatchSearchRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
    scorer: Scorer = Depends(get_scorer),
    scorer_v2: Scorer = Depends(get_scorer_v2),
):
    """Rank stored open issues for the caller using the heuristic baseline (not an ML model).

    With `persist` (default) the returned page is stored; each item then carries a `match_id`.
    """
    chosen = scorer_v2 if body.scorer == "heuristic_v2" else scorer
    return service.search_matches(db, gh, settings, chosen, user, body)


@router.get("", response_model=StoredMatchListOut)
def list_matches(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    model: Annotated[str | None, Query(max_length=60, description='Scoring method id, e.g. "heuristic_v1"')] = None,
    repository: Annotated[str | None, Query(pattern=_REPO_FILTER, description="owner/name")] = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    include_insufficient: bool = True,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The caller's stored matches, newest first."""
    return match_store.list_matches(db, user.id, limit=limit, offset=offset, model_id=model, repository=repository,
                                    min_score=min_score, include_insufficient=include_insufficient)


@router.get("/{match_id}", response_model=StoredMatchOut)
def get_match(
    match_id: Annotated[int, Path(ge=1, le=2_147_483_647)],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One stored match with its full historical score breakdown (404 if it isn't the caller's)."""
    return match_store.get_match(db, user.id, match_id)
