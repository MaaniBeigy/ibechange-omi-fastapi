"""Store normalized scheduled mission steps received from Artemis."""

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from sqlmodel import Session, select

from app.models.artemis import MissionScheduledStep
from app.models.user import User


def record_scheduled_step(
    db: Session,
    user_id: UUID,
    mission_process_id: int,
    mission_code: str,
    step_id: str,
    scheduled_for: datetime | None,
    start_date_raw: str | None = None,
    end_date_raw: str | None = None,
    source_message_id: str | None = None,
) -> MissionScheduledStep:
    """Insert or update one scheduled step within the caller's transaction."""
    if scheduled_for is not None and scheduled_for.tzinfo is None:
        raise ValueError("Scheduled task step time needs a timezone offset")
    if scheduled_for is None and (start_date_raw is None or end_date_raw is None):
        raise ValueError("Unresolved task step needs its source interval")
    if not mission_code or not step_id:
        raise ValueError("Mission code and step ID are required")
    user = db.get(User, user_id)
    if user is None:
        user = User(id=user_id)
        db.add(user)
        db.flush()
    step = db.exec(
        select(MissionScheduledStep).where(
            MissionScheduledStep.user_id == user_id,
            MissionScheduledStep.mission_process_id == mission_process_id,
            MissionScheduledStep.step_id == step_id,
        )
    ).first()
    if step is None:
        step = MissionScheduledStep(
            user_id=user_id,
            mission_process_id=mission_process_id,
            mission_code=mission_code,
            step_id=step_id,
            scheduled_for=(
                scheduled_for.astimezone(timezone.utc) if scheduled_for else None
            ),
            start_date_raw=start_date_raw,
            end_date_raw=end_date_raw,
            source_message_id=source_message_id,
        )
    elif step.mission_code != mission_code:
        raise ValueError("Scheduled step identity was reused for another mission")
    else:
        same_interval = (
            step.start_date_raw == start_date_raw and step.end_date_raw == end_date_raw
        )
        normalized = (
            scheduled_for.astimezone(timezone.utc)
            if scheduled_for
            else step.scheduled_for if same_interval else None
        )
        if (
            step.scheduled_for != normalized
            or step.start_date_raw != start_date_raw
            or step.end_date_raw != end_date_raw
            or not step.is_active
        ):
            step.scheduled_for = normalized
            step.start_date_raw = start_date_raw
            step.end_date_raw = end_date_raw
            step.source_message_id = source_message_id
            step.is_active = True
            step.updated_at = datetime.now(timezone.utc)
    db.add(step)
    db.flush()
    return step


def record_scheduled_snapshot(
    db: Session,
    user_id: UUID,
    mission_process_id: int,
    mission_code: str,
    windows: Iterable[tuple[str, str]],
    message_id: str,
) -> None:
    """Replace the active intervals for one mission process."""
    active_ids = set()
    for index, (start_date, end_date) in enumerate(windows, start=1):
        step_id = str(index)
        active_ids.add(step_id)
        record_scheduled_step(
            db,
            user_id,
            mission_process_id,
            mission_code,
            step_id,
            None,
            start_date_raw=start_date,
            end_date_raw=end_date,
            source_message_id=message_id,
        )
    previous = db.exec(
        select(MissionScheduledStep).where(
            MissionScheduledStep.user_id == user_id,
            MissionScheduledStep.mission_process_id == mission_process_id,
            MissionScheduledStep.mission_code == mission_code,
            MissionScheduledStep.is_active == True,
        )
    ).all()
    for step in previous:
        if step.step_id not in active_ids:
            step.is_active = False
            step.updated_at = datetime.now(timezone.utc)
            db.add(step)
    db.flush()
