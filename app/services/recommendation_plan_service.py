import os
import uuid
from datetime import datetime
from typing import List

from dotenv import load_dotenv
from sqlmodel import Session
from sqlalchemy import select

from app.models.recommendation_plan import RecommendationPlan
from app.utils.scheduling import generate_random_schedule
from app.models.mission import Mission

load_dotenv()


def generate_recommendation_plan(
    user_id: uuid.UUID,
    content_missions: dict,
    start_time: datetime,
    end_time: datetime,
    db: Session,
) -> List[RecommendationPlan]:
    mode = os.getenv("MODE", "test")
    plans = []

    if mode == "test":
        schedules = generate_random_schedule(start_time, end_time)
        for (content_id, mission_id_str), scheduled_time in zip(
            content_missions.items(), schedules
        ):
            # Look up Mission by mission_id (string)
            mission_obj = db.exec(
                select(Mission).where(
                    Mission.mission_id == mission_id_str, Mission.is_deleted == False
                )
            ).scalars().first()
            mission_uuid = mission_obj.id if mission_obj else None
            plan = RecommendationPlan(
                user_id=user_id,
                content_id=content_id,
                mission_id=mission_uuid,
                scheduled_for=scheduled_time,
            )
            db.add(plan)
            plans.append(plan)
    else:
        # TODO: Implement production mode scheduling logic
        pass

    return plans
