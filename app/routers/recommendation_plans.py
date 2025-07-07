from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.recommendation_plan import RecommendationPlan
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
    start_time: str = Query(...),
    end_time: str = Query(...),
    db: Session = Depends(get_db),
):
    # Parse the input start and end times from ISO format
    try:
        start_dt = datetime.fromisoformat(start_time)
        end_dt = datetime.fromisoformat(end_time)
    except ValueError:
        # Return 422 if the datetime format is invalid
        raise HTTPException(status_code=422, detail="Invalid datetime format")

    # Query for existing recommendation plans in the given time range
    plans = db.exec(
        select(RecommendationPlan)
        .where(RecommendationPlan.scheduled_for >= start_dt)
        .where(RecommendationPlan.scheduled_for <= end_dt)
        .where(RecommendationPlan.is_deleted == False)
    ).all()

    if plans:
        # If plans exist, group them by user_id and format the response
        user_plans = {}
        for plan in plans:
            if plan.user_id not in user_plans:
                user_plans[plan.user_id] = []
            user_plans[plan.user_id].append(
                RecommendationPlanItem(
                    content_id=plan.content_id, scheduled_for=plan.scheduled_for
                )
            )
        # Build the response as a list of RecommendationPlanResponse objects
        response = [
            RecommendationPlanResponse(user_id=user_id, plans=items)
            for user_id, items in user_plans.items()
        ]
        return RecommendationPlanList(recommendation_plans=response)

    # If no plans exist, generate new plans
    # Fetch all non-deleted SelectedContents
    selected_contents = db.exec(
        select(SelectedContent).where(SelectedContent.is_deleted == False)
    ).all()

    new_plans = []
    for content in selected_contents:
        # Extract content_id strings from list of dicts in SelectedContent
        content_ids = [c["id"] for c in content.contents]
        # Generate recommendation plans for each user/content
        plans = generate_recommendation_plan(
            content.user_id, content_ids, start_dt, end_dt, db
        )
        new_plans.extend(plans)

    # Commit all new plans to the database
    db.commit()

    # Format the response for the newly created plans
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
