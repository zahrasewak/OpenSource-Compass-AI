"""Feedback + observed events. Records are raw and append-only; no training labels are derived here.

Bias warning for anyone using this data later: users mostly interact with recommendations that
already look good to them (position/presentation bias, selection bias), a save or a "relevant" is not
a successful contribution, and `attempted`/`contributed` are unverified self-reports.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Feedback, Issue, Repository, User
from app.schemas.feedback import CLASSIFICATION, VALUES, FeedbackOut, FeedbackRequest, value_name
from app.services.targets import resolve_stored_target

SELF_REPORT_NOTE = (
    "Recorded as a self-reported claim. It is not verified and is not treated as proof that you "
    "attempted or merged a contribution."
)


def add_event(db: Session, user_id: int, issue_id: int, feedback_type: str, value: int,
              recommendation_id: int | None = None) -> Feedback:
    """Add a record to the session (the caller commits, so it shares the caller's transaction)."""
    category, source = CLASSIFICATION[feedback_type]
    row = Feedback(user_id=user_id, issue_id=issue_id, feedback_type=feedback_type, feedback_value=value,
                   category=category, source=source, recommendation_id=recommendation_id)
    db.add(row)
    return row


def _latest(db: Session, user_id: int, issue_id: int, ftype: str, rec_id: int | None) -> Feedback | None:
    cond = Feedback.recommendation_id.is_(None) if rec_id is None else Feedback.recommendation_id == rec_id
    return db.scalar(
        select(Feedback)
        .where(Feedback.user_id == user_id, Feedback.issue_id == issue_id, Feedback.feedback_type == ftype, cond)
        .order_by(Feedback.created_at.desc(), Feedback.id.desc()).limit(1)
    )


def _out(row: Feedback, repo: Repository, issue: Issue, duplicate: bool) -> FeedbackOut:
    note = SELF_REPORT_NOTE if row.source == "self_reported" else None
    return FeedbackOut(
        id=row.id, type=row.feedback_type, value=value_name(row.feedback_type, row.feedback_value),
        category=row.category, source=row.source, match_id=row.recommendation_id, owner=repo.owner, repo=repo.name,
        number=issue.issue_number, created_at=row.created_at, duplicate=duplicate, note=note,
    )


def submit_feedback(db: Session, user: User, req: FeedbackRequest) -> tuple[FeedbackOut, bool]:
    """Returns (record, created). Re-submitting the user's latest identical answer stores nothing new."""
    ref = (req.owner, req.repo, req.number) if req.owner is not None else None
    repo, issue, rec = resolve_stored_target(db, user.id, match_id=req.match_id, ref=ref)  # type: ignore[arg-type]
    ftype = req.type.value
    stored = VALUES[ftype][req.value]  # type: ignore[index]
    rec_id = rec.id if rec else None
    latest = _latest(db, user.id, issue.id, ftype, rec_id)
    if latest is not None and latest.feedback_value == stored:
        return _out(latest, repo, issue, duplicate=True), False
    row = add_event(db, user.id, issue.id, ftype, stored, rec_id)
    db.commit()
    return _out(row, repo, issue, duplicate=False), True
