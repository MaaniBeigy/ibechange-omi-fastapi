import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.escalation_level import EscalationLevel
from app.models.user import HealthHabitAssessment, MissionContent, User, UserFeedback
from app.schemas.update import UpdateCreate

router = APIRouter(prefix="/updates", tags=["Updates"])


@router.post("/", response_model=UpdateCreate)
def create_updates(updates: UpdateCreate, db: Session = Depends(get_db)):
    # --- NEW: Collect all mission_ids and recommendation_ids from the payload ---
    mission_ids = set()
    recommendation_ids = set()

    # From user_feedback
    for feedback in updates.user_feedback.values():
        for event in feedback.events:
            props = event.properties
            if isinstance(props, dict):
                mission_id = props.get("mission_id")
                content_id = props.get("content_id")
                if mission_id:
                    mission_ids.add(mission_id)
                if content_id:
                    recommendation_ids.add(content_id)

    # From new_missions_and_contents
    for nmc in updates.new_missions_and_contents.values():
        for mission in nmc.new_missions:
            if hasattr(mission, "mission"):
                mission_ids.add(mission.mission)
            if hasattr(mission, "recommendations"):
                for rec_id in mission.recommendations:
                    recommendation_ids.add(rec_id)

    # --- insert missing Missions ---
    from app.models.mission import Mission

    for m_id in mission_ids:
        existing = db.exec(
            select(Mission).where(
                Mission.mission_id == m_id, Mission.is_deleted == False
            )
        ).first()
        if not existing:
            db_mission = Mission(mission_id=m_id)
            db.add(db_mission)

    # --- insert missing Recommendations ---
    from app.models.recommendation import Recommendation

    for r_id in recommendation_ids:
        # Find all missions this recommendation should be linked to
        linked_missions = set()
        # Check new_missions_and_contents for mission links
        for nmc in updates.new_missions_and_contents.values():
            for mission in nmc.new_missions:
                if hasattr(mission, "recommendations") and r_id in getattr(mission, "recommendations", []):
                    if hasattr(mission, "mission"):
                        linked_missions.add(mission.mission)
        # Check user_feedback for mission links
        for feedback in updates.user_feedback.values():
            for event in feedback.events:
                props = event.properties
                if isinstance(props, dict):
                    if props.get("content_id") == r_id and props.get("mission_id"):
                        linked_missions.add(props["mission_id"])
        existing = db.exec(
            select(Recommendation).where(
                Recommendation.content_id == r_id, Recommendation.is_deleted == False
            )
        ).first()
        if not existing:
            db_recommendation = Recommendation(
                content_id=r_id,
                content_type="Recommendation",
                missions=list(linked_missions),
                objective=[],
                hapa=[],
                comb=[],
                intervention_type=[],
            )
            db.add(db_recommendation)
        else:
            # Update missions field if new mission(s) not present
            updated = False
            for m_id in linked_missions:
                if m_id not in existing.missions:
                    existing.missions.append(m_id)
                    updated = True
            if updated:
                db.add(existing)

    # Handle new users
    for user_id, user_data in updates.new_users.items():
        existing_user = db.exec(select(User).where(User.user_id == user_id)).first()
        if existing_user:
            continue
        characteristics = user_data.characteristics
        db_user = User(user_id=uuid.UUID(user_id), characteristics=characteristics)
        db.add(db_user)

    # Handle user feedback
    for user_id, feedback in updates.user_feedback.items():
        db_feedback = UserFeedback(
            user_id=user_id,
            events=[json.loads(e.model_dump_json()) for e in feedback.events],
        )
        db.add(db_feedback)

    # Handle disabled users
    for user_id, disable_info in updates.disabled_users.items():
        user = db.exec(select(User).where(User.user_id == user_id)).first()
        if user:
            date_disabled = disable_info.get("date_disabled")
            if date_disabled:
                if isinstance(date_disabled, str):
                    date_disabled = datetime.fromisoformat(date_disabled)
                user.deleteUser(date_disabled)
                db.add(user)

    # Handle health habit assessments
    for user_id, hha in updates.health_habit_assessments.items():
        for assessment in hha:
            db_hha = HealthHabitAssessment(user_id=user_id, **assessment.model_dump())
            db.add(db_hha)

    # Handle new missions and contents
    for user_id, nmc in updates.new_missions_and_contents.items():
        for mission in nmc.new_missions:
            db_nmc = MissionContent(
                user_id=user_id,
                selection_timestamp=mission.selection_timestamp,
                mission_id=mission.mission,
                mission_start_time=mission.selection_timestamp,
                mission_end_time=mission.finish_timestamp,
            )
            db.add(db_nmc)

    # Handle escalation levels
    escalation_levels = getattr(updates, "escalation_level", {})
    for user_id, levels in escalation_levels.items():
        for level_info in levels:
            db_el = EscalationLevel(user_id=user_id, **level_info.model_dump())
            db.add(db_el)

    db.commit()
    return updates
