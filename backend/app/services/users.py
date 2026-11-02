"""User lookup/creation. This is the seam where GitHub OAuth will plug in later."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User


def get_or_create_user(db: Session, github_user_id: int, username: str) -> User:
    """Return the user for a verified GitHub identity, creating it if needed."""
    user = db.scalar(select(User).where(User.github_user_id == github_user_id))
    if user is None:
        user = User(github_user_id=github_user_id, username=username)
        db.add(user)
    elif user.username != username:
        user.username = username
    db.commit()
    db.refresh(user)
    return user
