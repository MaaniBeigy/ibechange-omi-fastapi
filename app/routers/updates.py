from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.update import HealthHabitAssessment, NewMissionContent, UserFeedback
from app.models.user import User
from app.schemas.update import UpdateCreate

router = APIRouter(prefix="/updates", tags=["Updates"])


@router.post("/", response_model=UpdateCreate)
def create_updates(updates: UpdateCreate, db: Session = Depends(get_db)):
    # Handle new users
    for user_id, user_data in updates.new_users.items():
        existing_user = db.exec(select(User).where(User.user_id == user_id)).first()
        if existing_user:
            continue
        db_user = User(user_id=user_id, **user_data.dict(exclude_unset=True))
        db.add(db_user)

    # Handle user feedback
    for user_id, feedback in updates.user_feedback.items():
        db_feedback = UserFeedback(user_id=user_id, events=feedback.events)
        db.add(db_feedback)

    # Handle disabled users
    for user_id in updates.disabled_users:
        user = db.exec(select(User).where(User.user_id == user_id)).first()
        if user:
            user.is_deleted = True
            user.deleted_at = datetime.now(timezone.utc)
            db.add(user)

    # Handle health habit assessments
    for user_id, hha in updates.health_habit_assessments.items():
        db_hha = HealthHabitAssessment(user_id=user_id, **hha.model_dump())
        db.add(db_hha)

    # Handle new missions and contents
    for user_id, nmc in updates.new_missions_and_contents.items():
        db_nmc = NewMissionContent(user_id=user_id, **nmc.model_dump())
        db.add(db_nmc)

    db.commit()
    return updates
