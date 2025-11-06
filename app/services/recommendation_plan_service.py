import logging
import os
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import List

from dotenv import load_dotenv
from sqlalchemy import select
from sqlmodel import Session

from app.models.mission import Mission
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.utils.helpers import _aware_utc
from app.utils.scheduling import generate_random_schedule

load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def generate_recommendation_plan(
    user_id: uuid.UUID,
    content_missions: dict,
    plan_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
    db: Session,
) -> List[RecommendationPlan]:
    mode = os.getenv("MODE", "test")
    plans = []

    if mode != "test":
        return plans  # Production logic not implemented

    # Load constraints
    min_per_day = int(os.getenv("MIN_NOTIFICATIONS_PER_DAY", 1))
    max_per_day = int(os.getenv("MAX_NOTIFICATIONS_PER_DAY", 5))
    min_per_week = int(os.getenv("MIN_NOTIFICATIONS_PER_WEEK", 5))
    max_per_week = int(os.getenv("MAX_NOTIFICATIONS_PER_WEEK", 20))
    cooldown_minutes = int(os.getenv("BETWEEN_NOTIFICATION_COOLDOWN", 180))
    cooldown_delta = timedelta(minutes=cooldown_minutes)
    now = datetime.now(timezone.utc)
    start_time = _aware_utc(start_time)
    end_time = _aware_utc(end_time)
    if end_time <= now:
        return []
    if start_time < now:
        start_time = now
    # ----------------------------- Check cooldown window -----------------------------
    recent_exists = db.exec(
        select(RecommendationPlan)
        .where(RecommendationPlan.user_id == user_id)
        .where(RecommendationPlan.scheduled_for.between(now - cooldown_delta, now))
        .where(RecommendationPlan.is_deleted == False)
    ).first()
    if recent_exists:
        # Skip if the last recommendation is too recent
        return []

    # ----------------------- Fetch existing plans in the window ----------------------
    existing_for_plan = (
        db.exec(
            select(RecommendationPlan)
            .where(RecommendationPlan.user_id == user_id)
            .where(RecommendationPlan.plan_id == plan_id)
            .where(RecommendationPlan.is_deleted == False)
        )
        .scalars()
        .all()
    )
    # Already scheduled recommendation_ids for this plan → skip them
    already_rec_ids = {rp.recommendation_id for rp in existing_for_plan}
    # Count per-day across ALL existing so we respect max/day
    existing_by_day = Counter(
        _aware_utc(rp.scheduled_for).date() for rp in existing_for_plan
    )

    existing_in_window = (
        db.exec(
            select(RecommendationPlan.scheduled_for)
            .where(RecommendationPlan.user_id == user_id)
            .where(RecommendationPlan.scheduled_for >= start_time)
            .where(RecommendationPlan.scheduled_for <= end_time)
            .where(RecommendationPlan.is_deleted == False)
        )
        .scalars()
        .all()
    )
    scheduled_times = {_aware_utc(t) for t in existing_in_window}

    # How many are still needed in this window?
    total_existing_in_window = len(existing_in_window)
    total_days = (end_time.date() - start_time.date()).days + 1
    total_needed = max(min_per_week, min(max_per_week, max_per_day * total_days))
    remaining = max(0, total_needed - total_existing_in_window)
    if remaining == 0:
        return []

    # --------------------------- Generate fresh candidates ---------------------------
    candidates = [
        _aware_utc(ts) for ts in generate_random_schedule(start_time, end_time)
    ]

    new_schedules: List[datetime] = []
    for ts in candidates:
        ts = _aware_utc(ts)
        # cooldown against both existing_in_window and already-accepted candidates
        too_close = any(
            abs((ts - t).total_seconds()) < cooldown_delta.total_seconds()
            for t in scheduled_times
        )
        if too_close:
            continue
        day = ts.date()
        if existing_by_day.get(day, 0) < max_per_day:
            new_schedules.append(ts)
            scheduled_times.add(ts)
            existing_by_day[day] = existing_by_day.get(day, 0) + 1
        if len(new_schedules) >= remaining:
            break

    if not new_schedules:
        return []
    # -------------------------------- Assign schedules -------------------------------
    recommendation_lookup = {
        r.content_id: r.id
        for r in db.exec(
            select(Recommendation).where(Recommendation.is_deleted == False)
        )
        .scalars()
        .all()
    }
    # Filter contents to those not yet scheduled for this plan
    content_items = [
        (cid, mission_code)
        for cid, mission_code in content_missions.items()
        if recommendation_lookup.get(cid) not in already_rec_ids
    ]
    if not content_items:
        return []

    # Materialize mission UUIDs
    missions = (
        db.exec(select(Mission).where(Mission.is_deleted == False)).scalars().all()
    )
    mission_by_code = {m.mission_id: m.id for m in missions}

    plans: List[RecommendationPlan] = []
    for (content_id, mission_code), scheduled_time in zip(content_items, new_schedules):
        rec_uuid = recommendation_lookup.get(content_id)
        if rec_uuid is None:
            continue

        mission_uuid = mission_by_code.get(mission_code)
        rp = RecommendationPlan(
            user_id=user_id,
            plan_id=plan_id,
            recommendation_id=rec_uuid,
            mission_id=mission_uuid,
            scheduled_for=scheduled_time,
        )
        db.add(rp)
        plans.append(rp)

    return plans
