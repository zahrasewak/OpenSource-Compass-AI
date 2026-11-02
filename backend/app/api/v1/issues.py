"""Issue routes."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_difficulty_service, get_github_client, get_scorer, get_scorer_v2
from app.core.config import Settings, get_settings
from app.db.models import User
from app.db.session import get_db
from app.schemas.github_data import IssueNumberPath, IssueOut, OwnerPath, RepoPath
from app.ml import DifficultyModelService
from app.recommend import Scorer
from app.schemas.matches import IssueMatchOut, IssueMatchRequest
from app.services import matches as match_service
from app.schemas.intelligence import ReadinessOut
from app.schemas.ml import DifficultyEstimateOut
from app.services import difficulty_api, intel_api
from app.services import repositories as service
from app.services.github import GitHubClient

router = APIRouter(prefix="/issues", tags=["issues"])


@router.get("/{owner}/{repo}/{number}", response_model=IssueOut)
def get_issue(
    owner: OwnerPath,
    repo: RepoPath,
    number: IssueNumberPath,
    refresh: bool = False,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
):
    """One issue's public data. Contains no user-specific or profile-derived fields."""
    return service.get_issue(db, gh, settings, owner, repo, number, refresh=refresh)


@router.get("/{owner}/{repo}/{number}/readiness", response_model=ReadinessOut)
def get_readiness(
    owner: OwnerPath,
    repo: RepoPath,
    number: IssueNumberPath,
    refresh: bool = False,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
):
    """How well this issue and its repository prepare a contributor to start. Detection only: nothing is executed."""
    return intel_api.issue_readiness(db, gh, settings, owner, repo, number, refresh)


@router.post("/{owner}/{repo}/{number}/difficulty", response_model=DifficultyEstimateOut)
def estimate_difficulty(
    owner: OwnerPath,
    repo: RepoPath,
    number: IssueNumberPath,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
    service: DifficultyModelService = Depends(get_difficulty_service),
):
    """Difficulty indication: the maintainer-label hint (deterministic) and, if a model artifact is configured and
    usable, an ML estimate reported separately. Falls back to the hint when the model is unavailable."""
    return difficulty_api.estimate(db, gh, settings, service, owner, repo, number)


@router.post("/{owner}/{repo}/{number}/match", response_model=IssueMatchOut)
def match_issue(
    owner: OwnerPath,
    repo: RepoPath,
    number: IssueNumberPath,
    body: IssueMatchRequest | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gh: GitHubClient = Depends(get_github_client),
    settings: Settings = Depends(get_settings),
    scorer: Scorer = Depends(get_scorer),
    scorer_v2: Scorer = Depends(get_scorer_v2),
):
    """Heuristic match score of ONE issue for the caller (not an ML prediction). With `persist` the result is stored
    idempotently and carries a `match_id` that feedback can reference."""
    body = body or IssueMatchRequest()
    chosen = scorer_v2 if body.scorer == "heuristic_v2" else scorer
    return match_service.match_single_issue(db, gh, settings, chosen, user, owner, repo, number, persist=body.persist)
