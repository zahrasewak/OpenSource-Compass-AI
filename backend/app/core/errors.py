"""Consistent error envelope and exception handlers. Never leaks stack traces."""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("compass.errors")


class AppError(Exception):
    """Domain error mapped to an HTTP status and a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        headers: dict[str, str] | None = None,
        details: object = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.headers = headers
        self.details = details


def _envelope(
    request: Request, code: str, message: str, status: int, details=None, headers: dict[str, str] | None = None
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    body = {"error": {"code": code, "message": message, "details": details, "request_id": request_id}}
    return JSONResponse(status_code=status, content=body, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    """Attach handlers so every failure uses the same JSON shape."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _envelope(request, exc.code, exc.message, exc.status_code, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return _envelope(request, "validation_error", "Request validation failed", 422, details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _envelope(request, f"http_{exc.status_code}", str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return _envelope(request, "internal_error", "Internal server error", 500)
