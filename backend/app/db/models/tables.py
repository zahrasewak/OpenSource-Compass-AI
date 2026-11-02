"""ORM models for the OpenSource Compass AI schema.

No GitHub tokens or other secrets are stored in any table.
"""

import enum
from datetime import datetime

from sqlalchemy import false as sa_false
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, updated_col, utcnow_col


class ExecutionStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    UNSUPPORTED = "UNSUPPORTED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_user_id: Mapped[int] = mapped_column(unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = utcnow_col()
    updated_at: Mapped[datetime] = updated_col()


class UserSkill(Base):
    __tablename__ = "user_skills"
    __table_args__ = (
        UniqueConstraint("user_id", "skill_name"),
        CheckConstraint("proficiency >= 0 AND proficiency <= 1", name="proficiency_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    skill_name: Mapped[str] = mapped_column(String(100), nullable=False)
    proficiency: Mapped[float] = mapped_column(Float, nullable=False)  # 0..1, self-reported unless source says otherwise
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    # language | framework | library | skill (validated at the API layer)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="skill", server_default="skill")
    created_at: Mapped[datetime] = utcnow_col()


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    interests: Mapped[list] = mapped_column(JSONType, default=list)
    preferred_difficulty: Mapped[int | None] = mapped_column(Integer)  # 1..5
    preferred_task_types: Mapped[list] = mapped_column(JSONType, default=list)
    contribution_goals: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]")
    experience_level: Mapped[str | None] = mapped_column(String(20))  # beginner|intermediate|advanced (API-validated)
    updated_at: Mapped[datetime] = updated_col()

    __table_args__ = (
        CheckConstraint(
            "preferred_difficulty IS NULL OR (preferred_difficulty BETWEEN 1 AND 5)",
            name="difficulty_range",
        ),
    )


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("owner", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    primary_language: Mapped[str | None] = mapped_column(String(50))
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    stargazers_count: Mapped[int | None] = mapped_column(Integer)
    forks_count: Mapped[int | None] = mapped_column(Integer)
    open_issues_count: Mapped[int | None] = mapped_column(Integer)  # GitHub counts open PRs too
    license_spdx: Mapped[str | None] = mapped_column(String(50))
    topics: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]")
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issues_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issues_truncated: Mapped[bool | None] = mapped_column(Boolean)  # last open-issue sync hit the page limit


class RepositorySnapshot(Base):
    __tablename__ = "repository_snapshots"
    __table_args__ = (UniqueConstraint("repository_id", "commit_sha", "analysis_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    analysis_version: Mapped[str] = mapped_column(String(30), nullable=False)
    collected_at: Mapped[datetime] = utcnow_col()
    # Observations (absolute timestamps/counts), file-detection results and per-signal errors; see docs/INTELLIGENCE.md.
    data: Mapped[dict] = mapped_column(JSONType, default=dict, server_default="{}")


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("repository_id", "issue_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    issue_number: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    labels: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    author: Mapped[str | None] = mapped_column(String(100))
    author_association: Mapped[str | None] = mapped_column(String(30))
    assignees: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]")
    comments_count: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = utcnow_col()  # when we last synchronised this row from GitHub


class IssueFeature(Base):
    __tablename__ = "issue_features"
    __table_args__ = (UniqueConstraint("issue_id", "feature_version", name="uq_issue_features_issue_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    feature_version: Mapped[str] = mapped_column(String(30), nullable=False)
    structured_features: Mapped[dict] = mapped_column(JSONType, default=dict)
    extracted_skills: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = utcnow_col()


class IssuePrediction(Base):
    __tablename__ = "issue_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    # model_version + model_status ("heuristic_baseline" | "untrained" | "trained") are required on every prediction.
    model_version: Mapped[str] = mapped_column(String(60), nullable=False)
    model_status: Mapped[str] = mapped_column(String(30), nullable=False)
    predicted_category: Mapped[str | None] = mapped_column(String(40))
    predicted_difficulty: Mapped[int | None] = mapped_column(Integer)
    class_probabilities: Mapped[dict] = mapped_column(JSONType, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float)
    explanation: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = utcnow_col()


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_recommendations_user_score", "user_id", "final_score"),
        Index("ix_recommendations_user_created", "user_id", "created_at"),
        # Same user + issue + scoring method + identical inputs/outputs => one row, so retries are idempotent.
        UniqueConstraint("user_id", "issue_id", "ranking_model_version", "input_hash",
                         name="uq_recommendations_user_issue_model_input"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    skill_score: Mapped[float | None] = mapped_column(Float)
    interest_score: Mapped[float | None] = mapped_column(Float)
    difficulty_score: Mapped[float | None] = mapped_column(Float)
    activity_score: Mapped[float | None] = mapped_column(Float)
    readiness_score: Mapped[float | None] = mapped_column(Float)
    final_score: Mapped[float] = mapped_column(Float, nullable=False)
    # Full stored explanation (components, reasons, warnings, scoring config). Immutable once written.
    components: Mapped[dict] = mapped_column(JSONType, default=dict)
    ranking_model_version: Mapped[str] = mapped_column(String(60), nullable=False)  # e.g. "heuristic_v1"
    coverage: Mapped[float | None] = mapped_column(Float)
    insufficient_information: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")  # sha256 of inputs+outputs
    data_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict, server_default="{}")  # features that were scored
    created_at: Mapped[datetime] = utcnow_col()


class SavedIssue(Base):
    __tablename__ = "saved_issues"
    __table_args__ = (UniqueConstraint("user_id", "issue_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = utcnow_col()


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_user_issue_type", "user_id", "issue_id", "feedback_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    feedback_type: Mapped[str] = mapped_column(String(40), nullable=False)
    feedback_value: Mapped[int] = mapped_column(Integer, nullable=False)
    # Raw, append-only records. Training labels are derived elsewhere (offline), never stored here.
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="explicit_feedback",
                                          server_default="explicit_feedback")  # explicit_feedback | event
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user",
                                        server_default="user")  # user | self_reported | system
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = utcnow_col()


class ExecutionJob(Base):
    __tablename__ = "execution_jobs"
    __table_args__ = (
        Index("ix_execution_jobs_user_created", "user_id", "created_at"),
        CheckConstraint(
            "status IN ('QUEUED','PREPARING','RUNNING','COMPLETED','FAILED','TIMED_OUT','CANCELLED','UNSUPPORTED')",
            name="status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ExecutionStatus.QUEUED.value)
    created_at: Mapped[datetime] = utcnow_col()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionResult(Base):
    __tablename__ = "execution_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_job_id: Mapped[int] = mapped_column(
        ForeignKey("execution_jobs.id", ondelete="CASCADE"), unique=True
    )
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    sanitized_log_reference: Mapped[str | None] = mapped_column(String(500))
    test_summary: Mapped[dict | None] = mapped_column(JSONType)
    failure_reason: Mapped[str | None] = mapped_column(Text)


class ExecutionEvent(Base):
    """Audit trail of job status and security-relevant events. Must never contain secrets."""

    __tablename__ = "execution_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_job_id: Mapped[int] = mapped_column(
        ForeignKey("execution_jobs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utcnow_col()


class AuthSession(Base):
    """A signed-in client (e.g. one browser profile). Access tokens are bound to it by `sid`; revoking it ends them
    immediately. Only hashes of refresh tokens are stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    sid: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # public id carried in access tokens
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    prev_refresh_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # for refresh-token reuse detection
    created_at: Mapped[datetime] = utcnow_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # absolute lifetime
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(40))
    client_label: Mapped[str | None] = mapped_column(String(60))
    # The client chose to keep the refresh token on disk ("Keep me signed in"): such sessions get shorter limits.
    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())


class AuthThrottleEvent(Base):
    """One recent attempt/failure against an unauthenticated auth endpoint (database-backed rate limiting; no credentials)."""

    __tablename__ = "auth_throttle_events"
    __table_args__ = (Index("ix_auth_throttle_lookup", "scope", "client", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)  # exchange | refresh | logout
    client: Mapped[str] = mapped_column(String(64), nullable=False)  # client address (IPv6 max 45 chars)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # attempt | failure
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class PairingCode(Base):
    """One-time, short-lived code an admin hands to a user to sign a client in. Stored hashed; single use."""

    __tablename__ = "pairing_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = utcnow_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
