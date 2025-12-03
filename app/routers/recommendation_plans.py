from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.sql import and_, over
from sqlmodel import Session, join, select

from app.dependencies import get_db
from app.models.mission import Mission
from app.models.recommendation import Recommendation
from app.models.recommendation_plan import RecommendationPlan
from app.models.selected_content import SelectedContent
from app.models.user import MissionContent
from app.schemas.recommendation_plan import (
    NotificationItem,
    NotificationUserBucket,
    RecommendationPlanItem,
    RecommendationPlanList,
    RecommendationPlanResponse,
)
from app.services.recommendation_plan_service import generate_recommendation_plan
from app.utils.helpers import _aware_utc, _iso_to_utc_aware, _to_utc

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
    user_plans: Dict[UUID, List[RecommendationPlanItem]] = {}
    users_with_existing_plans: set[UUID] = set()
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

            users_with_existing_plans.add(plan.user_id)
            sel_ts = selected.mission_start_time
            if plan.mission_id:
                mc_sel = db.exec(
                    select(MissionContent.selection_timestamp).where(
                        MissionContent.user_id == plan.user_id,
                        MissionContent.mission_id == plan.mission_id,
                        MissionContent.selection_timestamp
                        == selected.mission_start_time,
                        MissionContent.is_deleted == False,
                    )
                ).first()
                if mc_sel:
                    sel_ts = mc_sel
            user_plans.setdefault(plan.user_id, []).append(
                RecommendationPlanItem(
                    content_id=rec.content_id,
                    scheduled_for=plan.scheduled_for.astimezone(timezone.utc),
                    plan_id=plan.plan_id,
                    mission_id=mission_code,
                    mission_start_time=(
                        sel_ts.astimezone(timezone.utc) if sel_ts else None
                    ),
                    mission_end_time=(
                        selected.mission_end_time.astimezone(timezone.utc)
                        if selected.mission_end_time
                        else None
                    ),
                )
            )
    # If the window is in the past, do not generate, just return existing
    if end_dt < now:
        response = [
            RecommendationPlanResponse(user_id=uid, plans=items)
            for uid, items in user_plans.items()
        ]
        return RecommendationPlanList(recommendation_plans=response)

    # --------------------- If no plans exist, generate new plans ---------------------
    # Build a subquery with row_number per user (latest first)
    sc_rn = (
        select(
            SelectedContent.id.label("id"),
            SelectedContent.user_id.label("user_id"),
            SelectedContent.contents.label("contents"),
            SelectedContent.mission_start_time.label("mission_start_time"),
            SelectedContent.mission_end_time.label("mission_end_time"),
            func.row_number()
            .over(
                partition_by=SelectedContent.user_id,
                order_by=SelectedContent.mission_start_time.desc(),
            )
            .label("rn"),
        )
        .where(SelectedContent.is_deleted == False)
        .subquery()
    )

    # Pick rn == 1
    selected_contents = db.exec(
        select(SelectedContent)
        .join(sc_rn, sc_rn.c.id == SelectedContent.id)
        .where(sc_rn.c.rn == 1)
    ).all()
    # Resolve mission codes -> Mission UUIDs
    mission_codes: set[str] = set()
    for sc in selected_contents:
        for c in sc.contents or []:
            mc = c.get("mission_id")
            if mc:
                mission_codes.add(mc)
    mission_rows = (
        db.exec(select(Mission).where(Mission.mission_id.in_(mission_codes))).all()
        if mission_codes
        else []
    )
    mission_code_to_uuid = {m.mission_id: m.id for m in mission_rows}
    # Preload MissionContent rows for these users/missions, then map by (user_id, mission_uuid, selection_timestamp)
    user_ids = list({sc.user_id for sc in selected_contents})
    mission_uuids = list(mission_code_to_uuid.values())

    mc_rows = (
        db.exec(
            select(MissionContent).where(
                MissionContent.is_deleted == False,
                MissionContent.user_id.in_(user_ids) if user_ids else False,
                (
                    MissionContent.mission_id.in_(mission_uuids)
                    if mission_uuids
                    else False
                ),
            )
        ).all()
        if user_ids and mission_uuids
        else []
    )
    # Key by (user_id, mission_uuid, selection_timestamp)
    mc_map: dict[tuple[UUID, UUID, datetime | None], MissionContent] = {}
    for mc in mc_rows:
        mc_map[(mc.user_id, mc.mission_id, mc.selection_timestamp)] = mc
    # preload already-scheduled counts per (plan_id, content_id)
    # this gives us how far we’ve progressed for each content in a plan.
    plan_ids = [sc.id for sc in selected_contents]
    already_counts = defaultdict(int)
    if plan_ids:
        rows = db.exec(
            select(RecommendationPlan.plan_id, Recommendation.content_id, func.count())
            .join(
                Recommendation,
                Recommendation.id == RecommendationPlan.recommendation_id,
            )
            .where(
                RecommendationPlan.plan_id.in_(plan_ids),
                RecommendationPlan.is_deleted == False,
            )
            .group_by(RecommendationPlan.plan_id, Recommendation.content_id)
        ).all()
        already_counts.update({(pid, cid): cnt for (pid, cid, cnt) in rows})
    new_plans = []
    for sc in selected_contents:
        row = db.exec(
            select(func.max(RecommendationPlan.scheduled_for)).where(
                RecommendationPlan.user_id == sc.user_id,
                RecommendationPlan.plan_id == sc.id,
                RecommendationPlan.is_deleted == False,
            )
        ).first()
        if row is None:
            latest_scheduled = None
        elif isinstance(row, tuple):
            latest_scheduled = row[0]
        else:
            latest_scheduled = row
        seq_cursor = (
            _aware_utc(latest_scheduled) if latest_scheduled else max(now, start_dt)
        )
        # Build windows per mission_code once
        mission_windows: dict[str, tuple[datetime, datetime]] = {}

        for c in sc.contents or []:
            mcode = c.get("mission_id")
            if not mcode or mcode in mission_windows:
                continue

            mission_uuid = mission_code_to_uuid.get(mcode)

            # Defaults from SelectedContent (fallback)
            sel_ts = sc.mission_start_time
            mc_end = sc.mission_end_time

            # Prefer MissionContent if we know the Mission UUID
            if mission_uuid:
                mc_key = (sc.user_id, mission_uuid, sc.mission_start_time)
                mc = mc_map.get(mc_key)
                if mc:
                    sel_ts = mc.selection_timestamp
                    mc_end = mc.mission_end_time

            # Normalize to UTC-aware
            if sel_ts is not None:
                sel_ts = (
                    sel_ts.replace(tzinfo=timezone.utc)
                    if sel_ts.tzinfo is None
                    else sel_ts.astimezone(timezone.utc)
                )
            if mc_end is not None:
                mc_end = (
                    mc_end.replace(tzinfo=timezone.utc)
                    if mc_end.tzinfo is None
                    else mc_end.astimezone(timezone.utc)
                )

            # Clip times
            window_start = max(start_dt, now, sel_ts or start_dt)
            window_end = min(end_dt, mc_end or end_dt)

            mission_windows[mcode] = (window_start, window_end)

        # ----------------------- sequence-aware scheduling loop ----------------------
        # We walk the list in order and only schedule the (k+1)-th occurrence
        # if k have already been scheduled for this plan_id/content_id.
        seen_so_far = defaultdict(int)  # per content_id within this list
        for c in sc.contents or []:
            cid = c.get("id")
            mcode = c.get("mission_id")
            if not cid or not mcode:
                continue

            # Which occurrence is this within the list? (1-based)
            next_occ_idx = seen_so_far[cid] + 1
            # How many of this content_id have already been scheduled for this plan?
            already_for_cid = already_counts[(sc.id, cid)]
            # If we already scheduled that many, skip to the next content in sequence
            if already_for_cid >= next_occ_idx:
                seen_so_far[cid] += 1
                continue
            # Otherwise, attempt to schedule this occurrence in the window for its mission
            window_start, window_end = mission_windows.get(mcode, (start_dt, end_dt))
            effective_start = max(window_start, seq_cursor)
            if window_end <= effective_start:
                seen_so_far[cid] += 1
                continue
            made = generate_recommendation_plan(
                sc.user_id,
                {cid: mcode},  # single item to preserve exact order
                sc.id,  # plan_id
                effective_start,
                window_end,
                db,
            )
            if made:
                new_plans.extend(made)
                # advance the sequence cursor so following items can't land earlier in time
                seq_cursor = max(
                    seq_cursor, *(_aware_utc(p.scheduled_for) for p in made)
                )
                already_counts[(sc.id, cid)] += 1
            # Move to the next list position either way
            seen_so_far[cid] += 1

    # Commit all new plans to the database
    db.commit()

    # Format the response for the newly created plans
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
    selected_rows_map = {s.id: s for s in selected_rows}
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
        # prefer MissionContent.selection_timestamp; fallback to SelectedContent.mission_start_time
        sc = selected_rows_map.get(plan.plan_id)
        sel_ts = sc.mission_start_time if sc else None
        end_ts = sc.mission_end_time if sc else None
        if plan.mission_id and sc:
            mc = mc_map.get((plan.user_id, plan.mission_id, sc.mission_start_time))
            if mc and mc.selection_timestamp:
                sel_ts = mc.selection_timestamp
        mst_utc = _to_utc(sel_ts)
        met_utc = _to_utc(end_ts)
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
    for uid, items in user_plans.items():
        items.sort(key=lambda it: it.scheduled_for)

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
                    selection_timestamp=(
                        plan_item.mission_start_time.replace(tzinfo=None)
                        if (
                            plan_item.mission_start_time
                            and plan_item.mission_start_time.tzinfo
                        )
                        else plan_item.mission_start_time
                    ),
                )
            )

        items.sort(key=lambda it: (it.send_timestamp, it.id or ""))
        out[user_bucket.user_id] = NotificationUserBucket(contents=items)

    return out
