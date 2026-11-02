"""Feedback routes (scoped to the authenticated user)."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas.feedback import FeedbackOut, FeedbackRequest
from app.services import feedback as service

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", response_model=FeedbackOut)
def submit(
    body: FeedbackRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """201 when stored; 200 with `duplicate: true` if it repeats your latest answer for the same target."""
    out, created = service.submit_feedback(db, user, body)
    response.status_code = 201 if created else 200
    return out
