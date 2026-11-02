"""Exceptions raised by the GitHub client.

Messages describe the failure only; they never include request headers or credentials.
"""


class GitHubError(Exception):
    """Base class for every GitHub client failure."""


class GitHubAuthError(GitHubError):
    """401: the configured token is missing, invalid or expired."""


class GitHubPermissionError(GitHubError):
    """403 that is not a rate limit (forbidden, blocked, SSO required...)."""


class GitHubNotFoundError(GitHubError):
    """404/410: resource does not exist, is private, or issues are disabled."""


class GitHubRequestError(GitHubError):
    """Other 4xx: the request itself was rejected. Not retried."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubRateLimitError(GitHubError):
    """Rate limit hit and the required wait exceeds what the client is willing to block for."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GitHubServerError(GitHubError):
    """5xx persisted after all permitted retries."""


class GitHubTimeoutError(GitHubError):
    """Request timed out on every permitted attempt."""


class GitHubDeadlineError(GitHubTimeoutError):
    """The request's overall time budget ran out (see app/core/requestctx.py); reported like any other GitHub timeout."""


class GitHubConnectionError(GitHubError):
    """Network-level failure on every permitted attempt."""


class GitHubResponseError(GitHubError):
    """Response was not valid JSON, had an unexpected shape, or failed validation."""
