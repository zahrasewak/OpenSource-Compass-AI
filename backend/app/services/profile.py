"""Profile business logic. Every function takes the authenticated user's id from the server side."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import UserPreference, UserSkill
from app.schemas.profile import ProfileIn, ProfileOut, SkillOut


def _prefs(db: Session, user_id: int) -> UserPreference | None:
    return db.scalar(select(UserPreference).where(UserPreference.user_id == user_id))


def _apply(prefs: UserPreference, data: ProfileIn) -> None:
    prefs.interests = list(data.interests)
    prefs.contribution_goals = list(data.contribution_goals)
    prefs.preferred_task_types = [t.value for t in data.preferred_task_types]
    prefs.preferred_difficulty = data.preferred_difficulty
    prefs.experience_level = data.experience_level.value if data.experience_level else None


def _sync_skills(db: Session, user_id: int, data: ProfileIn) -> None:
    """Make stored skills equal the request: update, add and remove, keeping created_at of kept rows."""
    existing = {s.skill_name: s for s in db.scalars(select(UserSkill).where(UserSkill.user_id == user_id))}
    wanted = {s.name: s for s in data.skills}
    for name, row in existing.items():
        if name not in wanted:
            db.delete(row)
    for name, s in wanted.items():
        row = existing.get(name)
        if row is None:
            db.add(UserSkill(user_id=user_id, skill_name=name, proficiency=s.proficiency,
                             category=s.category.value, source="manual"))
        else:
            row.proficiency = s.proficiency
            row.category = s.category.value


def get_profile(db: Session, user_id: int) -> ProfileOut:
    prefs = _prefs(db, user_id)
    if prefs is None:
        raise AppError("profile_not_found", "No profile exists for this user", 404)
    skills = db.scalars(
        select(UserSkill).where(UserSkill.user_id == user_id).order_by(UserSkill.skill_name)
    ).all()
    return ProfileOut(
        skills=[SkillOut(name=s.skill_name, category=s.category, proficiency=s.proficiency, source=s.source)
                for s in skills],
        interests=prefs.interests or [],
        contribution_goals=prefs.contribution_goals or [],
        preferred_task_types=prefs.preferred_task_types or [],
        preferred_difficulty=prefs.preferred_difficulty,
        experience_level=prefs.experience_level,
        updated_at=prefs.updated_at,
    )


def create_profile(db: Session, user_id: int, data: ProfileIn) -> ProfileOut:
    if _prefs(db, user_id) is not None:
        raise AppError("profile_exists", "Profile already exists; use PUT to update it", 409)
    prefs = UserPreference(user_id=user_id)
    _apply(prefs, data)
    db.add(prefs)
    try:
        _sync_skills(db, user_id, data)
        db.commit()
    except IntegrityError:  # concurrent POST won the unique(user_id) race
        db.rollback()
        raise AppError("profile_exists", "Profile already exists; use PUT to update it", 409)
    return get_profile(db, user_id)


def update_profile(db: Session, user_id: int, data: ProfileIn) -> ProfileOut:
    prefs = _prefs(db, user_id)
    if prefs is None:
        raise AppError("profile_not_found", "No profile exists for this user; use POST to create it", 404)
    _apply(prefs, data)
    _sync_skills(db, user_id, data)
    db.commit()
    return get_profile(db, user_id)
