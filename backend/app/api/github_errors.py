"""Translate GitHub client failures into the API's consistent error envelope.

Messages are fixed strings: upstream response bodies, headers and URLs are never forwarded.
"""

import logging
import math

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import AppError, _envelope
from app.services.github.errors import (
    GitHubAuthError,
    GitHubConnectionError,
    GitHubError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GitHubRequestError,
    GitHubResponseError,
    GitHubServerError,
    GitHubTimeoutError,
)

logger = logging.getLogger("compass.github.api")


def to_app_error(exc: GitHubError) -> AppError:
    """Map a GitHub failure to an AppError (status, stable code, safe message)."""
    if isinstance(exc, GitHubRateLimitError):
        seconds = exc.retry_after_seconds
        headers = {"Retry-After": str(max(1, math.ceil(seconds)))} if seconds is not None else None
        details = {"retry_after_seconds": math.ceil(seconds)} if seconds is not None else None
        return AppError("github_rate_limited", "GitHub rate limit reached; try again later", 429,
                        headers=headers, details=details)
    if isinstance(exc, GitHubNotFoundError):
        return AppError("github_not_found", "Resource not found on GitHub, or it is not public", 404)
    if isinstance(exc, GitHubPermissionError):
        return AppError("github_access_denied", "GitHub denied access to this resource", 403)
    if isinstance(exc, GitHubAuthError):
        # Our server-side credential is bad: an operator problem, not something the caller can fix.
        return AppError("github_auth_failed", "The server could not authenticate to GitHub", 502)
    if isinstance(exc, GitHubTimeoutError):
        return AppError("github_timeout", "GitHub did not respond in time", 504)
    if isinstance(exc, (GitHubConnectionError, GitHubServerError)):
        return AppError("github_unavailable", "GitHub is temporarily unavailable", 502)
    if isinstance(exc, GitHubResponseError):
        return AppError("github_bad_response", "GitHub returned an unexpected response", 502)
    if isinstance(exc, GitHubRequestError):
        return AppError("github_request_rejected", "GitHub rejected the request", 502)
    return AppError("github_error", "Unexpected error communicating with GitHub", 502)


def register_github_error_handler(app: FastAPI) -> None:
    @app.exception_handler(GitHubError)
    async def _github(request: Request, exc: GitHubError) -> JSONResponse:
        err = to_app_error(exc)
        logger.warning("github upstream failure: %s -> %s", type(exc).__name__, err.code)
        return _envelope(request, err.code, err.message, err.status_code, err.details, err.headers)
