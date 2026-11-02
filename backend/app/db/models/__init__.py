"""Import all models so Base.metadata is fully populated (needed by Alembic)."""

from app.db.models.tables import (  # noqa: F401
    AuthSession,
    AuthThrottleEvent,
    ExecutionEvent,
    ExecutionJob,
    ExecutionResult,
    ExecutionStatus,
    Feedback,
    Issue,
    PairingCode,
    IssueFeature,
    IssuePrediction,
    Recommendation,
    Repository,
    RepositorySnapshot,
    SavedIssue,
    User,
    UserPreference,
    UserSkill,
)
