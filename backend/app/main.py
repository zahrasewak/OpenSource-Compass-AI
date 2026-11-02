"""Application entry point: builds the FastAPI app and wires routers."""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import auth, feedback, issues, matches, profile, repositories, saved_issues, system
from app.api.github_errors import register_github_error_handler
from app.core.config import get_settings
from app.core.errors import _envelope, register_error_handlers
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import AuthRateLimiter, DatabaseAuthRateLimiter
from app.core.requestctx import RequestCancelled
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    """App factory (keeps tests and workers free of import-time side effects)."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="OpenSource Compass AI", version="0.1.0")
    # One limiter per app instance (per process). See app/core/ratelimit.py for what that does and does not cover.
    limits = dict(enabled=settings.auth_rate_limit_enabled, attempts_per_minute=settings.auth_attempts_per_minute,
                  max_failures=settings.auth_max_failures, failure_window_seconds=settings.auth_failure_window_seconds,
                  global_attempts_per_minute=settings.auth_global_attempts_per_minute)
    if settings.auth_rate_limit_backend == "database":
        from app.db.session import get_sessionmaker

        app.state.auth_limiter = DatabaseAuthRateLimiter(lambda: get_sessionmaker()(), **limits)  # engine is created on first use
    else:
        app.state.auth_limiter = AuthRateLimiter(**limits)
        if settings.environment == "production" and settings.auth_rate_limit_enabled:
            logging.getLogger("compass.auth.ratelimit").warning(
                "auth rate limiting uses the per-process memory backend; with several workers or instances the effective limits "
                "are multiplied. Set COMPASS_AUTH_RATE_LIMIT_BACKEND=database (docs/AUTHENTICATION.md section 7).")

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    # Outermost middleware: gives each request a cancellation flag (client disconnect) and a time budget.
    app.add_middleware(
        RequestContextMiddleware, enabled=settings.request_cancellation_enabled,
        deadline_seconds=settings.request_deadline_seconds, slow_deadline_seconds=settings.slow_request_deadline_seconds)

    @app.exception_handler(RequestCancelled)
    async def request_cancelled(request: Request, exc: RequestCancelled):
        # The client is already gone; nobody reads this. 499 is the conventional "client closed request" status.
        logging.getLogger("compass.requests").info("request cancelled: client disconnected (%s %s)", request.method, request.url.path)
        return _envelope(request, "client_closed_request", "The client closed the request", 499)

    register_error_handlers(app)
    register_github_error_handler(app)
    app.include_router(system.router)
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(profile.router, prefix="/api/v1")
    app.include_router(repositories.router, prefix="/api/v1")
    app.include_router(issues.router, prefix="/api/v1")
    app.include_router(matches.router, prefix="/api/v1")
    app.include_router(saved_issues.router, prefix="/api/v1")
    app.include_router(feedback.router, prefix="/api/v1")
    return app


app = create_app()
