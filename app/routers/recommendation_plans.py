"""Return committed notification plans and decide the current queue epoch."""

from datetime import datetime, timedelta, timezone
from typing import Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
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
from app.services.recommendation_plan_service import generate_recommendation_plan
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
    if now.minute % grid_min == 0 and now.second == 0 and now.microsecond == 0:
        return now
    minute = (now.minute // grid_min + 1) * grid_min
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)


def _latest_selections(db: Session) -> list[SelectedContent]:
    """Return the current ordered queue for every eligible user."""
    rows = db.exec(
        select(SelectedContent)
        .join(User, User.id == SelectedContent.user_id)
        .where(SelectedContent.is_deleted == False, User.is_deleted == False)
    ).all()
    latest: dict[UUID, SelectedContent] = {}
    for row in rows:
        current = latest.get(row.user_id)
        if current is None or _aware_utc(
            row.modified_at or row.created_at
        ) > _aware_utc(current.modified_at or current.created_at):
            latest[row.user_id] = row
    return list(latest.values())


def _decide_current_epoch(db: Session, epoch: datetime) -> None:
    """Commit at most one policy action per current queue and user."""
    for selected in _latest_selections(db):
        try:
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": str(selected.user_id)},
            )
            current = db.exec(
                select(SelectedContent)
                .where(SelectedContent.id == selected.id)
                .with_for_update()
            ).one()
            generate_recommendation_plan(current, epoch, db)
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
                plan_id=plan.plan_id,
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
                    id=plan.content_id,
                    send_timestamp=plan.scheduled_for.replace(tzinfo=None),
                    type=plan.content_type,
                    mission=plan.mission_id,
                    selection_timestamp=(
                        plan.mission_start_time.replace(tzinfo=None)
                        if plan.mission_start_time
                        else None
                    ),
                )
            )
        output[bucket.user_id] = NotificationUserBucket(contents=items)
    return output
