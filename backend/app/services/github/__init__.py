"""GitHub integration package. Independent of profiles, recommendations and ML."""

from app.core.config import Settings
from app.services.github.cache import ETagCache, InMemoryETagCache
from app.services.github.client import GitHubClient
from app.services.github.errors import GitHubError


def create_github_client(settings: Settings, cache: ETagCache | None = None) -> GitHubClient:
    """Build a client from application settings (token comes from the environment only)."""
    return GitHubClient(
        token=settings.github_token,
        base_url=settings.github_api_url,
        timeout=settings.github_timeout_seconds,
        max_retries=settings.github_max_retries,
        max_rate_limit_wait=settings.github_max_rate_limit_wait_seconds,
        cache=cache,
    )


__all__ = ["GitHubClient", "GitHubError", "ETagCache", "InMemoryETagCache", "create_github_client"]
