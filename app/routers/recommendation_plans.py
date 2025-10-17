from datetime import datetime, timezone
from typing import Dict, List, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, join, select

from app.dependencies import get_db
from app.models.mission import Mission
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from app.schemas.recommendation_plan import (
    NotificationItem,
    NotificationUserBucket,
    RecommendationPlanItem,
    RecommendationPlanList,
    RecommendationPlanResponse,
)
from app.services.recommendation_plan_service import generate_recommendation_plan
from app.utils.helpers import _iso_to_utc_aware, _to_utc, _aware_utc

router_recommendation_plans = APIRouter(
    prefix="/recommendation_plans", tags=["Recommendation Plans"]
)


@router_recommendation_plans.get("/", response_model=RecommendationPlanList)
def get_recommendation_plans(
    start_time: str = Query(...),
    end_time: str = Query(...),
    db: Session = Depends(get_db),
):
    # Parse the input start and end times from ISO format
    try:
        start_dt = _iso_to_utc_aware(start_time)
        end_dt = _iso_to_utc_aware(end_time)
    except ValueError:
        # Return 422 if the datetime format is invalid
        raise HTTPException(status_code=422, detail="Invalid datetime format")

    now = datetime.now(timezone.utc)

    # Query for existing recommendation plans in the given time range
    stmt = (
        select(RecommendationPlan, Recommendation, Mission, SelectedContent)
        .join(Recommendation, Recommendation.id == RecommendationPlan.recommendation_id)
        .join(SelectedContent, SelectedContent.id == RecommendationPlan.plan_id)
        .join(
            Mission, isouter=True, onclause=Mission.id == RecommendationPlan.mission_id
        )
        .where(RecommendationPlan.scheduled_for >= start_dt)
        .where(RecommendationPlan.scheduled_for <= end_dt)
        .where(RecommendationPlan.is_deleted == False)
        .order_by(
            RecommendationPlan.scheduled_for.asc(), Recommendation.content_id.asc()
        )
    )
    results = db.exec(stmt).all()

    if results:
        # If plans exist, group them by user_id and format the response
        user_plans = {}
        for plan, rec, mission_row, selected in results:
            mission_code = mission_row.mission_id if mission_row else None
            if mission_code is None:
                for c in selected.contents or []:
                    if c.get("id") == rec.content_id:
                        mission_code = c.get("mission_id")
                        if mission_code:
                            break
            if not mission_code:
                mission_code = "UNKNOWN"
            if plan.user_id not in user_plans:
                user_plans[plan.user_id] = []
            if plan.user_id not in user_plans:
                user_plans[plan.user_id] = []
            user_plans[plan.user_id].append(
                RecommendationPlanItem(
                    content_id=rec.content_id,
                    scheduled_for=plan.scheduled_for.astimezone(timezone.utc),
                    plan_id=plan.plan_id,
                    mission_id=mission_code,
                    mission_start_time=(
                        selected.mission_start_time.astimezone(timezone.utc)
                        if selected.mission_start_time
                        else None
                    ),
                    mission_end_time=(
                        selected.mission_end_time.astimezone(timezone.utc)
                        if selected.mission_end_time
                        else None
                    ),
                )
            )
        # Build the response as a list of RecommendationPlanResponse objects
        response = [
            RecommendationPlanResponse(user_id=user_id, plans=items)
            for user_id, items in user_plans.items()
        ]
        return RecommendationPlanList(recommendation_plans=response)

    if not results:
        # If asked for a past window, do not generate new plans
        if end_dt < now:
            return RecommendationPlanList(recommendation_plans=[])
    # --------------------- If no plans exist, generate new plans ---------------------
    # Fetch all non-deleted SelectedContents whose mission window includes current time
    selected_contents = db.exec(
        select(SelectedContent).where(
            SelectedContent.is_deleted == False,
            (
                (SelectedContent.mission_start_time == None)
                | (SelectedContent.mission_start_time <= now)
            ),
            (
                (SelectedContent.mission_end_time == None)
                | (SelectedContent.mission_end_time >= now)
            ),
        )
    ).all()

    new_plans = []
    for content in selected_contents:
        # Extract content_id -> mission_id mapping from list of dicts in SelectedContent
        content_missions = {c["id"]: c["mission_id"] for c in content.contents}
        if not content_missions:
            continue

        # -------------------- Normalize DB datetimes to UTC-aware --------------------
        mst = content.mission_start_time
        if mst is not None:
            mst = (
                mst.replace(tzinfo=timezone.utc)
                if mst.tzinfo is None
                else mst.astimezone(timezone.utc)
            )

        met = content.mission_end_time
        if met is not None:
            met = (
                met.replace(tzinfo=timezone.utc)
                if met.tzinfo is None
                else met.astimezone(timezone.utc)
            )
        # ----------------------- Clip to mission window and now ----------------------
        window_start = max(
            start_dt,
            now,
            mst or start_dt,
        )
        window_end = min(
            end_dt,
            met or end_dt,
        )
        if window_end <= window_start:
            continue
        # Generate recommendation plans for each user/content
        new_plans += generate_recommendation_plan(
            content.user_id, content_missions, content.id, window_start, window_end, db
        )

    # Commit all new plans to the database
    db.commit()

    # Format the response for the newly created plans
    user_plans = {}
    recommendation_ids = [p.recommendation_id for p in new_plans]
    recommendation_map = {
        r.id: r.content_id
        for r in db.exec(
            select(Recommendation).where(Recommendation.id.in_(recommendation_ids))
        ).all()
    }
    mission_ids = [p.mission_id for p in new_plans if p.mission_id is not None]
    mission_code_map = {
        m.id: m.mission_id
        for m in (
            db.exec(select(Mission).where(Mission.id.in_(mission_ids))).all()
            if mission_ids
            else []
        )
    }
    plan_ids = list({p.plan_id for p in new_plans})
    selected_rows = db.exec(
        select(SelectedContent).where(SelectedContent.id.in_(plan_ids))
    ).all()
    selected_times_map = {
        s.id: (s.mission_start_time, s.mission_end_time) for s in selected_rows
    }
    selected_contents_map = {s.id: (s.contents or []) for s in selected_rows}
    for plan in new_plans:
        if plan.user_id not in user_plans:
            user_plans[plan.user_id] = []
        content_id = recommendation_map.get(plan.recommendation_id)
        mission_code = mission_code_map.get(plan.mission_id)
        if not mission_code and content_id:
            for c in selected_contents_map.get(plan.plan_id, []):
                if c.get("id") == content_id:
                    mission_code = c.get("mission_id")
                    if mission_code:
                        break
        if not mission_code:
            mission_code = "UNKNOWN"
        mst, met = selected_times_map.get(plan.plan_id, (None, None))
        mst_utc = _to_utc(mst)
        met_utc = _to_utc(met)
        user_plans[plan.user_id].append(
            RecommendationPlanItem(
                content_id=content_id,
                scheduled_for=(
                    plan.scheduled_for.astimezone(timezone.utc)
                    if plan.scheduled_for.tzinfo is not None
                    else plan.scheduled_for.replace(tzinfo=timezone.utc)
                ),
                plan_id=plan.plan_id,
                mission_id=mission_code,
                mission_start_time=mst_utc,
                mission_end_time=met_utc,
            )
        )
    response = [
        RecommendationPlanResponse(user_id=user_id, plans=items)
        for user_id, items in user_plans.items()
    ]
    return RecommendationPlanList(recommendation_plans=response)


