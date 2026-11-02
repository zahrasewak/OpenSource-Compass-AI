"""Profile routes: thin wrappers; identity always comes from the bearer token."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas.profile import ProfileIn, ProfileOut
from app.services import profile as service

router = APIRouter(prefix="/profile", tags=["profile"])


@router.post("", response_model=ProfileOut, status_code=201)
def create(data: ProfileIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return service.create_profile(db, user.id, data)


@router.get("", response_model=ProfileOut)
def read(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return service.get_profile(db, user.id)


@router.put("", response_model=ProfileOut)
def update(data: ProfileIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return service.update_profile(db, user.id, data)
