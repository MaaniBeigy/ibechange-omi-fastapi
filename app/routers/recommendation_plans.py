"""Return committed notification plans and decide the current queue epoch."""

from datetime import datetime, timedelta, timezone
from typing import Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.mission import Mission
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from app.models.user import User
from app.schemas.recommendation_plan import (
    NotificationItem,
    NotificationUserBucket,
    RecommendationPlanItem,
    RecommendationPlanList,
    RecommendationPlanResponse,
)
from app.services.policy_learning_service import close_episode, lock_user
from app.services.recommendation_plan_service import (
    current_queue,
    generate_recommendation_plan,
)
from app.services.rl_policy import InfeasibleQueueError, load_policy_artifacts
from app.utils.helpers import _aware_utc, _iso_to_utc_aware

router_recommendation_plans = APIRouter(
    prefix="/recommendation_plans", tags=["Recommendation Plans"]
)
router_notification_plans = APIRouter(
    prefix="/notification_plans", tags=["Notification Plans"]
)


def _window(start_time: str, end_time: str) -> tuple[datetime, datetime]:
    """Read the requested UTC interval."""
    try:
        start = _iso_to_utc_aware(start_time)
        end = _iso_to_utc_aware(end_time)
    except ValueError as error:
        raise HTTPException(
            status_code=422, detail="Invalid datetime format"
        ) from error
    if end < start:
        raise HTTPException(status_code=422, detail="end_time precedes start_time")
    return start, end


def _next_epoch(now: datetime) -> datetime:
    """Return the first decision grid instant that has not passed."""
    grid_min = int(load_policy_artifacts()["nonwearer"].metadata["grid_min"])
    grid_seconds = grid_min * 60
    stamp = now.timestamp()
    remainder = stamp % grid_seconds
    return now if remainder == 0 else now + timedelta(seconds=grid_seconds - remainder)


def _eligible_users(db: Session) -> list[UUID]:
    return db.exec(
        select(SelectedContent.user_id)
        .join(User, User.id == SelectedContent.user_id)
        .where(SelectedContent.is_deleted == False, User.is_deleted == False)
        .distinct()
        .order_by(SelectedContent.user_id)
    ).all()


def _decide_current_epoch(db: Session, epoch: datetime) -> None:
    """Read the eligible participant's day and revision after acquiring their lock."""
    for user_id in _eligible_users(db):
        try:
            lock_user(db, user_id)
            current = current_queue(db, user_id, epoch)
            if current:
                generate_recommendation_plan(current, epoch, db)
            close_episode(db, user_id, epoch)
            db.commit()
        except InfeasibleQueueError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception:
            db.rollback()
            raise


def _read_plans(db: Session, start: datetime, end: datetime) -> RecommendationPlanList:
    """Return active scheduled rows in the requested interval."""
    rows = db.exec(
        select(RecommendationPlan, Recommendation, Mission, SelectedContent)
        .join(Recommendation, Recommendation.id == RecommendationPlan.recommendation_id)
        .join(SelectedContent, SelectedContent.id == RecommendationPlan.plan_id)
        .join(User, User.id == RecommendationPlan.user_id)
        .join(Mission, Mission.id == RecommendationPlan.mission_id, isouter=True)
        .where(
            RecommendationPlan.scheduled_for >= start,
            RecommendationPlan.scheduled_for <= end,
            RecommendationPlan.is_deleted == False,
            SelectedContent.is_deleted == False,
            User.is_deleted == False,
        )
        .order_by(RecommendationPlan.scheduled_for, RecommendationPlan.occurrence_index)
    ).all()
    buckets: Dict[UUID, list[RecommendationPlanItem]] = {}
    for plan, recommendation, mission, selected in rows:
        selected_item = None
        if plan.occurrence_index is not None and plan.occurrence_index < len(
            selected.contents or []
        ):
            selected_item = selected.contents[plan.occurrence_index]
        mission_code = mission.mission_id if mission else None
        if mission_code is None and selected_item:
            mission_code = selected_item.get("mission_id")
        content_type = plan.content_type or (
            selected_item.get("type") if selected_item else recommendation.content_type
        )
        buckets.setdefault(plan.user_id, []).append(
            RecommendationPlanItem(
                content_id=recommendation.content_id,
                content_type=content_type,
                scheduled_for=_aware_utc(plan.scheduled_for),
                plan_id=selected.source_plan_id or plan.plan_id,
                created_at=_aware_utc(
                    selected.source_created_at or selected.created_at
                ),
                push_text_id=(
                    selected_item.get("push_text_id") if selected_item else None
                ),
                mission_id=mission_code or "UNKNOWN",
                mission_start_time=(
                    _aware_utc(selected.mission_start_time)
                    if selected.mission_start_time
                    else None
                ),
                mission_end_time=(
                    _aware_utc(selected.mission_end_time)
                    if selected.mission_end_time
                    else None
                ),
                occurrence_index=plan.occurrence_index,
                mission_process_id=plan.mission_process_id,
                deadline=_aware_utc(plan.deadline) if plan.deadline else None,
                policy_version=plan.policy_version,
            )
        )
    return RecommendationPlanList(
        recommendation_plans=[
            RecommendationPlanResponse(user_id=user_id, plans=items)
            for user_id, items in buckets.items()
        ]
    )


@router_recommendation_plans.get("/", response_model=RecommendationPlanList)
def get_recommendation_plans(
    start_time: str = Query(...),
    end_time: str = Query(...),
    db: Session = Depends(get_db),
):
    """Decide the next epoch if it is in the requested interval."""
    start, end = _window(start_time, end_time)
    epoch = _next_epoch(datetime.now(timezone.utc))
    if start <= epoch <= end:
        _decide_current_epoch(db, epoch)
    return _read_plans(db, start, end)


@router_notification_plans.get("/", response_model=Dict[UUID, NotificationUserBucket])
def get_notification_plans(
    start_time: str = Query(...),
    end_time: str = Query(...),
    db: Session = Depends(get_db),
):
    """Return committed sends in the delivery API response shape."""
    plans = get_recommendation_plans(start_time, end_time, db)
    output = {}
    for bucket in plans.recommendation_plans:
        items = []
        for plan in bucket.plans:
            if plan.mission_id == "UNKNOWN":
                raise HTTPException(status_code=500, detail="Missing mission_id")
            if plan.content_type is None:
                raise HTTPException(status_code=500, detail="Missing content_type")
            items.append(
                NotificationItem(
                    plan_id=plan.plan_id,
                    id=plan.content_id,
                    send_timestamp=plan.scheduled_for.replace(tzinfo=None),
                    type=plan.content_type,
                    mission=plan.mission_id,
                    mission_process_id=plan.mission_process_id,
                    push_text_id=plan.push_text_id,
                    selection_timestamp=(
                        plan.mission_start_time.replace(tzinfo=None)
                        if plan.mission_start_time
                        else None
                    ),
                )
            )
        output[bucket.user_id] = NotificationUserBucket(
            contents=items,
            created_at=max(
                (p.created_at for p in bucket.plans if p.created_at), default=None
            ),
        )
    return output