router_notification_plans = APIRouter(
    prefix="/notification_plans", tags=["Notification Plans"]
)


@router_notification_plans.get("/", response_model=Dict[UUID, NotificationUserBucket])
def get_notification_plans(
    start_time: str = Query(...),
    end_time: str = Query(...),
    db: Session = Depends(get_db),
):
    # Parse datetimes
    try:
        start_dt = _iso_to_utc_aware(start_time)
        end_dt = _iso_to_utc_aware(end_time)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid datetime format")

    # Reuse recommendation_plans logic
    rec_plan_list: RecommendationPlanList = get_recommendation_plans(
        start_time=start_time, end_time=end_time, db=db
    )

    content_ids: List[str] = []
    user_scheduled_plans: List[Tuple[UUID, UUID, datetime]] = []

    for user_bucket in rec_plan_list.recommendation_plans:
        for item in user_bucket.plans:
            content_ids.append(item.content_id)
            user_scheduled_plans.append(
                (user_bucket.user_id, item.plan_id, item.scheduled_for)
            )

    content_type_map: Dict[str, str] = {}
    if content_ids:
        rows = db.exec(
            select(Recommendation.content_id, Recommendation.content_type).where(
                Recommendation.content_id.in_(set(content_ids))
            )
        ).all()
        content_type_map = {cid: ctype for cid, ctype in rows}

    mission_map: Dict[Tuple[UUID, UUID, datetime], str] = {}
    if user_scheduled_plans:
        rows = db.exec(
            select(
                RecommendationPlan.user_id,
                RecommendationPlan.plan_id,
                RecommendationPlan.scheduled_for,
                Mission.mission_id,
            )
            .join(Mission, Mission.id == RecommendationPlan.mission_id)
            .where(RecommendationPlan.scheduled_for >= start_dt)
            .where(RecommendationPlan.scheduled_for <= end_dt)
            .where(RecommendationPlan.is_deleted == False)
        ).all()
        mission_map = {(u, p, _aware_utc(s)): m for (u, p, s, m) in rows}

    out: Dict[UUID, NotificationUserBucket] = {}

    for user_bucket in rec_plan_list.recommendation_plans:
        items: List[NotificationItem] = []
        for plan_item in user_bucket.plans:
            ctype = content_type_map.get(plan_item.content_id)
            if ctype is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"Missing content_type for content_id={plan_item.content_id}",
                )

            mission_code = mission_map.get(
                (
                    user_bucket.user_id,
                    plan_item.plan_id,
                    _aware_utc(plan_item.scheduled_for),
                )
            )
            if mission_code is None:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Missing mission_id for "
                        f"user_id={user_bucket.user_id}, plan_id={plan_item.plan_id}, "
                        f"scheduled_for={plan_item.scheduled_for.isoformat()}"
                    ),
                )

            items.append(
                NotificationItem(
                    id=plan_item.content_id,
                    send_timestamp=(
                        plan_item.scheduled_for.replace(tzinfo=None)
                        if plan_item.scheduled_for.tzinfo
                        else plan_item.scheduled_for
                    ),
                    type=ctype,
                    mission=mission_code,
                )
            )

        out[user_bucket.user_id] = NotificationUserBucket(contents=items)

    return out
