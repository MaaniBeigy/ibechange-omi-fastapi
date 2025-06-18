from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.recommendation_plan import RecommendationPlan
from app.models.reminder import Reminder
from app.models.selected_content import SelectedContent
from app.schemas.recommendation_plan import (
    RecommendationPlanItem,
    RecommendationPlanList,
    RecommendationPlanResponse,
)
from app.services.recommendation_plan_service import generate_recommendation_plan

router = APIRouter(prefix="/recommendation_plans", tags=["Recommendation Plans"])


@router.get("/", response_model=RecommendationPlanList)
def get_recommendation_plans(
    start_time: datetime = Query(...),
    end_time: datetime = Query(...),
    db: Session = Depends(get_db),
):
    # Check if plans already exist for the given time range and users
    plans = db.exec(
        select(RecommendationPlan)
        .where(RecommendationPlan.scheduled_for >= start_time)
        .where(RecommendationPlan.scheduled_for <= end_time)
        .where(RecommendationPlan.is_deleted == False)
    ).all()

    if plans:
        # Group plans by user_id
        user_plans = {}
        for plan in plans:
            if plan.user_id not in user_plans:
                user_plans[plan.user_id] = []
            user_plans[plan.user_id].append(
                RecommendationPlanItem(
                    content_id=plan.content_id, scheduled_for=plan.scheduled_for
                )
            )

        response = [
            RecommendationPlanResponse(user_id=user_id, plans=items)
            for user_id, items in user_plans.items()
        ]
        return RecommendationPlanList(recommendation_plans=response)

    # Generate new plans if none exist
    selected_contents = db.exec(
        select(SelectedContent).where(SelectedContent.is_deleted == False)
    ).all()
    reminders = db.exec(select(Reminder).where(Reminder.is_deleted == False)).all()

    new_plans = []
    for content in selected_contents:
        plans = generate_recommendation_plan(
            content.user_id, content.contents, start_time, end_time, db
        )
        new_plans.extend(plans)

    for reminder in reminders:
        for rem in reminder.reminders:
            plan = RecommendationPlan(
                user_id=reminder.user_id,
                content_id=rem["Id"],
                scheduled_for=datetime.fromisoformat(rem["Sent_date"]),
            )
            db.add(plan)
            new_plans.append(plan)

    db.commit()

    # Format response
    user_plans = {}
    for plan in new_plans:
        if plan.user_id not in user_plans:
            user_plans[plan.user_id] = []
        user_plans[plan.user_id].append(
            RecommendationPlanItem(
                content_id=plan.content_id, scheduled_for=plan.scheduled_for
            )
        )

    response = [
        RecommendationPlanResponse(user_id=user_id, plans=items)
        for user_id, items in user_plans.items()
    ]
    return RecommendationPlanList(recommendation_plans=response)
