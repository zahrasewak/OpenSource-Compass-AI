"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. No secrets have defaults that would work in production."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="COMPASS_", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://compass:compass@localhost:5432/compass"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"

    # Bearer-token signing key. No default: tokens cannot be issued or accepted until it is set
    # (>= 32 chars). Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
    auth_secret: SecretStr | None = None
    auth_issuer: str = "opensource-compass"
    auth_audience: str = "compass-api"
    auth_token_ttl_minutes: int = 720  # legacy CLI-issued tokens only (see auth_allow_legacy_tokens)
    # Session-based authentication (docs/AUTHENTICATION.md)
    auth_access_ttl_seconds: int = Field(default=900, ge=5)  # short-lived access token
    auth_refresh_ttl_hours: int = Field(default=168, ge=1)  # each refresh token; rotated on every use
    auth_session_max_days: int = Field(default=30, ge=1)  # absolute session lifetime: re-pairing required afterwards
    auth_pairing_ttl_minutes: int = Field(default=10, ge=1)  # one-time pairing code lifetime
    # Sessions whose client keeps the refresh token on disk ("Keep me signed in") get tighter limits, because a plaintext
    # credential at rest is exposed to anything that can read the browser profile. Defaults: idle 3 days, absolute 14 days.
    auth_persistent_refresh_ttl_hours: int = Field(default=72, ge=1)
    auth_persistent_session_max_days: int = Field(default=14, ge=1)
    # Rate limiting of the unauthenticated auth endpoints (in-memory, per process: see app/core/ratelimit.py for limits)
    auth_rate_limit_enabled: bool = True
    # "memory": per process (default). "database": shared by every worker/instance that uses the same database (recommended
    # whenever more than one process serves requests). See app/core/ratelimit.py.
    auth_rate_limit_backend: Literal["memory", "database"] = "memory"
    auth_attempts_per_minute: int = Field(default=20, ge=1)  # per client address, every request
    auth_max_failures: int = Field(default=5, ge=1)  # rejected codes/tokens per client before a lockout...
    auth_failure_window_seconds: int = Field(default=900, ge=1)  # ...within this window (the lockout lasts until the oldest expires)
    auth_global_attempts_per_minute: int = Field(default=600, ge=1)  # database-protection ceiling across all clients
    # Number of trusted reverse proxies in front of the API. 0 = ignore X-Forwarded-For (it is client-controlled).
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5)
    # Legacy tokens (no session, cannot be revoked) are accepted only outside production and only if allowed.
    auth_allow_legacy_tokens: bool = True

    # GitHub API client. The token is optional: public data works unauthenticated (60 req/h);
    # a token raises the limit to 5000 req/h. It must come from the environment, never source code.
    github_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_timeout_seconds: float = 10.0
    github_max_retries: int = 3
    github_max_rate_limit_wait_seconds: float = 60.0
    # Synchronisation policy for stored repositories/issues.
    github_sync_ttl_seconds: int = 900  # stored data younger than this is served without calling GitHub
    github_min_refresh_seconds: int = 30  # ?refresh=true is ignored for data younger than this (quota protection)
    github_issue_sync_max_pages: int = 3  # x100 open issues per sync; beyond that the result is flagged truncated

    # Per-request budgets and cancellation (app/core/requestctx.py). Must stay below the extension's own timeouts
    # (30 s normal, 90 s for repository health/readiness) so the client receives a partial result instead of giving up.
    request_cancellation_enabled: bool = True  # stop GitHub work when the client disconnects
    request_deadline_seconds: float = Field(default=25.0, gt=0)
    slow_request_deadline_seconds: float = Field(default=60.0, gt=0)  # repository health / issue readiness

    # Only these origins may call the API from a browser (the extension origin is added by config).
    cors_origins: list[str] = Field(default_factory=list)

    # Recommendation weights; documented defaults, not scientifically optimised values.
    weight_skill: float = 0.35
    weight_interest: float = 0.25
    weight_difficulty: float = 0.20
    weight_activity: float = 0.15
    weight_readiness: float = 0.05

    # Heuristic recommendation baseline (see docs/RECOMMENDATIONS.md).
    reco_unknown_fill: float = Field(default=25.0, ge=0, le=100)  # score used for unknown/low-confidence evidence
    reco_min_coverage: float = Field(default=0.4, ge=0, le=1)  # below this an item is "insufficient information"
    reco_max_data_age_days: int = Field(default=7, ge=1)  # older stored data is treated as stale
    reco_max_candidates: int = Field(default=2000, ge=1, le=20000)

    # Repository intelligence (docs/INTELLIGENCE.md)
    intel_window_days: int = Field(default=90, ge=14, le=365)
    intel_snapshot_ttl_seconds: int = 24 * 3600  # complete analyses are reused this long without calling GitHub
    intel_linked_pr_ttl_seconds: int = 3600

    # Optional ML (docs/ML.md). Unset = the difficulty model is simply reported as unavailable.
    ml_difficulty_model_dir: str | None = None
    ml_allow_synthetic_artifacts: bool = False  # tests only: artifacts trained on synthetic data are refused otherwise
    ml_allow_exploratory_artifacts: bool = False  # models trained below the sufficiency gate are refused unless set

    execution_retention_days: int = 14

    @model_validator(mode="after")
    def _weights_valid(self) -> "Settings":
        weights = [self.weight_skill, self.weight_interest, self.weight_difficulty,
                   self.weight_activity, self.weight_readiness]
        if any(w < 0 for w in weights) or abs(sum(weights) - 1.0) > 1e-6:
            raise ValueError("recommendation weights must be non-negative and sum to 1.0")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
