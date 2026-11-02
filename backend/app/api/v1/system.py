"""Liveness and readiness probes."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    """Liveness: the process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/ready")
def ready(response: Response, db: Session = Depends(get_db)) -> dict:
    """Readiness: the database must answer. Redis is checked once it is used (Stage 3+)."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {"status": "unavailable", "checks": {"database": "down"}}
    return {"status": "ready", "checks": {"database": "up"}}
