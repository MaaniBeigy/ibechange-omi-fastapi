from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel


class RecommendationPlanItem(BaseModel):
    content_id: str
    scheduled_for: datetime
    plan_id: UUID
    mission_id: str
    mission_start_time: Optional[datetime] = None
    mission_end_time: Optional[datetime] = None


class RecommendationPlanResponse(BaseModel):
    user_id: UUID
    plans: List[RecommendationPlanItem]


class RecommendationPlanList(BaseModel):
    recommendation_plans: List[RecommendationPlanResponse]


class NotificationItem(BaseModel):
    id: str
    send_timestamp: datetime
    type: str
    mission: str
    selection_timestamp: Optional[datetime] = None


class NotificationUserBucket(BaseModel):
    contents: List[NotificationItem]
