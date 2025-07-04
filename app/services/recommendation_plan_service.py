import os
import uuid
from datetime import datetime
from typing import List

from dotenv import load_dotenv
from sqlmodel import Session

from app.models.recommendation_plan import RecommendationPlan
from app.utils.scheduling import generate_random_schedule

load_dotenv()


def generate_recommendation_plan(
    user_id: uuid.UUID,
    contents: List[str],
    start_time: datetime,
    end_time: datetime,
    db: Session,
) -> List[RecommendationPlan]:
    mode = os.getenv("MODE", "test")
    plans = []

    if mode == "test":
        schedules = generate_random_schedule(start_time, end_time)
        for content_id, scheduled_time in zip(contents, schedules):
            plan = RecommendationPlan(
                user_id=user_id, content_id=content_id, scheduled_for=scheduled_time
            )
            db.add(plan)
            plans.append(plan)
    else:
        # TODO: Implement production mode scheduling logic
        pass

    return plans
