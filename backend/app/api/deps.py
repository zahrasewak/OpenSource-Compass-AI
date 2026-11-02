"""Shared FastAPI dependencies (authentication)."""

from functools import lru_cache

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import AuthConfigError, InvalidToken, verify_claims
from app.db.models import User
from app.db.session import get_db
from app.ml import DifficultyModelService
from app.recommend import HeuristicScorer, ScoringConfig, Scorer
from app.recommend.heuristic_v2 import HeuristicScorerV2, v2_config
from app.services import sessions as session_service
from app.services.github import GitHubClient, create_github_client

_bearer = HTTPBearer(auto_error=False)  # so we return our own error envelope
bearer_scheme = _bearer


def _unauthorized(message: str) -> AppError:
    return AppError("unauthorized", message, 401)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Resolve the authenticated user from the bearer token. The user id never comes from the client."""
    if credentials is None:
        raise _unauthorized("Missing bearer token")
    try:
        user_id, sid = verify_claims(credentials.credentials, settings)
    except AuthConfigError:
        raise AppError("auth_unavailable", "Authentication is not configured on the server", 503)
    except InvalidToken:
        raise _unauthorized("Invalid or expired token")
    if sid is None:
        # Legacy CLI-issued token: no session, so it cannot be revoked. Refused in production or when disabled.
        if settings.environment == "production" or not settings.auth_allow_legacy_tokens:
            raise _unauthorized("Invalid or expired token")
    elif session_service.validate_session(db, sid, user_id) is None:  # revoked, expired, unknown or someone else's
        raise _unauthorized("Invalid or expired token")
    user = db.get(User, user_id)
    if user is None:  # e.g. account deleted after the token was issued
        raise _unauthorized("Invalid or expired token")
    return user


@lru_cache
def _shared_github_client() -> GitHubClient:
    return create_github_client(get_settings())


def get_github_client() -> GitHubClient:
    """One client per process so its ETag cache and rate-limit state are shared across requests."""
    return _shared_github_client()


def get_scorer(settings: Settings = Depends(get_settings)) -> Scorer:
    """The active scorer. Swap this to compare a trained model against the heuristic baseline."""
    return HeuristicScorer(ScoringConfig.from_settings(settings))


def get_scorer_v2(settings: Settings = Depends(get_settings)) -> Scorer:
    """Experimental scorer that also uses stored repository-intelligence reports."""
    return HeuristicScorerV2(v2_config(settings))


@lru_cache
def _difficulty_service(model_dir: str | None, allow_synthetic: bool, allow_exploratory: bool) -> DifficultyModelService:
    return DifficultyModelService(model_dir, allow_synthetic, allow_exploratory=allow_exploratory)


def get_difficulty_service(settings: Settings = Depends(get_settings)) -> DifficultyModelService:
    """Lazily loads the configured difficulty model; never raises, an unusable model is reported as unavailable."""
    return _difficulty_service(settings.ml_difficulty_model_dir, settings.ml_allow_synthetic_artifacts,
                               settings.ml_allow_exploratory_artifacts)
