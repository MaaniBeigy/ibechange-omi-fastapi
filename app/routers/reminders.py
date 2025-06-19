from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.reminder import Reminder
from app.schemas.reminder import ReminderList

router = APIRouter(prefix="/reminders", tags=["Reminders"])


@router.post("/", response_model=ReminderList)
def create_reminders(reminders: ReminderList, db: Session = Depends(get_db)):
    for user_id, reminder in reminders.reminders.items():
        existing_reminder = db.exec(
            select(Reminder).where(Reminder.user_id == user_id)
        ).first()
        if existing_reminder:
            continue
        db_reminder = Reminder(user_id=user_id, **reminder.model_dump())
        db.add(db_reminder)
    db.commit()
    return reminders
