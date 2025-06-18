from datetime import datetime
from typing import Dict, List

from pydantic import BaseModel


class RecommendationPlanItem(BaseModel):
    content_id: str
    scheduled_for: datetime


class RecommendationPlanResponse(BaseModel):
    user_id: str
    plans: List[RecommendationPlanItem]


class RecommendationPlanList(BaseModel):
    recommendation_plans: List[RecommendationPlanResponse]
