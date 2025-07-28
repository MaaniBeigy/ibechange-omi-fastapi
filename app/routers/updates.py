import json
import logging
from datetime import datetime, timezone
from typing import Dict, Set
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.escalation_level import EscalationLevel
from app.models.mission import Mission
from app.models.recommendation import Recommendation
from app.models.user import HealthHabitAssessment, MissionContent, User, UserFeedback
from app.schemas.update import UpdateCreate

router = APIRouter(prefix="/updates", tags=["Updates"])

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


@router.post("/", response_model=UpdateCreate)
def create_updates(updates: UpdateCreate, db: Session = Depends(get_db)):
    with db.no_autoflush:
        # Collect all user_ids from all sections of the payload
        user_ids: Set[str] = set()
        user_ids.update(updates.new_users.keys())
        user_ids.update(updates.disabled_users.keys())
        user_ids.update(updates.user_feedback.keys())
        user_ids.update(updates.health_habit_assessments.keys())
        user_ids.update(updates.new_missions_and_contents.keys())
        user_ids.update(getattr(updates, "escalation_level", {}).keys())

        # Create missing users
        for user_id in user_ids:
            existing_user = db.exec(select(User).where(User.id == user_id)).first()
            if not existing_user:
                logger.debug(f"Creating new user with user_id: {user_id}")
                # Access characteristics directly from NewUser object or use empty dict
                new_user = updates.new_users.get(user_id)
                characteristics = new_user.model_dump() if new_user else {}
                db_user = User(
                    id=UUID(user_id),
                    characteristics=characteristics,
                    created_at=datetime.now(timezone.utc),
                    is_deleted=False,
                )
                db.add(db_user)
        db.flush()
        logger.debug("All users created and flushed")

        # Collect mission_ids and recommendation_ids
        mission_ids = set()
        recommendation_ids = set()
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
        for nmc in updates.new_missions_and_contents.values():
            for mission in nmc.new_missions:
                if hasattr(mission, "mission"):
                    mission_ids.add(mission.mission)
                if hasattr(mission, "recommendations"):
                    for rec_id in mission.recommendations:
                        recommendation_ids.add(rec_id)

        # Insert missing Missions
        for m_id in mission_ids:
            existing = db.exec(
                select(Mission).where(
                    Mission.mission_id == m_id, Mission.is_deleted == False
                )
            ).first()
            if not existing:
                db_mission = Mission(
                    mission_id=m_id,
                    created_at=datetime.now(timezone.utc),
                    is_deleted=False,
                )
                db.add(db_mission)
                logger.debug(f"Created new Mission with mission_id: {m_id}")

        # Insert missing Recommendations
        for r_id in recommendation_ids:
            linked_missions = set()
            for nmc in updates.new_missions_and_contents.values():
                for mission in nmc.new_missions:
                    if hasattr(mission, "recommendations") and r_id in getattr(
                        mission, "recommendations", []
                    ):
                        if hasattr(mission, "mission"):
                            linked_missions.add(mission.mission)
            for feedback in updates.user_feedback.values():
                for event in feedback.events:
                    props = event.properties
                    if isinstance(props, dict):
                        if props.get("content_id") == r_id and props.get("mission_id"):
                            linked_missions.add(props["mission_id"])
            existing = db.exec(
                select(Recommendation).where(
                    Recommendation.content_id == r_id,
                    Recommendation.is_deleted == False,
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
                    created_at=datetime.now(timezone.utc),
                    is_deleted=False,
                )
                db.add(db_recommendation)
                logger.debug(f"Created new Recommendation for content_id: {r_id}")
            else:
                updated = False
                for m_id in linked_missions:
                    if m_id not in existing.missions:
                        existing.missions.append(m_id)
                        updated = True
                if updated:
                    db.add(existing)
                    logger.debug(
                        f"Updated Recommendation missions for content_id: {r_id}"
                    )

        # Handle disabled users
        for user_id, disable_info in updates.disabled_users.items():
            user = db.exec(select(User).where(User.id == user_id)).first()
            if user:
                date_disabled = disable_info.get("date_disabled")
                if date_disabled:
                    if isinstance(date_disabled, str):
                        date_disabled = datetime.fromisoformat(date_disabled)
                    user.deleteUser(date_disabled)
                    db.add(user)
                    logger.debug(f"Disabled user {user_id}")

        # Handle user feedback
        for user_id, feedback in updates.user_feedback.items():
            db_feedback = UserFeedback(
                user_id=UUID(user_id),
                events=[json.loads(e.model_dump_json()) for e in feedback.events],
                created_at=datetime.now(timezone.utc),
                is_deleted=False,
            )
            db.add(db_feedback)
            logger.debug(f"Added UserFeedback for user {user_id}")

        # Handle health habit assessments
        for user_id, hha in updates.health_habit_assessments.items():
            for assessment in hha:
                db_hha = HealthHabitAssessment(
                    user_id=UUID(user_id),
                    hhs=assessment.hhs,
                    assessment_timestamp=assessment.assessment_timestamp,
                    created_at=datetime.now(timezone.utc),
                    is_deleted=False,
                )
                db.add(db_hha)
                logger.debug(f"Added HealthHabitAssessment for user {user_id}")

        # Handle new missions and contents
        for user_id, nmc in updates.new_missions_and_contents.items():
            for mission in nmc.new_missions:
                existing_mc = db.exec(
                    select(MissionContent).where(
                        MissionContent.user_id == UUID(user_id),
                        MissionContent.mission_id == mission.mission,
                        MissionContent.is_deleted == False,
                    )
                ).first()

                if existing_mc:
                    # Only update if timestamps have changed
                    if (
                        existing_mc.mission_start_time != mission.selection_timestamp
                        or existing_mc.mission_end_time != mission.finish_timestamp
                    ):
                        existing_mc.selection_timestamp = mission.selection_timestamp
                        existing_mc.mission_start_time = mission.selection_timestamp
                        existing_mc.mission_end_time = mission.finish_timestamp
                        existing_mc.modified_at = datetime.now(timezone.utc)
                        db.add(existing_mc)
                        logger.debug(
                            f"Updated MissionContent for user {user_id}, mission {mission.mission}"
                        )
                    else:
                        logger.debug(
                            f"Skipped duplicate MissionContent for user {user_id}, mission {mission.mission}"
                        )
                else:
                    # New mission content
                    db_nmc = MissionContent(
                        user_id=UUID(user_id),
                        selection_timestamp=mission.selection_timestamp,
                        mission_id=mission.mission,
                        mission_start_time=mission.selection_timestamp,
                        mission_end_time=mission.finish_timestamp,
                        created_at=datetime.now(timezone.utc),
                        is_deleted=False,
                    )
                    db.add(db_nmc)
                    logger.debug(
                        f"Created new MissionContent for user {user_id}, mission {mission.mission}"
                    )

        # Handle escalation levels
        escalation_levels = getattr(updates, "escalation_level", {})
        for user_id, levels in escalation_levels.items():
            for level_info in levels:
                db_el = EscalationLevel(
                    user_id=UUID(user_id),
                    update_timestamp=level_info.update_timestamp,
                    level=level_info.level,
                    pillar_id=level_info.pillar_id,
                    created_at=datetime.now(timezone.utc),
                    is_deleted=False,
                )
                db.add(db_el)
                logger.debug(f"Added EscalationLevel for user {user_id}")

        db.commit()
        logger.debug("Committed all changes")
    return updates
